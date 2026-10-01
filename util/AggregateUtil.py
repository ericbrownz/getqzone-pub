"""E1：把 PC 互动流的「事件行」聚合成「原动态 + 互动事件」（docs/03 §二）。

`feeds2_html_pav_all` 按互动**事件**逐条返回（一条说说被 N 人赞/评/转就回 N 行，每行带
原说说全文），直接导出会「重复刷屏」。本模块按分组键压回「一条原动态」。

分组键 = feed_key 尾段 hash（`{互动人uin}_{本人uin}_{原动态hash}`）；**不能用 content**——
「X 发表说说」这类无正文通用事件行 content 全同，按 content 会把互不相干的说说误并成组。
组内不变量：feed_key 是 PRIMARY KEY 且第三段为原动态 id → **组大小即互动人数**。

键形认不出的行退化为按 (内容, 图片) 分组，但那会把同一条说说的两批事件行分进两个组（真库
实测 190 个兜底组的正文与某个 `h:` 组逐字相同，2026-09-29）；现补齐两种「时刻 key」形态
并用 `hash_alias` 归一。**归一只按证据、不按形状猜**（教训见 docs/03 §五）：纯形状的窗口
合并实测 139 对里有 10 对正文互不相干，故结构关系一律过 `_body_compatible`。
"""
import collections
import re

import util.DeepUtil as Deep

# 采集侧另有两种带规范时刻 id 的 key 形态，都过不了「`_` 三段」：
#   S1 无分隔 `ae6a55b1<16位hex><≤2位后缀>`；S2 序号_时刻 `<序号>_<24位hex id>`。
# 两者的 24 位段就是**原动态 id**（与留档 `data-detailurl` 逐字相同，2026-09-29），末 10 位
# 即 `h:` 组用的 hash。后缀放到 3 位会让重复变多（键编码的是另一个 24 位 id），故取 ≤2 位。
_MOMENT_KEY_NO_SEP = re.compile(r"[0-9a-f]{24}[^_&]{0,2}\Z")
_MOMENT_KEY_SEQ = re.compile(r"\d+_([0-9a-f]{24})\Z")


def parse_feed_key(feed_key):
    """互动事件的 key → (actor_uin, orig_hash)。

    点赞 key 三段 `{互动人uin}_{本人uin}_{原动态hash}`；评论 key `{uin}&{postid}&{commentid}`
    的**中段同样是原动态 id**（实测与同说说点赞 key 第三段一致），借此评论行也并入正确组。

    后两种形态（S1/S2）只有原动态 id、没有互动人 uin，故首段返回 None——互动人仍走
    `feeds.actor` 列。**不能拿 S2 的序号当 uin**：那是事件序号。
    """
    k = feed_key or ""
    parts = k.split("_")
    if len(parts) == 3 and parts[0].isdigit() and parts[2]:
        return parts[0], parts[2]
    parts = k.split("&")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return parts[0], parts[1]
    if _MOMENT_KEY_NO_SEP.fullmatch(k):
        return None, k[14:24]
    m = _MOMENT_KEY_SEQ.fullmatch(k)
    if m:
        return None, m.group(1)[-10:]
    return None, None


def _body_compatible(a, b):
    """两段正文能不能安全并组：任一方缺失，或一方是另一方的子串。

    并组只保留最长正文，「子串」这条就是**不丢字**的保证。不加把关（纯按形状并）实测会丢掉
    互不相干的正文——真库 11 组（2026-09-29）。
    """
    if not a or not b or a == b:
        return True
    return a in b or b in a


def hash_alias(pairs, bodies=None):
    """[(事件键 hash, 留档 hash), ...] → {hash: 规范 hash}：把同一个「时刻」的不同写法归一。

    键第三段与留档 `data-detailurl` 的 24 位 id 是同一条说说，但写法可能不同（实测三种：
    差一位、窗口差 3、尾 0）；不归一，一条说说就裂成两帖。证据分三档：
      1. 留档 id 是权威——同一条行的 (键 hash, 留档 hash) 连边；
      2. 后两种是**结构关系**，必须过 `_body_compatible`（只按形状并会把互不相干的说说并到一起）；
      3. 其余孤立 hash 各自成类。

    pairs: [(键 hash, 留档 hash)]，留档 hash 是 id 末 10 位；空值跳过。
    bodies: {hash: 组内最长正文}，给第 2 档把关用；不传则不做把关（只该在测试里省）。
    规范名优先取留档 hash：taotao 的键尾段也是真 hash，join 才对得上。
    """
    parent = {}
    rank = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    bodies = bodies or {}
    for h in bodies:
        parent.setdefault(h, h)
    for key_h, html_h in pairs:
        if not (key_h and html_h):
            continue
        parent.setdefault(key_h, key_h)
        parent.setdefault(html_h, html_h)
        rank[html_h] = 2
        if _body_compatible(bodies.get(key_h, ""), bodies.get(html_h, "")):
            union(key_h, html_h)

    hashes = list(parent)
    # 窗口差 3：某个 hash 的 `h[3:]` 与另一 hash 的 `h[:7]` 同串 → 同一条 id 的两个窗口
    by_tail, by_head = {}, {}
    for h in hashes:
        if len(h) == 10:
            by_tail.setdefault(h[3:], h)
            by_head.setdefault(h[:7], h)
    for k7, a in by_tail.items():
        b = by_head.get(k7)
        if b and a != b and _body_compatible(bodies.get(a, ""), bodies.get(b, "")):
            union(a, b)
    # 尾 0：同一个时刻既写成 X 又写成 X0（长的那个是真 hash）
    by_tail0 = collections.defaultdict(list)
    for h in hashes:
        by_tail0[h.rstrip("0") or h].append(h)
    for group in by_tail0.values():
        if len(group) < 2:
            continue
        longest = max(group, key=len)
        for h in group:
            if h != longest and _body_compatible(bodies.get(h, ""), bodies.get(longest, "")):
                union(h, longest)

    canon = {}
    for h in hashes:
        root = find(h)
        cur = canon.get(root)
        if cur is None or (rank.get(h, 0), len(h), h) > (rank.get(cur, 0), len(cur), cur):
            canon[root] = h
    return {h: canon[find(h)] for h in hashes}


def group_key(feed_key, content, alias=None, hint=None):
    """聚合分组的键。

    有原动态 hash → `h:`+hash（唯一正确）；无 hash 且**有正文** → `c:`+内容；
    无 hash 且**无正文**的通用事件行 → `u:`+key 各自独立（按内容会把跨月/跨年的不同事件误并）。

    `alias`（见 `hash_alias`）把键形给出的 hash 归一到规范写法。`hint` 是留档读出的时刻 id
    末 10 位，**只在键形认不出时才用**——键形认得出的行走 `alias` 那条「有证据才并」的路，
    拿 hint 无条件改写会把本该并的组拆开。
    """
    _, orig = parse_feed_key(feed_key)
    if not orig and hint:
        orig = hint
    if orig and alias:
        orig = alias.get(orig, orig)
    if orig:
        return "h:" + orig
    c = (content or "").strip()
    if "：" not in c:
        return "u:" + (feed_key or "")
    return "c:" + c


def actor_kind(action, feed_key, has_comments=False):
    """互动类型：`like`（渲染进点赞框）/ `comment`（评论·回复框）。

    有 action 按动作词判（含「赞」不含「转」→ like）；action 全空的老库存退化为按 key 形状判：
    三段 key 是点赞，& 形是评论（& 形现在也能解出 actor，但**不能**因此判 like）。
    **带正文的互动行一定是评论**（`has_comments` 护住 taotao 那种「键是时刻形、正文在 comments
    列、action 却空」的行）；剩下「无 action、无正文、有时刻 hash」的是点赞——真库 382 行这样
    的点赞，旧逻辑因 24 位键解不出 uin 而全判成评论，页面上成了 319 个匿名卡片（2026-09-30）。
    """
    a = (action or "").strip()
    if a:
        return "like" if ("赞" in a and "转" not in a) else "comment"
    if "&" in (feed_key or ""):
        return "comment"
    if has_comments:
        return "comment"
    return "like" if parse_feed_key(feed_key)[1] else "comment"


def _friends_map(friends):
    """friends 可以是 {qq: name} 或 [(name, qq, page), ...]（Store.load_friends 形态）。"""
    if not friends:
        return {}
    if isinstance(friends, dict):
        return friends
    return {qq: name for name, qq, _ in friends}


def aggregate(rows, friends=None, alias=None, hints=None):
    """rows: [(feed_key, time, content, pictures[, action[, actor[, comments]]]), ...] → [原动态 dict, ...]。

    action/actor 可省（E3 前的调用方/存量库没有）；actor 优先用 feeds.actor 列，退化才回退三段
    key 的首段。comments 可省；有值时**并入组**（mobile 评论事件把评论文本存在该列，pc 同一条
    互动的行只有名字没有正文——不并就丢）。alias 见 `hash_alias`，hints 见 `group_key`。
    返回按 first_time 倒序（新的在前）；每个原动态：
        group_key   分组键（`h:`原动态hash / `c:`内容 / `u:`key），**调用方按它去重**
        orig        原动态 hash（兜底组为空串）
        content     组内最长的正文（长正文偶尔被接口截断）
        pictures    首个非空图片串（逗号分隔）
        comments    组内全部评论行 [[时间,内容,昵称,QQ],...]（按出现顺序）
        first_time  组内最早时间（≈原说说发布时间）
        last_time   组内最晚时间
        count       组内互动事件数（同一条说说被赞/评/转各算一条）
        actors      [{"qq","name","time","action","kind"}, ...] 按时间升序
    """
    fmap = _friends_map(friends)
    hints = hints or {}
    groups = collections.OrderedDict()
    for row in rows:
        feed_key, time_s, content, pictures = row[0], row[1], row[2], row[3]
        groups.setdefault(
            group_key(feed_key, content, alias, hints.get(feed_key)), []).append(row)

    out = []
    for gk, members in groups.items():
        contents = [m[2] or "" for m in members]
        pics = [m[3] for m in members if m[3]]
        times = [m[1] or "" for m in members]
        comments = []
        for m in members:
            if len(m) > 6 and m[6]:
                comments.extend(m[6])
        actors = []
        for m in members:
            action = (m[4] or "").strip() if len(m) > 4 else ""
            actor_uin = (m[5] or "").strip() if len(m) > 5 else ""
            if not actor_uin:
                actor_uin, _ = parse_feed_key(m[0])
            actors.append({
                "qq": actor_uin or "",
                "name": fmap.get(actor_uin, "") if actor_uin else "",
                "time": m[1] or "",
                "action": action,
                "kind": actor_kind(action, m[0], bool(len(m) > 6 and m[6])),
            })
        actors.sort(key=lambda a: Deep.time_sort_key(a["time"]))
        out.append({
            # 带出分组键：兜底组 orig 全空，调用方按它去重才不会把它们全塌成一条。
            "group_key": gk,
            "orig": gk[2:] if gk.startswith("h:") else "",
            "content": max(contents, key=len),
            "pictures": pics[0] if pics else "",
            "comments": comments,
            # 中文日期串不能裸比较（「11月」<「7月」，AGENTS.md §四）；解不出的串不参与
            # min/max（无年份串假造最早会污染组的时间范围），见 DeepUtil.time_min。
            "first_time": Deep.time_min(times) if times else "",
            "last_time": Deep.time_max(times) if times else "",
            "count": len(members),
            "actors": actors,
        })
    out.sort(key=lambda g: Deep.time_sort_key(g["first_time"]), reverse=True)
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

    动作词全空（E3 前的存量库）→ 退化成纯昵称拼接。
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
