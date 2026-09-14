"""E1：把 PC 互动流的「事件行」聚合成「原动态 + 互动事件」（docs/03 §二）。

为什么需要：`feeds2_html_pav_all` 按互动**事件**逐条返回——一条说说被 N 人赞/评/转，
接口就回 N 行，每行都带原说说全文、时间是各自的互动时间。直接导出就「重复刷屏」：
全量 13877 行里 951 个内容重复组。本模块把它们压回「一条原动态」。

分组键 = feed_key 尾段 hash（格式 `{互动人uin}_{本人uin}_{原动态hash}`）。
为什么不用 content 分组：405 条无正文的通用事件行（"X 发表说说"）content 全同，
按 content 会把 180 条互不相干的说说误并成一组；hash 才是原动态 id。

组内不变量：feed_key 是 PRIMARY KEY 且第三段为原动态 id → 同一组内同一互动人
至多出现一次，**组大小即互动人数**。

局限（docs/03 §二）：
- 动作词（赞了/评论了）A 态解析时被丢弃，存量库 action 全空 → 本模块只能给出
  「谁 + 何时」。E3 起 parse_batch 已采集该字段，重抓时按 feed_key 回填，
  之后这里能一并给出「做了什么」。
- 非三段 key（约 6%）无原动态 id，退化为按 (内容, 图片) 分组。
"""
import collections


def parse_feed_key(feed_key):
    """`{互动人uin}_{本人uin}_{原动态hash}` → (actor_uin, orig_hash)。

    非三段 / 首段非数字 / 尾段为空 → (None, None)。
    """
    parts = (feed_key or "").split("_")
    if len(parts) == 3 and parts[0].isdigit() and parts[2]:
        return parts[0], parts[2]
    return None, None


def group_key(feed_key, content):
    """聚合分组的键。

    有原动态 hash → 按 hash（唯一正确）；
    无 hash（约 6%）且**有正文** → 按内容（`...00b`/`...00c` 这类确实同一条说说）；
    无 hash 且**无正文**的通用事件行（"X 发表说说"）→ 无法归属，各自独立，
    否则按内容会把跨月/跨年的不同事件误并成一组。
    """
    _, orig = parse_feed_key(feed_key)
    if orig:
        return "h:" + orig
    c = (content or "").strip()
    if "：" not in c:
        return "u:" + (feed_key or "")
    return "c:" + c


def _friends_map(friends):
    """friends 可以是 {qq: name} 或 [(name, qq, page), ...]（Store.load_friends 形态）。"""
    if not friends:
        return {}
    if isinstance(friends, dict):
        return friends
    return {qq: name for name, qq, _ in friends}


def aggregate(rows, friends=None):
    """rows: [(feed_key, time, content, pictures[, action]), ...] → [原动态 dict, ...]。

    action 可省（E3 之前的调用方/存量库没有）；有值时挂到对应 actor 上。
    返回按 first_time 倒序（新的在前）；每个原动态：
        orig        原动态 hash（兜底组为空串）
        content     组内最完整的一条正文（长正文偶尔被接口截断，取最长）
        pictures    首个非空图片串（逗号分隔）
        first_time  组内最早时间（≈原说说发布时间）
        last_time   组内最晚时间
        count       互动人数（=组大小）
        actors      [{"qq","name","time","action"}, ...] 按时间升序
    """
    fmap = _friends_map(friends)
    groups = collections.OrderedDict()
    for row in rows:
        feed_key, time_s, content, pictures = row[0], row[1], row[2], row[3]
        groups.setdefault(group_key(feed_key, content), []).append(row)

    out = []
    for gk, members in groups.items():
        contents = [m[2] or "" for m in members]
        pics = [m[3] for m in members if m[3]]
        times = [m[1] or "" for m in members]
        actors = []
        for m in members:
            actor_uin, _ = parse_feed_key(m[0])
            actors.append({
                "qq": actor_uin or "",
                "name": fmap.get(actor_uin, "") if actor_uin else "",
                "time": m[1] or "",
                "action": (m[4] or "").strip() if len(m) > 4 else "",
            })
        actors.sort(key=lambda a: a["time"])
        out.append({
            "orig": gk[2:] if gk.startswith("h:") else "",
            "content": max(contents, key=len),
            "pictures": pics[0] if pics else "",
            "first_time": min(times) if times else "",
            "last_time": max(times) if times else "",
            "count": len(members),
            "actors": actors,
        })
    out.sort(key=lambda g: g["first_time"], reverse=True)
    return out


def join_names(actors, sep="、", max_names=None):
    """把 actor 列表拼成一行昵称；无昵称退化用 uin，都没有用 ?。"""
    names = [(a["name"] or a["qq"] or "?") for a in actors]
    if max_names and len(names) > max_names:
        return sep.join(names[:max_names]) + f" 等{len(names)}人"
    return sep.join(names)


def format_actors(original, sep="、", max_names=None):
    """把原动态的互动人拼成一行。"""
    return join_names(original["actors"], sep, max_names)


def format_actors_by_action(actors, sep="、"):
    """按动作词分组拼互动人：「赞了：张三、李四；评论：王五」。

    动作词全空（E3 之前的存量库）→ 退化成纯昵称拼接，与 join_names 一致。
    """
    groups = collections.OrderedDict()
    for a in actors:
        groups.setdefault((a.get("action") or "").strip(), []).append(a)
    if not groups or set(groups) == {""}:
        return join_names(actors, sep)
    parts = []
    for act, members in groups.items():
        names = join_names(members, sep)
        parts.append(f"{act or '其他'}：{names}")
    return "；".join(parts)


def summarize(originals):
    """给导出/预览用的统计。"""
    total_events = sum(o["count"] for o in originals)
    multi = [o for o in originals if o["count"] > 1]
    return {
        "originals": len(originals),
        "events": total_events,
        "compression": (total_events / len(originals)) if originals else 0,
        "multi_originals": len(multi),
        "max_actors": max((o["count"] for o in originals), default=0),
        "nameless_actors": sum(
            1 for o in originals for a in o["actors"] if not a["name"]),
    }
