import argparse
from datetime import datetime
import html
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

from bs4 import BeautifulSoup
import pandas as pd
from tqdm import trange

import util.AggregateUtil as Aggregate
import util.ConfigUtil as Config
import util.DeepUtil as Deep
import util.DumpUtil as Dump
import util.GetAllMomentsUtil as GetAllMoments
import util.GetFeedsUtil as GetFeeds
import util.HtmlUtil as Html
import util.RequestUtil as Request
import util.RedownloadUtil as Redownload
import util.ResumeUtil as Resume
import util.SessionUtil as SessionUtil
import util.StoreUtil as StoreUtil
import util.ToolsUtil as Tools

sys.stdout.reconfigure(encoding="utf-8")
session = None
user_nickname = None
resumable = None  # ResumeUtil 实例（PC 流），signal_handler / parse_batch 用
store = None      # StoreUtil 实例（B 线），signal_handler 导出用
user_message = list()
leave_message = list()
forward_message = list()
other_message = list()


def signal_handler(sig, frame):
    # 手动结束时数据已逐批落库/落 jsonl，这里直接从库导出
    print("\n收到中断信号，正在导出已抓数据...")
    if store is not None and store.count() > 0:
        save_data(store, open_result=False)
    sys.exit(0)


def content_author(content):
    """取内容开头的作者昵称（「名字 + 连续空白」段）。

    不用 `user_nickname in content` 子串匹配——好友正文里提到本人昵称会被误归「我的说说」。
    """
    s = (content or "").strip()
    m = re.match(r"^(.*?)\s{2,}", s)
    if m:
        return m.group(1).strip()
    return s.split("：", 1)[0].strip()




def stored_nickname(store):
    """离线导出用的本人昵称：取库中正文作者前缀的众数（拿不到返回空串，调用方退回 uin）。

    不走 `get_login_user_info`——那条路登录态过期时会弹二维码，而纯导出不该联网（见 run_export_only）。
    """
    counter = {}
    for (content,) in store.conn.execute(
            "SELECT content FROM feeds WHERE content IS NOT NULL AND content != ''"):
        author = content_author(content)
        if author:
            counter[author] = counter.get(author, 0) + 1
    return max(counter.items(), key=lambda kv: kv[1])[0] if counter else ""


def image_url(img_element):
    """`a.img-item` 里的图片链接，无有效图返回 None。

    懒加载图的真地址在 `img[onload]` 的 `trueSrc:'…'` 里，`img[src]` 只是占位 `/ac/b.gif`。
    """
    if img_element is None:
        return None
    img = img_element.find("img")
    if img is None:
        return None
    src = (img.get("src") or "").strip()
    if src and not src.startswith("http"):
        matched = re.search(r"trueSrc:'([^']*)'", img.get("onload") or "")
        if matched:
            src = re.sub(r"\\+/", "/", matched.group(1))
    if not src:
        return None
    src = src.replace("\\/", "/").replace("&amp;", "&").strip()
    if not src.startswith("http") or "qzonestyle.gtimg.cn" in src:
        return None
    return src


def parse_li(element, key, raw_html):
    """解析互动流里的一条 <li>，返回 (行, 好友)；解不出正文则行是 None。

    行 = [feed_key, 时间, 正文, 图片, 评论(空), raw_html, 动作词, 互动人uin]。
    """
    friend_element = element.find("a", class_="f-name q_namecard")
    friend = None
    if friend_element is not None:
        link_value = friend_element.get("link")
        friend = [
            friend_element.get_text(),
            link_value.removeprefix("nameCard_") if isinstance(link_value, str) else "",
            friend_element.get("href"),
        ]
    friend_qqid = friend[1] if friend else ""

    # 动作词：div.f-nick 里 <span class="ui-mr10 state">（赞了/评论/…）；老库空值重抓回填。
    # 限定在 f-nick 里：无 f-nick 的 li 上同 class 的 span 可能是别的东西（实测取到过日
    # 期串，docs/03 §306 那批 29 行时间串 action 同根因）；兜底再排一次日期形状。
    state_element = element.select_one("div.f-nick span.ui-mr10.state") \
        or element.select_one("span.ui-mr10.state")
    action = state_element.get_text().strip() if state_element is not None else ""
    # 日期形状守卫：safe_strptime 只认带年份的串，无年份时间（「5月8日 14:24」）兜不住
    if action and (Tools.safe_strptime(action) or Tools._YEARLESS_TIME.match(action)):
        action = ""

    time_element = element.find("div", class_="info-detail")
    # 必须用单类名：bs4 里是包含匹配，能通吃三种标题变体；写全类名是顺序敏感精确匹配，
    # 会把相册/卡片式转发当「无文本」丢弃（docs/03 §五）。
    text_element = element.find("p", class_="txt-box-title")
    if time_element is None or text_element is None:
        return None, friend

    put_time = time_element.get_text().replace("\xa0", " ").strip()
    # 本年动态服务端不给年份（「5月8日 14:24」），年份在 li id 的时间戳里。补不了留给 --fix-times。
    put_time = Tools.fill_missing_year(put_time, Tools.year_from_li_id(element.get("id")))
    # 原po昵称被服务端包在 q_namecard 里，get_text() 一扁平化边界就没了——「原po昵称 ：
    # 原文」会和正文糊成一片，而昵称本身可能带冒号（`magnet:?xt=urn:btih: ☭☭☭☭☭`）。
    # 扁平化前把 uin 与昵称一起裹进哨兵，Web 端据此上样式并挂 QQ 号气泡。
    # 跳过第 1 个（作者前缀），裹了会让 content_author 分桶失配。
    for anchor in text_element.find_all("a", class_="q_namecard")[1:]:
        uin = str(anchor.get("link") or "").removeprefix("nameCard_")
        anchor.string = (f"{Tools.NAME_OPEN}{uin}{Tools.NAME_SEP}"
                         f"{anchor.get_text()}{Tools.NAME_CLOSE}")
    raw_text = (
        text_element.get_text()
        .replace("\xa0", " ")
        .replace("​", "")
        .replace("﻿", "")
        .strip()
    )
    # 用正则剥离点赞/评论前缀，还原真实的已删除说说内容（保留转发链）
    clean_text = re.sub(
        r"^.*?(赞了|评论了|回复了|留言|赞过|查看了|觉得很赞|访问了空间|在照片中圈了你)[：:\s]*",
        "",
        raw_text,
    ).strip()

    # 卡片/相册/视频的落地链接在 li 的 a.c_tx（q_namecard 那个是作者名片，要排除；
    # `javascript:` 的「评论」也要排除）。整条 li 按文档序取第一个即可。
    card_link = ""
    for anchor in element.find_all("a", class_="c_tx"):
        if "q_namecard" in (anchor.get("class") or []):
            continue
        href = Tools.share_url(anchor.get("href"))
        if href and not href.startswith("javascript:"):
            card_link = href
            break
    if card_link and card_link not in clean_text:
        clean_text = f"{clean_text} {card_link}".strip()

    if not clean_text:
        return None, friend
    return [key, put_time, clean_text, image_url(element.find("a", class_="img-item")),
            parse_comments(element, put_time), raw_html, action, friend_qqid], friend


def parse_comments(element, put_time):
    """li 里的评论/回复 → [[时间, 正文, 昵称, uin], ...]；没有则空表。

    pc 互动流只回事件，但评论正文就在**同一条 li 的留档 HTML** 里：每个 `.comments-content`
    是一句「昵称 : 正文」（回复是「昵称 回复 某人 : 正文」）。不捞它，渲染时评论卡只剩
    动作词「评论」。

    时间一律用**评论人自己的** `.comments-op`；取不到退化成事件时间（比留空好，渲染器不按
    时间匹配）。`get_text()` 前先摘掉 .comments-op，否则时间戳会混进正文。
    """
    rows = []
    for block in element.select(".comments-content"):
        op = block.select_one(".comments-op")
        op_time = op.get_text().replace("\xa0", " ").strip() if op is not None else ""
        if op is not None:
            op.decompose()
        anchor = block.find("a", class_="q_namecard")
        link = anchor.get("link") if anchor is not None else None
        # link 可能是「nameCard_<uin> des_<uin>」（头像那个 img 就长这样），取第一段即可；
        # 整串塞进去会让 uin 匹配（评论正文并进互动小框）对不上。
        uin = str(link).removeprefix("nameCard_").split()[0] if link else ""
        nickname = anchor.get_text().strip() if anchor is not None else ""
        if anchor is not None:
            anchor.decompose()
        # 剩下的是「 : 正文」或「 回复 某人 : 正文」；只剥到第一个冒号为止，
        # 正文自己带冒号（「提示：答案有误」）不能一起切掉。
        said = re.sub(r"^[^：:]{0,40}?[：:]\s*", "", block.get_text().replace("\xa0", " ").strip())
        said = said.strip()
        if not said:
            continue
        rows.append([op_time or put_time, said, nickname or None, uin or None])
    return rows


def parse_batch(message, pos):
    """解析一批 PC 互动流响应，返回 (batch, friends)。

    batch: [(feed_key, time, content, img, comments, raw, action, actor), ...]；friends: [name, qq, link]。
    """
    reason = Resume.classify_body(message)  # 登录态/WAF/限流兜底检查
    if reason in Resume.FATAL_REASONS:
        raise Resume.FatalFetchError(f"批次 {pos}: {reason}")
    if reason == "throttled":
        # 软限流 ≠ 空页：单独标注（可重试），不混进 "parse/无 html 字段"
        if resumable is not None:
            resumable.record_skip(pos, "throttled", "服务端软限流 network busy")
        print(f"批次 {pos} 被限流(network busy)，已记入 skips（可重试）")
        return [], []
    items = Tools.extract_items_with_keys(message)
    if not items:
        if resumable is not None:
            resumable.record_skip(pos, "parse", "无 html 字段")
        print(f"批次 {pos} 无 html 字段，已记入 skips")
        return [], []
    soup = BeautifulSoup("".join(html for _, html in items), "html.parser")
    feed_keys = [k for k, _ in items]
    friends = []
    batch = []
    # 每个 item 恰一条 li；只有部分 li 能解出时间+文本（其余为纯事件/折叠头），按索引回查 key
    lis = soup.find_all("li", class_="f-single f-s-s")
    if len(feed_keys) != len(lis):
        print(f"[WARN] 批次 {pos}: li({len(lis)}) 与 item({len(feed_keys)}) 数不一致，key 可能错位")
    for idx, element in enumerate(lis):
        key = feed_keys[idx] if idx < len(feed_keys) else ""
        # raw 放该 item 自己的 html 片段：留档后改解析器可离线重算，不必重抓
        raw_html = items[idx][1] if idx < len(items) else ""
        row, friend = parse_li(element, key, raw_html)
        if friend is not None:
            # 互动人 uin 与 key 无关（评论行的 key 是评论自己的 id），单独带上
            friends.append(friend)
        if row is not None:
            batch.append(row)
    return batch, friends


def fetch_pc_interactions(args, store):
    """流 A：PC 互动流，断点续传 + 坏页跳过 + 速率窗口，B 线落库。"""
    global resumable
    resumable = Resume.ResumeUtil(session.uin, prefix="pc_", rate_limit=args.rate)
    count = Request.get_message_count(session, rate_limiter=resumable)
    print(f"互动流总量(二分): {count}")

    if args.fresh:
        resumable.clear_checkpoint()
    else:
        done = store.count("pc")
        if done:
            print(f"库中已有 {done} 条 PC 互动记录（重复批次由 feed_key 幂等去重）")
        elif resumable.load_skips():
            print("发现 skips 记录但无已存条目（--retry-skips 可单独重试坏页）")

    start_i = resumable.checkpoint_pos() + 1
    total_batches = int(count / 10) + 1
    if start_i >= total_batches:
        # 陈旧断点越过本轮二分末尾：循环为空，但下方仍走 Once 一次 retry_skips
        # （此前静默 return，坏页没人管）
        print("断点已越过本轮二分的末尾，批次循环为空（仍会做坏页重试）")

    fetched = 0
    try:
        for i in trange(start_i, total_batches, initial=start_i, total=total_batches,
                        desc="Progress", unit="10条"):
            if args.max_batches and fetched >= args.max_batches:
                print(f"已达到本批上限 {args.max_batches} 批，中断（断点已保存，可直接重跑续传）")
                return
            offset = i * 10
            response = Request.get_message(session, offset, 10, rate_limiter=resumable)
            if response is None or not hasattr(response, "content"):
                resumable.record_skip(offset, "timeout", "响应为空或超时")
                print(f"获取消息失败：offset {offset}，已记入 skips")
                continue
            message = response.content.decode("utf-8", errors="replace")
            try:
                batch, friends = parse_batch(message, offset)
            except Resume.FatalFetchError:
                # 必须穿透 except Exception 上抛中止，不能把登录态失效当解析坏页吞掉
                raise
            except Exception as e:
                resumable.record_skip(offset, "parse", str(e))
                print(f"批次 {offset} 解析异常: {e}，已记入 skips")
                continue
            new = store.upsert_feeds(batch, source="pc")
            for name, qq, link in friends:
                store.upsert_friend(qq, name, link)
            if new:
                print(f"批次 {offset}: 落库 {new}/{len(batch)} 条新记录")
            resumable.save_checkpoint(i)
            fetched += 1
    except Resume.FatalFetchError as e:
        print(f"\n不可恢复错误，主循环中止: {e}")
        print("（登录态失效通常需重新扫码；断点与已抓数据已保存，重登后直接重跑续传）")
        return
    except KeyboardInterrupt:
        print("\n手动中断，断点与已抓数据已保存")
        return

    # 正常跑完：定向重试坏页（C2）
    retry_skips(store)


def retry_skips(store):
    """对 skips.jsonl 里记录的坏页单条重试；成功则补数据并移除记录。"""
    skips = resumable.load_skips()
    if not skips:
        return
    print(f"\n共 {len(skips)} 条坏页记录，开始定向重试...")
    done_pos = set()
    for s in skips:
        pos = s["pos"]
        try:
            response = Request.get_message(session, pos, 10, rate_limiter=resumable)
        except Resume.FatalFetchError as e:
            print(f"重试 offset {pos} 遇到不可恢复错误: {e}，中止重试")
            return
        if response is None or not hasattr(response, "content"):
            print(f"重试 offset {pos} 仍失败（超时/空响应）")
            continue
        message = response.content.decode("utf-8", errors="replace")
        try:
            batch, friends = parse_batch(message, pos)
        except Exception as e:
            print(f"重试 offset {pos} 解析仍异常: {e}")
            continue
        new = store.upsert_feeds(batch, source="pc")
        for name, qq, link in friends:
            store.upsert_friend(qq, name, link)
        done_pos.add(pos)
        print(f"重试 offset {pos} 成功，补入 {new} 条")
    resumable.remove_skips(done_pos)
    remaining = resumable.load_skips()
    if remaining:
        print(f"仍有 {len(remaining)} 条坏页未恢复，明细见 {resumable.skips_path}")


def fetch_pc_deep(args, store):
    """深区补洞（opt-in）：浅区之外的稀疏深 offset 扫描，逻辑在 util/DeepUtil。

    parse_batch 通过模块全局 resumable 记 skip，故临时换成 pcdeep_ 实例——否则深区的
    "无 html 字段"噪声会污染浅区 pc_ 的 skips（retry_skips 会拿深区 offset 反复白跑）。"""
    global resumable
    shallow_resumable = resumable
    resumable = Resume.ResumeUtil(session.uin, prefix="pcdeep_", rate_limit=args.deep_rate)
    try:
        if args.fresh:
            resumable.clear_checkpoint()
        cells = None
        if args.deep_offsets:
            # 无历史首选 set → None，让 fetch_cell 从 set0 起轮
            cells = [(int(x), None) for x in args.deep_offsets.split(",") if x.strip()]
        print(f"\n===== 深区扫描开始（独立配额窗口 {args.deep_rate} 页/"
              f"{Resume.WINDOW_SECONDS // 60} 分钟）=====", flush=True)
        Deep.fetch_deep_band(session, store, parse_batch, resumable, cells=cells)
    finally:
        resumable = shallow_resumable


def fetch_mobile_source(args, store):
    """流 A 的 mobile 替代/补充源（get_feeds），游标断点续传 + 落库。"""
    mobile_resumable = Resume.ResumeUtil(session.uin, prefix="mobile_",
                                         rate_limit=args.rate)
    rows, pages = GetFeeds.fetch_all_feeds(
        session, max_pages=args.max_pages, rate_limiter=mobile_resumable,
        resumable=mobile_resumable, fresh=args.fresh, store=store)
    print(f"get_feeds 共 {pages} 页，解析 {len(rows)} 条"
          f"（库中 mobile 现有 {store.count('mobile')} 条）")


def build_posts(store):
    """库 → 导出用「原动态」列表（E1 聚合）。

    pc 与 mobile 都是事件流、键归一化后共享主键，故一起聚合：一条说说被 N 人互动只出一行。

    taotao 是本人原帖的权威源（正文含 [em]、图片齐全），feed_key 尾 10 位即互动流的原动态
    id：能对上的并入该组，对不上的（墙外时期）保留为独立一帖。
    """
    posts = []
    event_rows = store.load_rows(
        columns="feed_key, time, content, pictures, action, actor, comments",
        source="pc")
    event_rows += store.load_rows(
        columns="feed_key, time, content, pictures, action, actor, comments",
        source="mobile")
    taotao_rows = store.load_rows(
        columns="feed_key, time, content, pictures, comments", source="taotao")
    friends = store.load_friends()
    # 评论人昵称跟互动人一样走好友表**备注**，不认接口回的自选昵称——同一个人点赞与评论
    # 会显示成两个不同的名字，同屏对不上。不是好友（墙外时期/未收录）才回退接口昵称。
    remark = {str(f[1]): f[0] for f in friends if len(f) >= 2 and f[1]}

    def with_remarks(rows):
        return [[c[0], c[1], remark.get(str(c[3]), "") or c[2], c[3]]
                if len(c) >= 4 else c for c in (rows or [])]

    # 同一条说说的事件键写法可能不一致（键第三段未必等于留档 id 末 10 位），先归一成一类再
    # 聚合，否则一条说说导出两份。留档 id 由 Store 从 raw_json 里按字节抠出（不整读留档）；
    # 「正文」一列喂给 hash_alias 做结构关系的把关，见 AggregateUtil.hash_alias。
    mids = store.moment_ids("pc")
    bodies = {}
    for r in event_rows + taotao_rows:
        h = Aggregate.parse_feed_key(r[0])[1]
        if h and len(r[2] or "") > len(bodies.get(h, "")):
            bodies[h] = (r[2] or "").strip()
    alias = Aggregate.hash_alias(
        [(Aggregate.parse_feed_key(k)[1], mid[-10:]) for k, mid in mids], bodies)
    # 键形烂到 parse_feed_key 认不出的行（真库 10 行 27 位、夹非 hex 字符）只能靠留档的时刻
    # id 定位——alias 按 hash 连边，这些行压根没解出 hash。
    hints = {k: mid[-10:] for k, mid in mids}

    # 按 group_key 取键，不能按 orig：无 hash 的兜底组（`c:` 内容 / `u:` key）orig 全是空串，
    # 按 orig 建字典会把它们全塌成一条，除最后一条外静默消失——实测丢 392 条，其中 203 条
    # 正文在导出里没有第二份（167 条带图，2015-2023）。
    originals = {o["group_key"]: o
                 for o in Aggregate.aggregate(event_rows, friends, alias, hints)}
    for o in originals.values():
        o["comments"] = with_remarks(o["comments"])
    # taotao 键尾段也是原动态 id，但可能落在某个归一类里（同一条说说有两种写法），故过 alias 再查
    for key, t, c, p, cm in taotao_rows:
        cm = with_remarks(cm)
        kh = Aggregate.parse_feed_key(key)[1]
        o = originals.get("h:" + alias.get(kh, kh)) if kh else None
        if o is not None:
            # taotao 是原帖权威源：图片以它为准——组内事件行只带互动时刻的单图，
            # 拿它占位会把 taotao 的全量图挡在门外（9 图帖只剩 1 图）
            if c and len(c) > len(o["content"]):
                o["content"] = c
            if p:
                o["pictures"] = p
            if cm:
                o["comments"].extend(cm)
            if t:
                times = [o["first_time"], o["last_time"], t]
                o["first_time"] = Deep.time_min(times)
                o["last_time"] = Deep.time_max(times)
        else:
            posts.append({"time": t, "content": c, "pictures": p,
                          "interactors": [], "comments": cm or []})
    for o in originals.values():
        posts.append({
            "time": o["first_time"],
            "content": o["content"],
            "pictures": o["pictures"],
            "interactors": o["actors"],
            "comments": o["comments"],
        })
    return sort_posts(posts)


def sort_posts(posts):
    """按时间倒序（新的在前）。时间解不出来的（无年份串等）沉到最底，不抛。

    中文日期串不能直接比较（「10月」会排到「8月」前），一律走 safe_strptime。
    """
    return sorted(posts,
                  key=lambda p: Tools.safe_strptime(str(p["time"])) or datetime.min,
                  reverse=True)


# B 线后 Excel/HTML 只是库数据的导出视图，不再有自己的数据流；此为 Excel 的列名
EXPORT_COLUMNS = ["时间", "内容", "图片链接", "评论", "互动人"]


def posts_to_rows(posts, pic_dir=None):
    """导出用的行：评论存 JSON 串，互动人按动作词分组拼昵称（无互动则空）。

    正文去掉昵称哨兵——哨兵只给 Web 端上样式用，Excel 里是噪声。

    `图片链接` 列写**相对路径** `pic/<指纹>.jpg`（本地没有才回落原 URL）：与 HTML 同源的本地
    文件，把整个 `<uin>/` 目录拷到哪儿都还有效。原先存带时效签名的完整 URL，CDN 一过期整列
    成死链，`vuin=` 段里还带着真号。
    """
    out = []
    for p in posts:
        pics = [
            Redownload.local_first(pic_dir, *Redownload.photo(url))
            for _fp, url in Redownload.iter_pics(p["pictures"])
        ]
        out.append([
            p["time"],
            Tools.strip_names(p["content"]),
            ", ".join(pics),
            json.dumps(p["comments"], ensure_ascii=False) if p["comments"] else "",
            Aggregate.format_actors_by_action(p["interactors"]),
        ])
    return out


def download_pictures(posts, pic_save_path, force=False):
    """落盘本次导出要显示的图片：正文图（原图）+ 互动人头像 + 表情。

    命名与渲染器共用 util.RedownloadUtil 一份实现——旧版在这里按正文给文件起名、重名再补
    时间戳，与指纹命名并存，pic/ 里因此叠了两代文件（实测 5740 个里 2894 个无人引用的孤儿）。

    force=False（日常路径）时已存在的跳过，只补缺的；force=True 把早先存的中图重下成原图
    ——下载失败不写盘，故不会拿失败结果盖掉手里已有的中图。
    """
    items = Redownload.render_items(posts, [session.uin])
    Redownload.download_items(pic_save_path, items, force=force)
    return len(items)


def save_data(store, open_result=True, skip_pics=False, force_pics=False):
    user_save_path = Config.result_path + session.uin + "/"
    pic_save_path = user_save_path + "pic/"
    os.makedirs(user_save_path, exist_ok=True)
    os.makedirs(pic_save_path, exist_ok=True)

    posts = build_posts(store)
    all_friends = store.load_friends()

    # 按内容里的作者昵称分类（子串匹配会把提到本人昵称的好友说说误归）。必须先清空：
    # 这些模块级列表在 signal_handler 导出后仍保留，第二次导出会重复累加。
    for lst in (user_message, forward_message, leave_message, other_message):
        lst.clear()
    for p in posts:
        text = p["content"] or ""
        if content_author(text) == user_nickname:
            if "留言" in text:
                leave_message.append(p)
            elif "转发" in text:
                forward_message.append(p)
            else:
                user_message.append(p)
        else:
            other_message.append(p)

    columns = EXPORT_COLUMNS

    # 图片先落盘再渲染：渲染器按「本地有就优先本地」决定 src，顺序反了它就只能回落 CDN。
    pic_count = 0 if skip_pics else download_pictures(posts, pic_save_path, force=force_pics)

    # 四个桶各占一个 tab，不再单出一份「全部」表：它是四个桶的并集，等于把同一批行
    # 存两遍（实测占该表 84% 的字符量），而「全部」视图 HTML 已经给了。
    # 空桶不建 tab——只有表头的空表纯属噪声（留言桶在多数账号上是空的）。
    with pd.ExcelWriter(user_save_path + session.uin + "_归档.xlsx") as writer:
        for name, rows in [("说说", user_message), ("转发", forward_message),
                           ("留言", leave_message), ("其他", other_message)]:
            if rows:
                pd.DataFrame(posts_to_rows(rows, pic_save_path), columns=columns).to_excel(
                    writer, sheet_name=name, index=False
                )
        pd.DataFrame(all_friends, columns=["昵称", "QQ", "空间主页"]).to_excel(
            writer, sheet_name="好友", index=False
        )

    # 网页版是「全部」视图：说说与转发两个桶各自有序，拼接后整体却无序（转发块会整段
    # 落到 2015 年说说之后），故合并后再排一次。
    Html.render_html(
        sort_posts(user_message + forward_message),
        os.path.join(os.getcwd(), user_save_path, session.uin + "_说说网页版.html"),
        session.uin,
        user_nickname,
        pic_dir=pic_save_path,
    )

    Tools.show_author_info()
    print(
        "\033[36m"
        + "导出成功，请查看 "
        + user_save_path
        + session.uin
        + " 文件夹内容"
        + "\033[0m"
    )
    print("\033[32m" + "共有 " + str(len(posts)) + " 条原动态" + "\033[0m")
    # 无年份串在 sort_posts 里沉底，拿 posts[-1] 当「最早」是假最早——只从解得出日期的行取
    dated = [p for p in posts if Deep.is_dated(p["time"])]
    if dated:
        earliest = min(dated, key=lambda p: Deep.time_sort_key(p["time"]))
        print("\033[36m" + "最早的一条说说发布在" + str(earliest["time"]) + "\033[0m")
    print("\033[32m" + "好友列表共有 " + str(len(all_friends)) + " 个好友" + "\033[0m")
    print("\033[36m" + "说说列表共有 " + str(len(user_message)) + " 条说说" + "\033[0m")
    print("\033[32m" + "转发列表共有 " + str(len(forward_message)) + " 条转发" + "\033[0m")
    print("\033[36m" + "留言列表共有 " + str(len(leave_message)) + " 条留言" + "\033[0m")
    print("\033[32m" + "其他列表共有 " + str(len(other_message)) + " 条内容" + "\033[0m")
    # 别报 pic/ 的总文件数：里面还堆着早年版按正文命名的孤儿（没人引用），报出来只会误导。
    print("\033[36m" + f"本地图片 {len(os.listdir(pic_save_path))} 个文件"
          + (f"（本次网页版用到 {pic_count} 张）" if pic_count else "") + "\033[0m")
    if open_result:
        open_file(os.getcwd() + user_save_path[1:])
        # 停住控制台，避免双击运行时窗口一闪而过。只在**会打开结果**（双击/交互式）时停：
        # `--no-open` 是脚本在驱动，等按键会让进程永远挂着——macOS 没有 `pause` 命令，
        # `os.system("pause")` 正好命中 zsh 的 `pause` 内建，无 tty 时无限等待
        # （2026-09-29 两个后台导出各挂了数小时）。
        if platform.system() == "Windows":
            os.system("pause")
        else:
            os.system("stty raw -echo;dd bs=1 count=1 >/dev/null 2>&1;stty cooked echo")


def open_file(file_path):
    # 各平台用系统默认方式打开目录；Linux 依次退化到常见桌面环境的打开工具
    openers = {
        "Darwin": ["open"],
        "Linux": ["xdg-open", "gnome-open", "kde-open"],
    }
    if platform.system() == "Windows":
        os.startfile(file_path)
        return
    for cmd in openers.get(platform.system(), []):
        if shutil.which(cmd):
            subprocess.run([cmd, file_path])
            return
    if platform.system() == "Linux":
        print("未找到可用的打开命令，请手动打开文件。")
    else:
        print(f"Unsupported OS: {platform.system()}")


def saved_uin(user_file):
    """从已存登录态文件读裸 uin（不联网、不出码）。

    `--user` 是登录态**文件名**（带 o 前缀的原始 cookie uin），库文件名/结果目录却用归一化裸号，
    拿文件名当 uin 会指向另一个库。
    """
    cookies = Config.select_saved_login(user_file) if user_file else None
    return SessionUtil.normalize_uin(cookies.get("uin")) if cookies else ""


def backup_db(db_path):
    """整库备份到 .db.bak-<日期>，返回路径。

    用 sqlite3 backup API 而非 shutil.copy：库开着 WAL，直接拷主文件会丢掉未 checkpoint 的事务。
    """
    backup = f"{db_path}.bak-{time.strftime('%Y%m%d')}"
    if os.path.exists(backup):
        backup = f"{db_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return backup


def run_backfill_actors(uin):
    """离线把互动人 uin 从留档 raw_json 回填进 feeds.actor（不联网、不抓取）。

    写前整库备份：回填要 UPDATE 几千行，出错代价比重抓大。
    """
    db_path = StoreUtil._db_path(uin)
    if not os.path.exists(db_path):
        print(f"未找到库文件 {db_path}")
        return False
    print(f"已备份：{backup_db(db_path)}")

    store = StoreUtil.Store(uin)
    stats = store.backfill_actors()
    store.close()
    print(f"回填互动人：扫描 {stats['scanned']} 行（有留档且 actor 为空），"
          f"填入 {stats['filled']} 行，新增好友 {stats['friends']} 个")
    if stats["scanned"] > stats["filled"]:
        print(f"其中 {stats['scanned'] - stats['filled']} 行的留档里没有 nameCard_<uin>"
              "（多为非互动类事件行），保持原样")
    return True


def run_fix_times(uin):
    """离线补全无年份的时间串（不联网、不抓取）。

    无年份串排序时沉到最底，相册动态就是这么「丢失」的。年份取法见 Tools.repair_yearless_time。
    """
    db_path = StoreUtil._db_path(uin)
    if not os.path.exists(db_path):
        print(f"未找到库文件 {db_path}")
        return False
    print(f"已备份：{backup_db(db_path)}")

    store = StoreUtil.Store(uin)
    stats = store.fix_yearless_times()
    store.close()
    print(f"补全无年份时间：命中 {stats['scanned']} 行，改写 {stats['fixed']} 行")
    if stats["scanned"] > stats["fixed"]:
        print(f"其余 {stats['scanned'] - stats['fixed']} 行既没有留档也没有落库时刻，"
              "补不出年份，保持原样")
    return True


def run_rekey_mobile(uin):
    """离线修好存量 mobile 行：主键归一化 + 用留档重算正文（不联网、不抓取）。

    采集侧老 bug：mobile 的 `comm.feedskey` 是 pc 明文键的十六进制编码，原样入库撞不上 pc
    主键，一条相册导出成「原帖 + 每条点赞各一条」。重算正文是为了去掉老行的
    `［互动·点赞·X］` 前缀（会让聚合「取最长」挑中某个人的版本，看着像他发的）。写前备份。
    """
    db_path = StoreUtil._db_path(uin)
    if not os.path.exists(db_path):
        print(f"未找到库文件 {db_path}")
        return False
    print(f"已备份：{backup_db(db_path)}")

    store = StoreUtil.Store(uin)
    stats = store.rekey_mobile(GetFeeds.decode_feedskey)
    print(f"归一化 mobile 主键：扫描 {stats['scanned']} 行，改写 {stats['moved']} 行，"
          f"并入已有 pc 行 {stats['merged']} 行")
    redone = store.reparse("mobile", derive_mobile, pictures="replace")
    store.close()
    print(f"用留档重算 mobile：扫描 {redone['scanned']} 行，改写 {redone['updated']} 行，"
          f"无变化 {redone['unchanged']} 行")
    return True


def run_export_only(uin, args):
    """只重建导出视图：不抓取，默认也不重下图片。返回是否成功。

    uin 从登录态文件读、昵称取库中正文作者前缀（stored_nickname）——不走 `get_login_user_info`，
    那条路登录态过期会出二维码。

    `--with-pics` 会联网，但只连图片 CDN（qpic/qlogo），仍不校验登录态、不出二维码：图片 URL
    自带凭证，与登录 session 无关。导出目录要能脱离网络打开就得带上它。
    """
    global session, user_nickname
    db_path = StoreUtil._db_path(uin)
    if not os.path.exists(db_path):
        print(f"未找到库文件 {db_path}")
        return False
    store = StoreUtil.Store(uin)
    if store.count() == 0:
        print("库中无数据，跳过导出")
        store.close()
        return True
    # 不走 Login.cookie 的扫码分支：登录态文件在就用（u 只是从 cookie 读个 uin 号段），
    # 不校验登录态有效性——校验即联网，过期就弹码，与「纯离线导出」矛盾。
    # cookies 传 None 会让 QzoneSession 退化为扫码登录（破离线纪律），这里显式给空 dict。
    session = SessionUtil.QzoneSession(cookies=Config.select_saved_login(args.user) or {})
    user_nickname = stored_nickname(store) or session.uin
    print(f"离线导出：用户 <{session.uin}>（{user_nickname}），库中 {store.count()} 条")
    save_data(store, open_result=not args.no_open, skip_pics=not args.with_pics,
              force_pics=args.force_pics)
    store.close()
    return True


def derive_pc(raw_html):
    """从留档的 <li> 片段重算 pc 行（正文/图片走与采集同一套 parse_li）。

    留档多为整条 <li>，个别是外层包装，退化用整个片段找 li。
    """
    soup = BeautifulSoup(raw_html, "html.parser")
    element = soup.find("li", class_="f-single f-s-s") or soup
    row, _friend = parse_li(element, "", raw_html)
    if row is None:
        return None
    _key, time_s, content, img, comments, _raw, _action, _actor = row
    return time_s, content, img, comments


def derive_mobile(raw_json):
    """从留档的 feed dict 重算 mobile 行（正文/图片/评论/动作词/互动人）。

    **不改主键**——老行的十六进制键要先经 `store.rekey_mobile` 归一化（docs/03 §五）。
    """
    parsed = GetFeeds.parse_feed(json.loads(raw_json))
    if not parsed:
        return None
    _key, row, _raw = parsed
    return tuple(row)


def replay_dump_files(store, paths):
    """重放一批留档响应体：parse_batch 一遍，新键插入、老键用重算值覆盖。

    接文件列表而非目录，让测试能直接用历史探针留档而不必先搭目录。限流/空页（体量极小）跳过。
    """
    rows = []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        if len(body) < 2000:
            continue  # 限流 / 空页
        name = os.path.basename(path)
        batch, friends = parse_batch(body, name)
        for friend_name, qq, link in friends:
            store.upsert_friend(qq, friend_name, link)
        rows.extend(batch)
        print(f"  {name}: {len(batch)} 行 / 好友 {len(friends)}")
    return store.replay_rows(rows, "pc")


def replay_dump_dir(store, directory):
    """重放 --dump-raw 留档目录：被旧选择器丢弃的卡片/相册动态由此补回，已存在的键也会
    被覆盖成新的正文/图片（懒加载真图）。"""
    if not os.path.isdir(directory):
        print(f"留档目录不存在：{directory}")
        return None
    files = sorted(os.path.join(directory, f) for f in os.listdir(directory)
                   if f.endswith(".txt"))
    stats = replay_dump_files(store, files)
    print(f"重放留档：新入库 {stats['inserted']} 行，覆盖 {stats['updated']} 行，"
          f"无变化 {stats['unchanged']} 行")
    return stats


def run_offline_rederive(uin, args):
    """--reparse-raw / --replay-dump 的公共入口：整库备份后离线重算，不联网不抓取。"""
    db_path = StoreUtil._db_path(uin)
    if not os.path.exists(db_path):
        print(f"未找到库文件 {db_path}")
        return False
    print(f"已备份：{backup_db(db_path)}")

    store = StoreUtil.Store(uin)
    if args.reparse_raw:
        # mobile 相册去重是「4 档尺寸 → 1 张」的变少修复，fill 判不出来，必须 replace
        derive = derive_pc if args.reparse_raw == "pc" else derive_mobile
        mode = "fill" if args.reparse_raw == "pc" else "replace"
        stats = store.reparse(args.reparse_raw, derive, pictures=mode)
        print(f"重算 {args.reparse_raw}：扫描 {stats['scanned']} 行（有留档），"
              f"改写 {stats['updated']} 行，无变化 {stats['unchanged']} 行")
    if args.replay_dump:
        replay_dump_dir(store, args.replay_dump)
    store.close()
    return True


def parse_args():
    parser = argparse.ArgumentParser(description="QQ空间互动消息/说说抓取导出")
    parser.add_argument("--source", choices=["pc", "mobile", "both"], default="pc",
                        help="互动流来源：pc=feeds2_html_pav_all, mobile=get_feeds, both=两个都抓")
    parser.add_argument("--fresh", action="store_true",
                        help="忽略断点从头抓（默认有断点就续传）")
    parser.add_argument("--user", type=str, default="", metavar="NAME",
                        help="登录态文件名（resource/user/ 下的名字）。指定后不再交互选择用户，"
                             "无 TTY（后台/重定向）也能跑；不指定时维持原来的提问选择")
    parser.add_argument("--retry-skips", action="store_true",
                        help="只重试上次记录的坏页，不跑主循环")
    parser.add_argument("--rate", type=int, default=Resume.DEFAULT_WINDOW_PAGES,
                        help=f"每 {Resume.WINDOW_SECONDS // 60} 分钟最多抓取页数（默认 {Resume.DEFAULT_WINDOW_PAGES}，PC 源还叠加 3.5~4.5s/页间隔）")
    parser.add_argument("--max-batches", type=int, default=None,
                        help="最多抓多少批（试跑/分次抓用，断点保留可续）")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="mobile 源最多翻页数（默认翻到 hasmore=0）")
    parser.add_argument("--no-open", action="store_true",
                        help="导出后不自动打开结果目录")
    parser.add_argument("--deep", action="store_true",
                        help="浅区抓完后追加深区补洞扫描（offset ~20500-21500，2014-08 起的已删说说互动残留）；"
                             "默认关闭，仅 pc/both 源有效")
    parser.add_argument("--deep-rate", type=int, default=Deep.DEEP_WINDOW_PAGES,
                        help=f"深区每 {Resume.WINDOW_SECONDS // 60} 分钟页数上限（默认 {Deep.DEEP_WINDOW_PAGES}；"
                             "深区比浅区更易触发 network busy，故独立且更严）")
    parser.add_argument("--deep-offsets", type=str, default="",
                        help="覆盖深区格表（逗号分隔 offset）；不填用内置 9 个历史命中格")
    parser.add_argument("--deep-only", action="store_true",
                        help="跳过浅区互动流抓取，直接跑深区（隐式开 --deep）。浅区已抓完、"
                             "只想补深带时用——否则浅区会先跑一遍，可能吃掉整段登录态寿命")
    parser.add_argument("--dump-raw", type=str, default="", metavar="DIR",
                        help="把每个 pav_all 响应体原样留档到 DIR（默认关）。解析器改动后可离线重放，"
                             "不必重抓；响应含真实昵称/QQ号，务必落在 .gitignore 的目录（如 resource/temp/dump）")
    parser.add_argument("--maintain", choices=["actors", "mobile", "times",
                                               "reparse-pc", "reparse-mobile"],
                        default=None, metavar="KIND",
                        help="离线维护工具（互斥地执行一项）：actors=从留档 raw_json 回填"
                             "互动人 uin（评论行的 key 里没有互动人）；mobile=归一化存量"
                             "mobile 行的十六进制 feedskey + 重算正文去互动注解；times=补全"
                             "无年份的时间串（本年动态服务端不给年份，这类行排序沉底）;"
                             "reparse-pc / reparse-mobile=用留档重算该源的存量行（卡片/相册"
                             "正文、懒加载真图、相册同图去重）。纯本地不联网不抓取、不出"
                             "二维码，账号号段从 --user 的登录态文件读；写库前自动备份 "
                             ".db.bak-<日期>")
    parser.add_argument("--fix-times", action="store_true",
                        help="（已并入 --maintain times，此旗保留一个过渡期）")
    parser.add_argument("--replay-dump", type=str, default="", metavar="DIR",
                        help="离线重放 --dump-raw 留档的 pc 响应目录：新键插入、老键用重算值覆盖"
                             "（旧选择器丢弃的卡片/相册动态由此补回）。纯本地不联网，写库前备份")
    parser.add_argument("--export-only", action="store_true",
                        help="只用库中现有数据重建 Excel/网页版：不抓取、不下载图片、不校验登录态，"
                             "也就不需要联网（登录态过期也不会弹二维码）")
    parser.add_argument("--with-pics", action="store_true",
                        help="配合 --export-only：把网页版用到的图片、互动人头像与表情补到本地 "
                             "pic/（图片取原图），已有的跳过——重复跑很快。会连图片 CDN，但仍不"
                             "校验登录态、不出二维码。不带它导出的 HTML 会去连远程图链，签名过期"
                             "后就是一片空白")
    parser.add_argument("--force-pics", action="store_true",
                        help="配合 --with-pics：已存在的也重下。从老版本升上来时跑一次，把早先"
                             "存的中图换成原图；之后只带 --with-pics 即可")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.deep_only:
        args.deep = True
    if args.deep and args.source == "mobile":
        print("--deep 仅作用于 pc 互动流（pav_all），--source mobile 下已忽略")
        args.deep = False
    if args.dump_raw:
        Dump.enable(args.dump_raw)
        print(f"原始响应留档已开启：{args.dump_raw}")

    if (args.maintain or args.replay_dump or args.fix_times or args.export_only):
        # 纯离线：放在登录之前，登录态过期/没网也能跑（也不出二维码）
        if not args.user:
            print("离线操作需要 --user 指定登录态文件名（账号号段从该文件里读）")
            sys.exit(2)
        try:
            uin = saved_uin(args.user)
        except FileNotFoundError as e:
            print(e)
            sys.exit(2)
        if not uin:
            print(f"登录态 {args.user} 里读不到 uin，无法定位库文件")
            sys.exit(2)
        if args.fix_times:
            # 旧写法的过渡别名：--fix-times → --maintain times
            args.maintain = "times"
        if args.maintain in ("actors", "mobile", "times"):
            runner = {"actors": run_backfill_actors, "mobile": run_rekey_mobile,
                      "times": run_fix_times}[args.maintain]
            sys.exit(0 if runner(uin) else 2)
        if args.maintain in ("actors", "mobile", "times"):
            runner = {"actors": run_backfill_actors, "mobile": run_rekey_mobile,
                      "times": run_fix_times}[args.maintain]
            sys.exit(0 if runner(uin) else 2)
        reparse = (args.maintain.removeprefix("reparse-")
                   if args.maintain and args.maintain.startswith("reparse-") else None)
        if reparse or args.replay_dump:
            # run_offline_rederive 只读 reparse_raw/replay_dump 两个属性，给个轻量视图即可
            ns = argparse.Namespace(reparse_raw=reparse, replay_dump=args.replay_dump)
            sys.exit(0 if run_offline_rederive(uin, ns) else 2)
        sys.exit(0 if run_export_only(uin, args) else 2)

    try:
        session = SessionUtil.get_session(user_file=args.user)
    except FileNotFoundError as e:
        # --user 写错不该静默落到扫码分支：直接退出，列表里能看见正确名字
        print(e)
        sys.exit(2)
    try:
        user_info = Request.get_login_user_info(session)
    except Exception as e:
        # 已存登录态失效就出二维码重登（无 TTY 也能走完），不再让用户先去跑别的脚本
        print(f"已存登录态不可用（{type(e).__name__}: {e}），改走扫码登录")
        try:
            session = SessionUtil.get_session(user_file=args.user, force_new=True, force_qr=True)
            user_info = Request.get_login_user_info(session)
        except Exception as e2:
            print(f"登录失败:请重新登录,错误信息:{str(e2)}")
            sys.exit(0)
    user_nickname = user_info[session.uin][6]
    print(f"用户<{session.uin}>,<{user_nickname}>登录成功")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 全局 resumable = PC 流实例（signal_handler / parse_batch 用）；taotao/mobile 各用自己的 prefix
    resumable = Resume.ResumeUtil(session.uin, prefix="pc_", rate_limit=args.rate)
    store = StoreUtil.Store(session.uin)
    print(f"数据库：{store.conn.execute('PRAGMA database_list').fetchone()[2]}"
          f"（现有 {store.count()} 条 / 好友 {len(store.load_friends())} 个）")

    if args.retry_skips:
        # 只补坏页：不跑任何抓取主循环，重试完直接按库中现有数据导出
        retry_skips(store)
        if store.count() > 0:
            save_data(store, open_result=not args.no_open)
        else:
            print("库中无数据，跳过导出")
        sys.exit(0)

    if args.export_only:
        # 不会走到这里：--export-only 已在登录之前离线处理并退出（见 __main__ 开头）
        sys.exit(0)

    try:
        if args.source in ("pc", "both"):
            if args.deep_only:
                print("--deep-only：跳过浅区互动流抓取，直接进深区")
            else:
                fetch_pc_interactions(args, store)
            if args.deep:
                # 浅区抓完再进深区：深区是稀疏 offset 扫描，与浅区的二分总量假设无关
                fetch_pc_deep(args, store)
        if args.source in ("mobile", "both"):
            fetch_mobile_source(args, store)
    except Resume.FatalFetchError as e:
        print(f"不可恢复错误，中止: {e}")

    try:
        taotao_resumable = Resume.ResumeUtil(session.uin, prefix="taotao_",
                                             rate_limit=args.rate)
        user_moments = GetAllMoments.get_visible_moments_list(
            session, resumable=taotao_resumable, fresh=args.fresh, store=store)
        # 可见说说已在 get_visible_moments_list 内落库（taotao），剔除互动流里的重复内容。
        # DELETE 不可逆：先整库备份（与离线维护命令同一约束）。
        if user_moments:
            print(f"已备份：{backup_db(store.path)}")
            store.remove_rows_matching(user_moments)
    except Exception as err:
        print(f"获取未删除QQ空间记录发生异常: {str(err)}")

    if store.count() > 0:
        save_data(store, open_result=not args.no_open)
    else:
        print("库中无数据，跳过导出")


if __name__ == "__main__":
    main()
