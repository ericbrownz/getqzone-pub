"""mobile get_feeds 源（D 线备选/补充流）。

字段路径（2026-09-05/06 实测 dump 确认，见 docs/02 Step 0 结论）：
    正文    original.cell_summary.summary 或 original.cell_title.title（以 "：" 开头含昵称）
    作者    original.cell_userinfo.user.{nickname,uin}（原帖/相册**主人**，不是互动人）
    互动人  feed.userinfo.user.{uin,nickname}（评论人 / 点赞人）
    评论    original.cell_comment.main_comment(dict) + comments[]（date 是 unix 秒）
    图片    original.cell_pic.picdata.pic[].photourl（dict，**同一张图的各档尺寸**）
    唯一键  comm.feedskey（十六进制编码的 pc 明文键，见 decode_feedskey）
    游标    data.attachinfo + data.hasmore
    事件    comm.subid：217 点赞聚合 / 2 评论聚合 / 1 评论 / 27 其他

这条流**只回事件、不回原帖正文**：评论事件里的 `feed.summary` 是**评论人的话**，套上原帖
作者昵称就成了「本人发的说说」。故互动事件的正文用可离线还原的事件信息（标题 + 落地链接），
互动人走 actor 列、渲染进点赞/评论框（docs/03 §五）。

同一句 `feed.summary` 也**正是评论正文**，落进评论列（event_kind 为 comment 且 cell_comment
里认不出本人时）——它是本人说的那句话，只是不能当原帖正文用。pc 流没有 cell_comment，
不补这一句，渲染时只剩动作词「评论」。
"""
import binascii
import json
import random
import time

import requests

import util.ResumeUtil as Resume
import util.ToolsUtil as Tools

BASE_URL = 'https://mobile.qzone.qq.com/get_feeds'


def build_params(g_tk, cursor, first_page):
    params = {
        'g_tk': g_tk,
        'res_type': 1,
        'format': 'json',
    }
    params['refresh_type'] = 1 if first_page else 2
    if not first_page and cursor is not None:
        params['res_attach'] = cursor
    return params


def get_feed_page(session, cursor=None, first_page=True, rate_limiter=None):
    """抓一页 mobile feeds，返回 data dict；可恢复错误（超时/5xx）返回 None，
    登录态失效抛 Resume.FatalFetchError。"""
    if rate_limiter is not None:
        rate_limiter.wait_for_slot()
    time.sleep(random.uniform(3.5, 4.5))
    try:
        resp = requests.get(
            BASE_URL,
            params=build_params(session.g_tk, cursor, first_page),
            cookies=session.cookies,
            headers=session.mobile_headers,
            timeout=(5, 15),
        )
    except requests.Timeout:
        print('请求超时')
        return None
    text = resp.text.strip()
    reason = Resume.classify_error(resp.status_code, text[:2000])
    if reason in Resume.FATAL_REASONS:
        raise Resume.FatalFetchError(f'mobile get_feeds HTTP {resp.status_code}: {reason}')
    if reason is not None:
        return None
    data = json.loads(text)
    code = data.get('code', data.get('ret'))
    if code not in (0, None):
        if code == -3000:
            raise Resume.FatalFetchError(f'get_feeds code={code}: {data.get("message")}')
        print(f'get_feeds code={code}: {data.get("message")}')
        return None
    return data.get('data') or {}


def _pic_url_rank(url):
    """给候选图 URL 打分：先「能否直连」，再「尺寸大小」。

    `r.photo.store.qq.com` 的 `/o`、`/r` 会 302 跳到 photon 域（https→http 降级）→ 浏览器裂图；
    `m.qpic.cn` 的 `/b`(大)、`/m`(中) 能直取。故直连权重高于尺寸，`/b` 高于 `/m`。
    """
    direct = 0 if "r.photo.store.qq.com" in url else 1
    tail = url.split("!/")[-1][:1] if "!/" in url else ""
    return (direct, {"b": 3, "o": 2, "r": 2, "m": 1, "s": 1}.get(tail, 0))


def pick_pic_url(pic):
    """从一张 pic 记录取**一个**可直接显示的 URL。

    `photourl` 是按尺寸键的 dict（14/11/1/0），键之间是**同一张照片**的不同分辨率、不是多张
    照片——旧实现四档全塞进 pictures，一条相册事件就渲染出 4 张一样的图。按 `_pic_url_rank`
    取分最高的一档（而非键号最大）。
    """
    ph = pic.get('photourl')
    if isinstance(ph, dict):
        best = None
        for v in ph.values():
            url = v.get('url') if isinstance(v, dict) else v
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            rank = _pic_url_rank(url)
            if best is None or rank > best[0]:
                best = (rank, url)
        if best:
            return best[1]
    url1 = pic.get('url1')
    return url1 if isinstance(url1, str) and url1 else None


def event_kind(feed):
    """事件类型：`comm.subid` 217=点赞聚合 / 2=评论聚合 / 1=评论 / 27=其他。

    subid 缺失或非数字退化为 "other"——仍走 original 正文，不会误标。
    """
    try:
        subid = int((feed.get('comm') or {}).get('subid'))
    except (TypeError, ValueError):
        return 'other'
    if subid in (1, 2):
        return 'comment'
    if subid == 217:
        return 'like'
    return 'other'


def _is_plain_feed_key(text):
    """解码结果得**像是 pc 的键**才认：`{uin}_{uin}_{原动态id}` 或 `{uin}&{postid}&{commentid}`。

    只查「含 `_` 或 `&`」不够——pc 自己的三段键第三段常是纯十六进制（如 `123456789_987654321_655f5506`，
    示例号非真实），一律 unhexlify 会解出含下划线的乱码把 pc 键改坏。按段数 + 数字段校验才不误伤。
    """
    parts = text.split("_")
    if len(parts) == 3:
        return parts[0].isdigit() and parts[1].isdigit() and bool(parts[2])
    parts = text.split("&")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


def decode_feedskey(feedskey):
    """`comm.feedskey` 归一化成 pc 明文键。

    mobile 的键是「事件类型 + pc 那个键的十六进制编码」：`217_3_<hex>`（点赞聚合）/ `202_1_<hex>`（评论）；
    `<hex>` 解出来**正是 pc 用的键**。不还原就撞不上 pc 的 feed_key 主键（`pav_all` 与 `get_feeds` 返回
    同一批互动），一条互动落成两行，同一条相册被导成「原帖 + 每条点赞各一条」（docs/03 §五）。

    只解第三段，且必须过 `_is_plain_feed_key` 形状校验才认，否则原样返回。
    """
    parts = (feedskey or "").split("_")
    if len(parts) != 3:
        return feedskey
    try:
        plain = binascii.unhexlify(parts[2]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return feedskey
    return plain if _is_plain_feed_key(plain) else feedskey


def event_actor(feed):
    """互动人 `(uin, 昵称)`：取 `feed.userinfo.user`。

    `original.cell_userinfo` 是原帖/相册**主人**，不是互动人，别混用。
    """
    user = (feed.get("userinfo") or {}).get("user") or {}
    return (user.get("uin") or "").strip(), (user.get("nickname") or "").strip()


# 事件类型 → 动作词（与 pc 流的取值一致，聚合/导出才能按同一套词分组）
EVENT_ACTIONS = {"like": "赞了", "comment": "评论"}


def event_body(feed, original, kind):
    """互动事件的正文：原帖全文不在事件里，用可离线还原的信息代替。

    评论事件的原帖是转发/分享卡片，标题在 `feed.cell_left_thumb`；点赞事件的对象标题
    在 `original.cell_title.title`、张数在 `cell_remark.remark`；落地链接取
    `original.cell_comm.orglikekey`。

    **只给正文**，不拼 `［互动·点赞·X］` 前缀——键已归一化、与 pc 一起聚合，动作/互动人走
    action/actor 两列，前缀反而会把同一条相册拆成「一帖一人」。
    """
    url = Tools.share_url((original.get("cell_comm") or {}).get("orglikekey"))
    if kind == "comment":
        card = feed.get("cell_left_thumb") or {}
        label = "·".join(
            p for p in ((card.get("title") or "").strip(),
                        (card.get("summary") or "").strip()) if p
        )
        return f'转发自「{label or "分享卡片"}」{url}'.strip()
    title = ((original.get("cell_title") or {}).get("title") or "").strip()
    remark = ((original.get("cell_remark") or {}).get("remark") or "").strip()
    detail = f"{title}（{remark}）" if title and remark else (title or remark)
    return f"{detail} {url}".strip()


def parse_feed(feed):
    """一条 feed → (feed_key, 行, raw_json)；解析不出正文返回 None（跳过）。

    行 = [时间, 内容, 图片链接, 评论, 动作词, 互动人uin]（前四列与流 A/流 B 一致，后两列
    供聚合区分点赞/评论并渲染互动人）。feed_key 是归一化后的 pc 明文键（decode_feedskey），
    这样 pc/mobile 的同一条互动落到同一主键、只留一行。

    作者取 `original.cell_userinfo`（原帖/相册**主人**），故内容前缀是本人；互动事件
    **不把 `feed.summary` 当原帖正文**——那是评论人的话，套上本人昵称就成了「我发的说说」
    （docs/03 §五）。
    """
    original = feed.get('original') or {}
    user = (original.get('cell_userinfo') or {}).get('user') or {}
    nickname = user.get('nickname') or (feed.get('userinfo') or {}).get('nickname') or ''
    kind = event_kind(feed)

    if kind == 'other':
        # 只有「其他」类才拿事件自己的 summary 兜底（评论/点赞事件里那是互动人的话，见 docstring）
        body = None
        summary = original.get('cell_summary') or {}
        if isinstance(summary, dict) and summary.get('summary'):
            body = summary['summary']
        else:
            title = original.get('cell_title') or {}
            if isinstance(title, dict) and title.get('title'):
                body = title['title']
        if body is None:
            top_summary = feed.get('summary') or {}
            body = top_summary.get('summary') if isinstance(top_summary, dict) else None
    else:
        body = event_body(feed, original, kind)
    if not body:
        return None

    feed_key = decode_feedskey((feed.get('comm') or {}).get('feedskey'))

    ts = (feed.get('comm') or {}).get('time')
    put_time = None
    if ts:
        try:
            put_time = time.strftime('%Y年%m月%d日 %H:%M:%S', time.localtime(int(ts)))
        except (ValueError, TypeError, OverflowError):
            put_time = None

    pictures = []
    picdata = ((original.get('cell_pic') or {}).get('picdata') or {}).get('pic')
    if isinstance(picdata, list):
        for pic in picdata:
            url = pick_pic_url(pic)
            if url:
                pictures.append(url)

    comments = []
    cc = original.get('cell_comment') or {}
    main_comment = cc.get('main_comment')
    if isinstance(main_comment, dict) and main_comment.get('content'):
        comments.append(main_comment)
    for c in cc.get('comments') or []:
        if isinstance(c, dict) and c.get('content'):
            comments.append(c)
    comment_rows = []
    for c in comments:
        cu = c.get('user') or {}
        c_time = None
        if c.get('date'):
            try:
                c_time = time.strftime('%Y年%m月%d日 %H:%M:%S', time.localtime(int(c['date'])))
            except (ValueError, TypeError, OverflowError):
                c_time = None
        # uin 必须转字符串：mobile 接口给的是 int，而互动人的 uin 全程是 str。
        # 类型不一致会让「评论正文并进互动小框」按 uin 匹配永远落空（正文丢成动作词）。
        c_uin = cu.get('uin')
        comment_rows.append([c_time, c.get('content'), cu.get('nickname'),
                             str(c_uin) if c_uin else None])

    action = EVENT_ACTIONS.get(kind)
    actor_uin, actor_name = event_actor(feed)
    # 评论事件自己的话在 `feed.summary.summary`（docstring 里那条「不当原帖正文」的字段）。
    # 它不是原帖正文，却正是**事件人自己说的那句**——pc 流没有 cell_comment 时只能靠它，
    # 否则渲染时拿动作词「评论」顶替。
    # 已能按 uin 在 cell_comment 里找到本人时说明这句是重复的（同一句在两边都出现），
    # 丢掉；找不到才补——认的是**事件人**，所以原作者回帖也归他自己，不会张冠李戴。
    if kind == "comment" and actor_uin:
        said = (feed.get('summary') or {}).get('summary')
        known = {str((c.get('user') or {}).get('uin') or "") for c in comments}
        if said and str(actor_uin) not in known:
            comment_rows.append([put_time, said, actor_name or None, str(actor_uin)])
    return feed_key, [put_time, f'{nickname} ：{body.strip()}', ','.join(pictures),
                      comment_rows, action, actor_uin or None], feed


def fetch_all_feeds(session, max_pages=None, rate_limiter=None, on_page=None,
                    resumable=None, fresh=False, store=None):
    """游标翻页抓全量。on_page(feeds, page_no) 每页回调。返回 (rows, pages)。

    断点续传（C1，参照 QzoneArchive advance_feed_cursor）：游标 attachinfo 自包含可序列化，
    存进 checkpoint 即可中断后带回续翻。每页先 append_texts 落盘再 save_checkpoint，崩溃窗口
    最多重复一页、按 comm.feedskey 去重兜底；跑完（hasmore=0）清断点。

    store 不为 None 时逐页落库（source="mobile"，归一化 feed_key 幂等），互动人顺带进 friends
    表（mobile 独有的互动人 pc 未必见过，不进表渲染只有 "?"）。rows 为 parse_feed 的六列行。
    """
    rows = []
    cursor = None
    page = 0
    seen_keys = set()
    finished = False
    if resumable is not None:
        if fresh:
            resumable.clear_checkpoint()
        else:
            page = resumable.checkpoint_pos()
            cursor = resumable.checkpoint_extra().get('cursor')
            if page > 0 and cursor:
                rows = resumable.load_texts()
                seen_keys = {r[4] for r in rows if len(r) > 4 and r[4]}
                print(f'从断点第 {page} 页之后继续（游标已恢复，已有 {len(rows)} 条）')
            else:
                page, cursor = 0, None
    while True:
        page += 1
        data = get_feed_page(session, cursor=cursor, first_page=(page == 1),
                             rate_limiter=rate_limiter)
        if data is None:
            break
        feeds = data.get('vFeeds') or []
        if on_page:
            on_page(feeds, page)
        page_rows = []
        for feed in feeds:
            parsed = parse_feed(feed)
            if parsed:
                key, row, raw = parsed
                page_rows.append((key, row, raw))
        if resumable is not None:
            new_rows = [p for p in page_rows if p[0] and p[0] not in seen_keys]
            for p in page_rows:
                if p[0]:
                    seen_keys.add(p[0])
        else:
            new_rows = page_rows
        if new_rows:
            rows.extend(p[1] for p in new_rows)
            if resumable is not None:
                # 断点文件存「四列 + feed_key」（去重快照，load_texts 取 r[4]）；动作词/互动人不入，重算便宜。
                resumable.append_texts([p[1][:4] + [p[0]] for p in new_rows])
            if store is not None:
                store.upsert_feeds(
                    [(p[0], p[1][0], p[1][1], p[1][2], p[1][3],
                      json.dumps(p[2], ensure_ascii=False), p[1][4], p[1][5])
                     for p in new_rows],
                    source='mobile')
                for p in new_rows:
                    uin, name = event_actor(p[2])
                    if uin:
                        store.upsert_friend(uin, name, '')
        hasmore = int(data.get('hasmore') or 0)
        cursor = data.get('attachinfo')
        if isinstance(cursor, dict):
            cursor = json.dumps(cursor, ensure_ascii=False)
        if resumable is not None:
            resumable.save_checkpoint(page, extra={'cursor': cursor})
        if not hasmore:
            finished = True
            break
        if not feeds or (max_pages and page >= max_pages):
            break
        time.sleep(random.uniform(0.5, 1.5))
    if resumable is not None:
        if finished:
            resumable.clear_checkpoint()  # 全量完成，断点使命结束
        elif data is None:
            print('本页请求失败（超时/可恢复错误），断点已保留，重跑续传')
    return rows, page
