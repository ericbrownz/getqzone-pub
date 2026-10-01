"""B 线数据层：SQLite 落库 + raw_json，Excel/HTML 降级为导出视图。

库文件 resource/store/<uin>.db，三流共用：
    feeds 表   feed_key UNIQUE（流 A=响应 item 的 key / 流 B=tid / mobile=comm.feedskey）
               + time / content / pictures / comments(JSON) / source / raw_json / fetched_at
               + action（动作词）/ actor（互动人 uin）
    friends 表 流 A 解析出的好友（name+uin 唯一）

actor 单独成列：评论/回复/转发的 feed_key 是**评论自己的 data-key**，不含互动人 uin、反推
不出；且改键会与已落库的旧 key 冲突、破坏幂等，故另存一列（uin 只在响应 HTML 的
nameCard_<uin> 里）。

C1 断点仍走 ResumeUtil 的 JSON 文件，不迁库（SQLite 版按 docs/02 原设计只管数据）。
"""
import json
import os
import sqlite3
import time

import util.ToolsUtil as Tools

STORE_DIR = "./resource/store/"


def _db_path(uin):
    return os.path.join(STORE_DIR, f"{uin}.db")


class Store:
    def __init__(self, uin):
        self.uin = uin
        self.path = _db_path(uin)
        os.makedirs(STORE_DIR, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS feeds (
                feed_key   TEXT PRIMARY KEY,
                time       TEXT,
                content    TEXT,
                pictures   TEXT,
                comments   TEXT,          -- JSON 数组 [[时间,内容,昵称,QQ],...]
                source     TEXT NOT NULL, -- pc / taotao / mobile
                raw_json   TEXT,
                fetched_at TEXT,
                action     TEXT,          -- 互动动作词（赞了/评论/赞了我的说说…），E3 起采集
                actor      TEXT           -- 互动人 uin（评论行 key 里没有，只能从响应 HTML 提）
            );
            CREATE TABLE IF NOT EXISTS friends (
                qq   TEXT PRIMARY KEY,
                name TEXT,
                page TEXT
            );
        """)
        # E3 起采集动作词；老库没有 action/actor 列，ALTER 补上。列位置在新库与迁移库
        # 之间可能不同（ALTER 追加在末尾），故写入一律显式列名，不用位置 INSERT。
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(feeds)")}
        if "action" not in cols:
            self.conn.execute("ALTER TABLE feeds ADD COLUMN action TEXT")
        if "actor" not in cols:
            self.conn.execute("ALTER TABLE feeds ADD COLUMN actor TEXT")
        self.conn.commit()

    # ---------- feeds ----------
    def upsert_feed(self, feed_key, source, time_s=None, content=None,
                    pictures=None, comments=None, raw_json=None, action=None,
                    actor=None):
        """幂等写入：同 feed_key 重复抓到时保留首条，冲突时只在新值非空且旧值为空时回填
        action / raw_json / actor——老库这三列全空，若用 INSERT OR IGNORE，重抓时所有
        feed_key 已存在 → 一条都补不上。feed_key 为空时退化为 hash(时间+内容)，非幂等
        （docs/02 B 风险节）。返回是否有行被写入或回填。
        """
        if not feed_key:
            feed_key = "hash_" + str(abs(hash(f"{time_s}|{content}")))
        cur = self.conn.execute(
            "INSERT INTO feeds "
            "(feed_key, time, content, pictures, comments, source, raw_json, fetched_at, action, actor) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(feed_key) DO UPDATE SET "
            "  action   = COALESCE(NULLIF(feeds.action, ''),   NULLIF(excluded.action, '')), "
            "  raw_json = COALESCE(NULLIF(feeds.raw_json, ''), NULLIF(excluded.raw_json, '')), "
            "  actor    = COALESCE(NULLIF(feeds.actor, ''),    NULLIF(excluded.actor, '')) "
            "WHERE (excluded.action IS NOT NULL AND excluded.action != '' "
            "       AND (feeds.action IS NULL OR feeds.action = '')) "
            "   OR (excluded.raw_json IS NOT NULL AND excluded.raw_json != '' "
            "       AND (feeds.raw_json IS NULL OR feeds.raw_json = '')) "
            "   OR (excluded.actor IS NOT NULL AND excluded.actor != '' "
            "       AND (feeds.actor IS NULL OR feeds.actor = ''))",
            (feed_key, time_s, content, pictures,
             json.dumps(comments, ensure_ascii=False) if comments is not None else None,
             source, raw_json,
             time.strftime("%Y-%m-%d %H:%M:%S"),
             (action or "").strip() or None,
             (actor or "").strip() or None))
        self.conn.commit()
        return cur.rowcount > 0

    def upsert_feeds(self, rows, source):
        """批量写入。rows: [(feed_key, time, content, pictures, comments, raw_json[, action[, actor]]), ...]，
        第 7/8 项可省（老调用方/测试仍传 6 元组）。返回写入或回填条数。"""
        new = 0
        for row in rows:
            key, t, content, pics, comments, raw = row[:6]
            action = row[6] if len(row) > 6 else None
            actor = row[7] if len(row) > 7 else None
            if self.upsert_feed(key, source, t, content, pics, comments, raw, action, actor):
                new += 1
        return new

    def backfill_actors(self):
        """离线回填 actor：从留档 raw_json 的 `nameCard_<uin>` 提互动人。

        评论/回复/转发的 feed_key 不含互动人 uin、反推不出；uin 只在响应 HTML 的 li 头
        `<a class="f-name q_namecard" link="nameCard_<uin>">昵称</a>` 里。只处理 actor 为空
        且有留档的行（墙外的行没有留档，只能等 pc 窗口恢复后重抓）。顺带把 (uin, 昵称)
        补进 friends 表，渲染查昵称走既有 friends map。

        返回 {"scanned": 扫过, "filled": 填了 actor, "friends": 新进 friends 表}。
        """
        from bs4 import BeautifulSoup

        scanned = filled = new_friends = 0
        for feed_key, raw in self.conn.execute(
                "SELECT feed_key, raw_json FROM feeds "
                "WHERE source='pc' AND raw_json IS NOT NULL AND raw_json != '' "
                "  AND (actor IS NULL OR actor = '')"):
            scanned += 1
            element = BeautifulSoup(raw, "html.parser").find(
                "a", class_="f-name q_namecard")
            link = element.get("link") if element is not None else None
            if not isinstance(link, str) or not link.startswith("nameCard_"):
                continue
            uin = link.removeprefix("nameCard_")
            if not uin:
                continue
            self.conn.execute(
                "UPDATE feeds SET actor=? WHERE feed_key=?", (uin, feed_key))
            filled += 1
            if self.upsert_friend(uin, element.get_text().strip(), ""):
                new_friends += 1
        self.conn.commit()
        return {"scanned": scanned, "filled": filled, "friends": new_friends}

    def fix_yearless_times(self):
        """离线补全无年份的时间串（「5月8日 14:24」→「2026年5月8日 14:24」）。

        这类串 safe_strptime 解不出来，导出排序时被判成 None 沉到最底，相册动态因此「丢失」。
        年份怎么取见 Tools.repair_yearless_time。返回 {"scanned","fixed"}（scanned 是命中
        「无年份」形状的行数）。
        """
        rows = list(self.conn.execute(
            "SELECT feed_key, time, raw_json, fetched_at FROM feeds"))
        scanned = fixed = 0
        for feed_key, time_s, raw, fetched in rows:
            fixed_time = Tools.repair_yearless_time(time_s, raw or "", fetched or "")
            if fixed_time is None:
                continue
            scanned += 1
            if fixed_time != time_s:
                self.conn.execute(
                    "UPDATE feeds SET time=? WHERE feed_key=?", (fixed_time, feed_key))
                fixed += 1
        self.conn.commit()
        return {"scanned": scanned, "fixed": fixed}

    def rekey_mobile(self, key_fn):
        """把存量 mobile 行的 `comm.feedskey` 归一化成 pc 明文键（docs/03 §五）。只治存量，
        采集侧 parse_feed 现已直接写明文键。

        撞键（同一条互动的 pc 行已存在）就并掉 mobile 行：往对方空列补 action/actor，评论
        列也并（mobile 评论事件把评论文本存 comments 列，pc 行只有这一份，不并就真丢了）；
        正文/图片以 pc 行为准，**raw_json 不并**（pc 留档是 HTML，混进 mobile 的 JSON 会让
        --reparse-raw pc 的 HTML 解析器吃到非预期输入）。不撞键的直接改写主键。幂等。
        返回 {"scanned","moved","merged"}。
        """
        scanned = moved = merged = 0
        for feed_key, in self.conn.execute(
                "SELECT feed_key FROM feeds WHERE source='mobile'").fetchall():
            scanned += 1
            new_key = key_fn(feed_key)
            if not new_key or new_key == feed_key:
                continue
            old = self.conn.execute(
                "SELECT action, actor, comments FROM feeds WHERE feed_key=?",
                (feed_key,)).fetchone()
            if self.conn.execute(
                    "SELECT 1 FROM feeds WHERE feed_key=?", (new_key,)).fetchone():
                self.conn.execute(
                    "UPDATE feeds SET action   = COALESCE(NULLIF(action, ''), NULLIF(?, '')), "
                    "                 actor    = COALESCE(NULLIF(actor, ''),  NULLIF(?, '')), "
                    "                 comments = CASE WHEN comments IS NULL OR comments IN ('', '[]') "
                    "                                 THEN NULLIF(?, '[]') ELSE comments END "
                    "WHERE feed_key=?",
                    (old[0], old[1], old[2], new_key))
                self.conn.execute("DELETE FROM feeds WHERE feed_key=?", (feed_key,))
                merged += 1
            else:
                self.conn.execute(
                    "UPDATE feeds SET feed_key=? WHERE feed_key=?", (new_key, feed_key))
                moved += 1
        self.conn.commit()
        return {"scanned": scanned, "moved": moved, "merged": merged}

    def replay_rows(self, rows, source, pictures="fill"):
        """留档重放：新 feed_key 插入，老 feed_key 用重算值**覆盖** 时间/正文/评论/图片。

        与 upsert_feed 相反（那是采集期幂等去重、只回填空列），故不复用——本方法用于解析器
        改进后离线重放留档（--reparse-raw / --replay-dump），必须覆盖。raw_json 一并刷新；
        action/actor 只在重算值非空时覆盖，免得抹掉已回填的值。

        pictures 决定图片列怎么覆盖：
          "fill"    旧值已是 http 图就保留，只在缺失/非 http（懒加载占位 /ac/b.gif）时填真图。
                    pc 图是签名 URL，重抓会让 tm/dis_t token 变（实测同图同字节），无条件覆盖
                    只会白白改写，故默认保守。
          "replace" 无条件覆盖。mobile 相册去重是「同一张图 4 档尺寸 → 1 张」这种「变少」的
                    修复，fill 判断不出来，必须 replace。

        返回 {"inserted","updated","unchanged"}。
        """
        inserted = updated = unchanged = 0
        for row in rows:
            key, t, content, pics, comments, raw = row[:6]
            action = (row[6] if len(row) > 6 else None) or ""
            actor = (row[7] if len(row) > 7 else None) or ""
            if not key:
                # 与 upsert_feed 一致的空 key 兜底（非幂等）
                key = "hash_" + str(abs(hash(f"{t}|{content}")))
            cm = json.dumps(comments, ensure_ascii=False) if comments is not None else None
            old = self.conn.execute(
                "SELECT time, content, pictures, comments FROM feeds WHERE feed_key=?",
                (key,)).fetchone()
            if old is None:
                self.conn.execute(
                    "INSERT INTO feeds "
                    "(feed_key, time, content, pictures, comments, source, raw_json, fetched_at, action, actor) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (key, t, content, pics, cm, source, raw,
                     time.strftime("%Y-%m-%d %H:%M:%S"), action.strip() or None,
                     actor.strip() or None))
                inserted += 1
                continue
            if pictures == "fill" and (old[2] or "").startswith("http"):
                pics = old[2]
            if tuple(old) == (t, content, pics, cm):
                unchanged += 1
                continue
            self.conn.execute(
                "UPDATE feeds SET time=?, content=?, pictures=?, comments=?, "
                "  raw_json     = COALESCE(NULLIF(?, ''), raw_json), "
                "  action       = COALESCE(NULLIF(?, ''), action), "
                "  actor        = COALESCE(NULLIF(?, ''), actor) "
                "WHERE feed_key=?",
                (t, content, pics, cm, raw, action.strip(), actor.strip(), key))
            updated += 1
        self.conn.commit()
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged}

    def reparse(self, source, derive, pictures="fill"):
        """用留档 raw_json 重算某流的存量行并覆盖（解析器改进后不必重抓）。

        derive(raw_json) → (time, content, pictures, comments[, action[, actor]])，解不出返回
        None（保留原行）——只覆盖**能解出**的行，解析器收窄导致的丢行不会误删老数据。
        pictures 见 replay_rows。返回 replay_rows 的统计，外加 "scanned"（有留档的行数）。
        """
        rows = []
        scanned = 0
        for feed_key, raw in self.conn.execute(
                "SELECT feed_key, raw_json FROM feeds "
                "WHERE source=? AND raw_json IS NOT NULL AND raw_json != ''", (source,)):
            scanned += 1
            got = derive(raw)
            if got:
                # raw 固定插在第 5 位（replay_rows 的行布局），动作词/互动人跟在它后面
                rows.append([feed_key, *got[:4], raw, *got[4:]])
        stats = self.replay_rows(rows, source, pictures=pictures)
        stats["scanned"] = scanned
        return stats

    def load_rows(self, columns="time, content, pictures, comments", source=None):
        """读回导出视图用的行（落库顺序）。source: None=三流全部，否则只取指定流。

        comments 列（JSON 数组）自动反序列化，按列名定位而非固定第 4 位——聚合路径会带
        feed_key/action/actor 等额外列，comments 的位置随列序漂移。
        """
        sql = f"SELECT {columns} FROM feeds"
        params = ()
        if source:
            sql += " WHERE source=?"
            params = (source,)
        ci = [c.strip().lower() for c in columns.split(",")].index("comments") \
            if "comments" in columns else None
        out = []
        for row in self.conn.execute(sql + " ORDER BY rowid", params):
            if ci is not None:
                row = list(row)
                row[ci] = json.loads(row[ci]) if row[ci] else []
            out.append(row)
        return out

    def moment_ids(self, source):
        """[(feed_key, 24 位规范时刻 id), ...]：留档 HTML `data-detailurl=".../mood/<id>."` 的 id。

        只回 24 字节而非整段 raw_json：raw_json 约 4~12 KB/行，pc 有留档的 5000+ 行整读是几十
        MB，聚合层只要这一小段（`instr` 找第一处 `/mood/`，实测与 Python 正则逐字相同，5377/5377）。
        不是 24 位十六进制的行跳过：那说明第一处 `/mood/` 后面不是规范 id，这些行回退用键形。
        """
        out = []
        for feed_key, mid in self.conn.execute(
                "SELECT feed_key, substr(raw_json, instr(raw_json, '/mood/') + 6, 24) "
                "FROM feeds WHERE source=? AND raw_json LIKE '%/mood/%'", (source,)):
            mid = mid or ""
            if len(mid) == 24 and all(c in "0123456789abcdef" for c in mid):
                out.append((feed_key, mid))
        return out

    def count(self, source=None):
        if source is None:
            return self.conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM feeds WHERE source=?", (source,)).fetchone()[0]

    def remove_rows_matching(self, moments):
        """剔除互动流（pc/mobile）里与可见说说重复的条目，返回删除条数。

        moments 是 get_visible_moments_list 的行（[时间,内容,...]，内容以「昵称 ：」开头）。
        库版按「同昵称前缀 + 内容互含」匹配（旧逻辑用 is_any_mutual_exist）。
        """
        removed = 0
        for row in self.conn.execute(
                "SELECT feed_key, content FROM feeds WHERE source != 'taotao'"):
            feed_key, content = row
            if content and any(Tools.is_any_mutual_exist(content, u[1]) for u in moments):
                self.conn.execute("DELETE FROM feeds WHERE feed_key=?", (feed_key,))
                removed += 1
        if removed:
            self.conn.commit()
        return removed

    # ---------- friends ----------
    def upsert_friend(self, qq, name, page):
        if not qq:
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO friends VALUES (?,?,?)", (qq, name, page))
        self.conn.commit()
        return cur.rowcount > 0

    def load_friends(self):
        return list(self.conn.execute("SELECT name, qq, page FROM friends ORDER BY rowid"))

    def close(self):
        self.conn.close()
