"""mobile get_feeds 源（D 线备选/补充流）。

字段路径均为 2026-09-05/06 实测 dump 确认（见 docs/02 Step 0 结论）：
    正文    original.cell_summary.summary 或 original.cell_title.title（以 "：" 开头含昵称）
    作者    original.cell_userinfo.user.{nickname,uin}
    评论    original.cell_comment.main_comment(dict) + comments[]（date 是 unix 秒）
    图片    original.cell_pic.picdata.pic[].photourl（dict，按尺寸键 0/1/11/14）
    唯一键  comm.feedskey（已含 subid+uin，跨事件稳定）
    游标    data.attachinfo + data.hasmore
事件语义 comm.subid：217 点赞聚合 / 2 评论聚合 / 1 评论 / 27 其他。
"""
import json
import random
import time

import requests

import util.ResumeUtil as Resume

BASE_URL = 'https://mobile.qzone.qq.com/get_feeds'


def build_params(g_tk, cursor, first_page):
    params = {
        'g_tk': g_tk,
        'res_type': 1,
        'format': 'json',
    }
    if first_page:
        params['refresh_type'] = 1
    else:
        params['refresh_type'] = 2
        if cursor is not None:
            params['res_attach'] = cursor
    return params


def get_feed_page(session, cursor=None, first_page=True, rate_limiter=None):
    """抓一页 mobile feeds，返回解析后的 data dict。

    可恢复错误（超时/5xx）返回 None；登录态失效抛 Resume.FatalFetchError。
    """
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
        print("请求超时")
        return None
    text = resp.text.strip()
    reason = Resume.classify_error(resp.status_code, text[:2000])
    if reason in Resume.FATAL_REASONS:
        raise Resume.FatalFetchError(f"mobile get_feeds HTTP {resp.status_code}: {reason}")
    if reason is not None:
        return None
    data = json.loads(text)
    code = data.get("code", data.get("ret"))
    if code not in (0, None):
        if code == -3000:
            raise Resume.FatalFetchError(f"get_feeds code={code}: {data.get('message')}")
        print(f"get_feeds code={code}: {data.get('message')}")
        return None
    return data.get("data") or {}


def pick_pic_urls(pic):
    """从一张 pic 记录取所有可用 URL（photourl 是按尺寸键的 dict）。"""
    urls = []
    ph = pic.get('photourl')
    if isinstance(ph, dict):
        for key in ('14', '11', '1', '0'):  # 大图优先
            v = ph.get(key)
            if isinstance(v, dict) and v.get('url'):
                urls.append(v['url'])
    if not urls and pic.get('url1'):
        urls.append(pic['url1'])
    return urls


def parse_feed(feed):
    """一条 feed → (feed_key, 行, raw_json)。

    行 = [时间, 内容, 图片链接, 评论] 四列（与流 A/流 B 产物一致）；
    feed_key = comm.feedskey（已含 subid+uin，跨事件稳定）。
    解析不出正文的 feed 返回 None（跳过）。
    """
    original = feed.get('original') or {}
    user = (original.get('cell_userinfo') or {}).get('user') or {}
    nickname = user.get('nickname') or (feed.get('userinfo') or {}).get('nickname') or ''

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
    if not body:
        return None

    feed_key = (feed.get('comm') or {}).get('feedskey')

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
            pictures.extend(pick_pic_urls(pic))

    import util.ToolsUtil as Tools
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
        comment_rows.append([c_time, c.get('content'), cu.get('nickname'), cu.get('uin')])

    return feed_key, [put_time, f"{nickname} ：{body.strip()}", ",".join(pictures), comment_rows], feed


def fetch_all_feeds(session, max_pages=None, rate_limiter=None, on_page=None,
                    resumable=None, fresh=False, store=None):
    """游标翻页抓全量。on_page(feeds, page_no) 每页回调（供调用方实时消费）。

    断点续传（C1，参照 QzoneArchive advance_feed_cursor）：游标 attachinfo 是
    自包含可序列化字符串，存进 checkpoint 即可中断后直接带回续翻，无需改写。
    每页先 append_texts 落盘再 save_checkpoint，崩溃窗口内最多重复一页，
    按 comm.feedskey 去重兜底。全量跑完（hasmore=0）清断点。

    store 不为 None 时逐页落库（B 线，source="mobile"，feedskey 幂等）。

    返回 (rows, pages_fetched)。rows 是 parse_feed 的行（四列）。
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
            cursor = resumable.checkpoint_extra().get("cursor")
            if page > 0 and cursor:
                rows = resumable.load_texts()
                seen_keys = {r[4] for r in rows if len(r) > 4 and r[4]}
                print(f"从断点第 {page} 页之后继续（游标已恢复，已有 {len(rows)} 条）")
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
                resumable.append_texts([p[1] + [p[0]] for p in new_rows])
            if store is not None:
                store.upsert_feeds(
                    [(p[0], p[1][0], p[1][1], p[1][2], p[1][3],
                      json.dumps(p[2], ensure_ascii=False)) for p in new_rows],
                    source="mobile")
        hasmore = int(data.get('hasmore') or 0)
        cursor = data.get('attachinfo')
        if isinstance(cursor, dict):
            cursor = json.dumps(cursor, ensure_ascii=False)
        if resumable is not None:
            resumable.save_checkpoint(page, extra={"cursor": cursor})
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
            print("本页请求失败（超时/可恢复错误），断点已保留，重跑续传")
    return [p[1] for p in rows] if rows and isinstance(rows[0], tuple) else rows, page
