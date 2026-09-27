import shutil
from datetime import datetime
import argparse
import json
import subprocess
from bs4 import BeautifulSoup
import util.RequestUtil as Request
import util.ResumeUtil as Resume
import util.SessionUtil as SessionUtil
import util.StoreUtil as StoreUtilMod
import util.AggregateUtil as Aggregate
import util.DeepUtil as Deep
import util.DumpUtil as Dump
import util.ToolsUtil as Tools
import util.ConfigUtil as Config
import util.GetAllMomentsUtil as GetAllMoments
import pandas as pd
import signal
import os
import re
from tqdm import trange, tqdm
import requests
import time
import platform
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
session = None
user_nickname = None
resumable = None  # ResumeUtil 实例（PC 流），signal_handler / parse_batch 用
store = None      # StoreUtil 实例（B 线），signal_handler 导出用
user_message = list()
leave_message = list()
forward_message = list()
other_message = list()


# 信号处理函数
def signal_handler(sig, frame):
    # 手动结束时数据已逐批落库/落 jsonl，这里直接从库导出
    print("\n收到中断信号，正在导出已抓数据...")
    if store is not None and store.count() > 0:
        save_data(store, open_result=False)
    exit(0)


def content_author(content):
    """取内容里的作者昵称。

    内容形态有两种，作者都在「名字 + 连续空白」的开头：
        "昵称 ： 正文"        （普通说说 / 互动事件）
        "昵称  转发： 正文"    （转发，冒号前多了动作词）
        "昵称    发表说说"     （无正文的通用事件行）
    旧逻辑用 `user_nickname in 整条内容` 子串匹配，好友的说说正文里提到本人昵称
    就会被误归到「我的说说」；改为只认开头这段名字。
    """
    s = (content or "").strip()
    m = re.match(r"^(.*?)\s{2,}", s)
    if m:
        return m.group(1).strip()
    return s.split("：", 1)[0].strip()


def parse_batch(message, pos):
    """解析一批 PC 互动流响应。返回 (batch, friends)：

    batch: [(feed_key, time, content, img, comments, raw, action), ...]（含 key 空的条目，
           落库时 StoreUtil 用 hash 兜底）；去重由库 INSERT OR IGNORE 兜底，
           这里不再按内容去重（真实 key 比分钟级时间键更准）。
    friends: [name, qq, link] 好友（本批出现的全部，含已见过的）。
    """
    reason = Resume.classify_body(message)  # 登录态/WAF/限流检查（体级错误 get_message 已查，这里兜底）
    if reason in Resume.FATAL_REASONS:
        raise Resume.FatalFetchError(f"批次 {pos}: {reason}")
    if reason == "throttled":
        # 软限流 ≠ 空页：单独标注（可重试），别混进 "parse/无 html 字段"，
        # 否则诊断时看不出这一页是无内容还是被限流。
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
    # 真实响应里每个 item 恰一条 li、一一对应（存量样本 10/10 验证）；
    # 只有部分 li 可解析出时间+文本（其余为纯事件/折叠头），按 li 索引回查 key
    lis = soup.find_all("li", class_="f-single f-s-s")
    if len(feed_keys) != len(lis):
        print(f"[WARN] 批次 {pos}: li({len(lis)}) 与 item({len(feed_keys)}) 数不一致，key 可能错位")
    for idx, element in enumerate(lis):
        friend_element = element.find("a", class_="f-name q_namecard")
        if friend_element is not None:
            friend_name = friend_element.get_text()
            friend_qq = friend_element.get("link")[9:]
            friend_link = friend_element.get("href")
            friends.append([friend_name, friend_qq, friend_link])

        # 动作词（E3）：div.f-nick 里紧跟互动人昵称的 <span class="ui-mr10 state">，
        # 取值如「赞了」「赞了我的说说」「评论」。A 态解析时被丢弃且未落库，
        # 库中存量行为空；重抓时 StoreUtil 会按 feed_key 回填。
        state_element = element.select_one("span.ui-mr10.state")
        action = state_element.get_text().strip() if state_element is not None else ""

        time_element = element.find("div", class_="info-detail")
        text_element = element.find("p", class_="txt-box-title ellipsis-one")
        img_element = element.find("a", class_="img-item")

        if time_element is None or text_element is None:
            continue
        put_time = time_element.get_text().replace("\xa0", " ").strip()
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

        img = None
        if img_element is not None:
            img = img_element.find("img").get("src")
            if img:
                img = img.replace("\\/", "/").replace("&amp;", "&").strip()

        if clean_text:
            key = feed_keys[idx] if idx < len(feed_keys) else ""
            # raw 槽位放该 item 自己的 html 片段（不是整批，不重复 10 倍）。动作词(E3)与
            # 互动人昵称都在里面——留档后改解析器可离线重算，不必为补列重抓全量。
            # 库里存量行的 raw 为空（当年写死 ""），重抓时由 StoreUtil 回填。
            raw_html = items[idx][1] if idx < len(items) else ""
            batch.append([key, put_time, clean_text, img, [], raw_html, action])
    return batch, friends


def fetch_pc_interactions(args, store):
    """流 A：PC 互动流，C1 断点 + C2 跳过 + C3 速率窗口，B 线落库。"""
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
        print("断点已到末尾，无需继续抓取")
        return

    fetched = 0
    try:
        for i in trange(start_i, total_batches, initial=start_i, total=total_batches,
                        desc="Progress", unit="10条"):
            if args.max_batches and fetched >= args.max_batches:
                print(f"已达到本批上限 {args.max_batches} 批，中断（断点已保存，可直接重跑续传）")
                return
            offset = i * 10
            try:
                response = Request.get_message(session, offset, 10, rate_limiter=resumable)
            except Resume.FatalFetchError as e:
                raise
            if response is None or not hasattr(response, "content"):
                if resumable is not None:
                    resumable.record_skip(offset, "timeout", "响应为空或超时")
                print(f"获取消息失败：offset {offset}，已记入 skips")
                continue
            message = response.content.decode("utf-8", errors="replace")
            try:
                batch, friends = parse_batch(message, offset)
            except Resume.FatalFetchError:
                raise
            except Exception as e:
                if resumable is not None:
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

    parse_batch 通过模块全局 resumable 记 skip，故这里把全局临时换成 pcdeep_ 实例——
    深区的"无 html 字段"噪声才不会污染浅区 pc_ 的 skips（否则 retry_skips 会拿
    深区 offset 反复白跑）。"""
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
    import util.GetFeedsUtil as GetFeeds
    mobile_resumable = Resume.ResumeUtil(session.uin, prefix="mobile_",
                                         rate_limit=args.rate)
    rows, pages = GetFeeds.fetch_all_feeds(
        session, max_pages=args.max_pages, rate_limiter=mobile_resumable,
        resumable=mobile_resumable, fresh=args.fresh, store=store)
    print(f"get_feeds 共 {pages} 页，解析 {len(rows)} 条"
          f"（库中 mobile 现有 {store.count('mobile')} 条）")


# 还原QQ空间网页版说说
def render_html(posts, output_file):
    """把「原动态」列表渲染成网页版。

    posts: [{"time","content","pictures","interactors":[{name,qq,time}],"comments":[...]}]
    互动人复用评论模板（头像+昵称+时间）挂在原动态下方——即「谁赞了/评论了」。
    旧版从 Excel 读回并 `split("：")`，无冒号的行被静默丢弃、正文里第二个冒号后的
    内容被截掉；现在直接用结构化数据，两个问题都没了。
    """
    avatar_url = (
        f"https://q.qlogo.cn/headimg_dl?dst_uin={session.uin}&spec=640&img_type=jpg"
    )
    html_template, post_template, comment_template = Tools.get_html_template()

    def em(text):
        return re.sub(r"\[em\](.*?)\[/em\]", Tools.replace_em_to_img, str(text))

    def avatar(uin):
        return f"https://q.qlogo.cn/headimg_dl?dst_uin={uin or session.uin}&spec=640&img_type=jpg"

    post_html = ""
    for p in posts:
        time_str = str(p["time"] or "").strip()
        if not time_str:
            continue
        author, sep, body = str(p["content"] or "").partition("：")
        nickname = em(author.strip() or user_nickname)
        message = em(body.strip() if sep else "")

        image_html = '<div class="image">'
        for img_url in [
            url for url in str(p["pictures"] or "").split(",") if url.startswith("http")
        ]:
            img_url = img_url.replace("/m&ek=1&kp=1", "/s&ek=1&kp=1").replace(
                r"!/m/", "!/s/"
            )
            image_html += f'<img src="{img_url}" alt="图片">\n'
        image_html += "</div>"

        comment_html = ""
        for a in p.get("interactors", []):
            comment_html += comment_template.format(
                avatar_url=avatar(a.get("qq")),
                nickname=em(a["name"] or a["qq"] or "?"),
                time=a["time"],
                message=em(a.get("action") or ""),
            )
        for c in p.get("comments", []):
            if len(c) >= 4:
                c_time, c_content, c_nickname, c_uin = c
                comment_html += comment_template.format(
                    avatar_url=avatar(c_uin),
                    nickname=em(c_nickname),
                    time=c_time,
                    message=em(c_content),
                )

        post_html += post_template.format(
            avatar_url=avatar_url,
            nickname=nickname,
            time=time_str,
            message=message,
            image=image_html,
            comments=comment_html,
        )

    final_html = html_template.format(posts=post_html)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_html)


def build_posts(store):
    """库 → 导出用「原动态」列表（E1 聚合）。

    pc 事件流按原动态聚合——一条说说被 N 人互动只出一行，互动人挂在下面，
    不再因互动时间不同而重复刷屏；mobile/taotao 本身已是「一条原动态一行」，原样并入。
    """
    posts = []
    pc_rows = store.load_rows(columns="feed_key, time, content, pictures, action", source="pc")
    for o in Aggregate.aggregate(pc_rows, store.load_friends()):
        posts.append({
            "time": o["first_time"],
            "content": o["content"],
            "pictures": o["pictures"],
            "interactors": o["actors"],
            "comments": [],
        })
    for src in ("mobile", "taotao"):
        for t, c, p, cm in store.load_rows(
                columns="time, content, pictures, comments", source=src):
            posts.append({"time": t, "content": c, "pictures": p,
                          "interactors": [], "comments": cm or []})
    posts.sort(key=lambda x: Tools.safe_strptime(str(x["time"])) or datetime.min,
               reverse=True)
    return posts


# 保存数据（B 线后：从 SQLite 读，Excel/HTML 只是导出视图；E1 后 pc 流按原动态聚合）
EXPORT_COLUMNS = ["时间", "内容", "图片链接", "评论", "互动人"]


def posts_to_rows(posts):
    """导出用的行：评论存 JSON 串，互动人按动作词分组拼昵称（无互动则空）。"""
    return [
        [
            p["time"],
            p["content"],
            p["pictures"],
            json.dumps(p["comments"], ensure_ascii=False) if p["comments"] else "",
            Aggregate.format_actors_by_action(p["interactors"]),
        ]
        for p in posts
    ]


def save_data(store, open_result=True):
    user_save_path = Config.result_path + session.uin + "/"
    pic_save_path = user_save_path + "pic/"
    if not os.path.exists(user_save_path):
        os.makedirs(user_save_path)
        print(f"Created directory: {user_save_path}")
    if not os.path.exists(pic_save_path):
        os.makedirs(pic_save_path)
        print(f"Created directory: {pic_save_path}")

    posts = build_posts(store)
    all_friends = store.load_friends()

    # 分类按「内容里的作者昵称」，不再用 `user_nickname in 整条内容` 子串匹配
    # （好友的说说只要正文提到本人昵称就会被误归）。清空是必须的：这些模块级列表
    # 在 signal_handler 导出后仍会保留，第二次导出会重复累加。
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

    def export(rows, filename):
        pd.DataFrame(posts_to_rows(rows), columns=columns).to_excel(
            user_save_path + filename, index=False
        )

    export(posts, session.uin + "_全部列表.xlsx")
    pd.DataFrame(all_friends, columns=["昵称", "QQ", "空间主页"]).to_excel(
        user_save_path + session.uin + "_好友列表.xlsx", index=False
    )
    export(user_message, session.uin + "_说说列表.xlsx")
    export(forward_message, session.uin + "_转发列表.xlsx")
    export(leave_message, session.uin + "_留言列表.xlsx")
    export(other_message, session.uin + "_其他列表.xlsx")

    render_html(
        user_message + forward_message,
        os.path.join(os.getcwd(), user_save_path, session.uin + "_说说网页版.html"),
    )

    for p in tqdm(posts, desc="处理消息列表", unit="item"):
        item_text = p["content"] or ""
        # 原动态可能有多张图片
        for item_pic_link in str(p["pictures"] or "").split(","):
            # 如果图片链接为空或者不是http链接，则跳过
            if not item_pic_link or "http" not in item_pic_link:
                continue
            # 去除非法字符 / Emoji表情，限制文件名长度
            pic_name = (
                re.sub(
                    r'\[em\].*?\[/em\]|[^\w\s]|[\\/:*?"<>|\r\n]+', "_", item_text
                ).replace(" ", "")
                + ".jpg"
            )
            if len(pic_name) > 40:
                pic_name = pic_name[:40] + ".jpg"
            item_pic_link = (
                item_pic_link.replace("\\/", "/").replace("&amp;", "&").strip()
            )
            try:
                response = requests.get(item_pic_link, timeout=(10, 20))
            except Exception:
                continue

            if response.status_code == 200:
                # 防止图片重名
                if os.path.exists(pic_save_path + pic_name):
                    pic_name = (
                        pic_name.split(".")[0] + "_" + str(int(time.time())) + ".jpg"
                    )
                with open(pic_save_path + pic_name, "wb") as f:
                    f.write(response.content)

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
    if posts:
        print("\033[36m" + "最早的一条说说发布在" + str(posts[-1]["time"]) + "\033[0m")
    print("\033[32m" + "好友列表共有 " + str(len(all_friends)) + " 个好友" + "\033[0m")
    print("\033[36m" + "说说列表共有 " + str(len(user_message)) + " 条说说" + "\033[0m")
    print("\033[32m" + "转发列表共有 " + str(len(forward_message)) + " 条转发" + "\033[0m")
    print("\033[36m" + "留言列表共有 " + str(len(leave_message)) + " 条留言" + "\033[0m")
    print("\033[32m" + "其他列表共有 " + str(len(other_message)) + " 条内容" + "\033[0m")
    print("\033[36m" + "图片列表共有 " + str(len(os.listdir(pic_save_path))) + " 张图片" + "\033[0m")
    if open_result:
        open_file(os.getcwd() + user_save_path[1:])
    if platform.system() == "Windows":
        os.system("pause")
    else:
        os.system("stty raw -echo;dd bs=1 count=1 >/dev/null 2>&1;stty cooked echo")


# 打开文件展示
def open_file(file_path):
    # 检查操作系统
    if platform.system() == "Windows":
        # Windows 系统使用 os.startfile
        os.startfile(file_path)
    elif platform.system() == "Darwin":
        # macOS 系统使用 subprocess 和 open 命令
        subprocess.run(["open", file_path])
    elif platform.system() == "Linux":
        # Linux 系统，首先检查是否存在 xdg-open 工具
        if shutil.which("xdg-open"):
            subprocess.run(["xdg-open", file_path])
        # 如果 xdg-open 不存在，检查是否存在 gnome-open 工具（适用于 GNOME 桌面环境）
        elif shutil.which("gnome-open"):
            subprocess.run(["gnome-open", file_path])
        # 如果 gnome-open 不存在，检查是否存在 kde-open 工具（适用于 KDE 桌面环境）
        elif shutil.which("kde-open"):
            subprocess.run(["kde-open", file_path])
        # 如果以上工具都不存在，提示用户手动打开文件
        else:
            print("未找到可用的打开命令，请手动打开文件。")
    else:
        print(f"Unsupported OS: {platform.system()}")


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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.deep_only:
        args.deep = True
    if args.deep and args.source == "mobile":
        print("--deep 仅作用于 pc 互动流（pav_all），--source mobile 下已忽略")
        args.deep = False
    if args.dump_raw:
        Dump.enable(args.dump_raw)
        print(f"原始响应留档已开启：{args.dump_raw}")

    try:
        session = SessionUtil.get_session(user_file=args.user)
    except FileNotFoundError as e:
        # --user 写错不该静默落到扫码分支：直接退出，列表里能看见正确名字
        print(e)
        exit(2)
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
            exit(0)
    user_nickname = user_info[session.uin][6]
    print(f"用户<{session.uin}>,<{user_nickname}>登录成功")

    # 注册信号处理函数
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 全局 resumable = PC 流实例，供 signal_handler / parse_batch 使用；
    # taotao/mobile 流在各自入口用自己的 prefix 实例化。
    resumable = Resume.ResumeUtil(session.uin, prefix="pc_", rate_limit=args.rate)
    store = StoreUtilMod.Store(session.uin)
    print(f"数据库：{store.conn.execute('PRAGMA database_list').fetchone()[2]}"
          f"（现有 {store.count()} 条 / 好友 {len(store.load_friends())} 个）")

    if args.retry_skips:
        # 只补坏页：不跑任何抓取主循环，重试完直接按库中现有数据导出
        retry_skips(store)
        if store.count() > 0:
            save_data(store, open_result=not args.no_open)
        else:
            print("库中无数据，跳过导出")
        exit(0)

    try:
        if args.source in ("pc", "both"):
            if args.deep_only:
                print("--deep-only：跳过浅区互动流抓取，直接进深区")
            else:
                fetch_pc_interactions(args, store)
            if args.deep:
                # 浅区抓完（或已在断点末尾早退）再进深区：深区是稀疏 offset 扫描，
                # 与浅区的"二分总量 + set0 连续翻页"假设无关（docs/03 §四）。
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
        # 可见说说已在 get_visible_moments_list 内落库（source=taotao），
        # 互动流里与之重复的内容（说说自己出现在互动流）从库中剔除，导出不含冗余
        if user_moments:
            store.remove_rows_matching(user_moments)
    except Exception as err:
        print(f"获取未删除QQ空间记录发生异常: {str(err)}")

    if store.count() > 0:
        save_data(store, open_result=not args.no_open)
    else:
        print("库中无数据，跳过导出")
