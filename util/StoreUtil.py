"""B 线数据层：SQLite 落库 + raw_json，Excel/HTML 降级为导出视图。

库文件 resource/store/<uin>.db，三流共用：
    feeds 表   feed_key UNIQUE（流 A=响应 item 的 key / 流 B=tid / mobile=comm.feedskey）
               + time / content / pictures / comments(JSON) / source / raw_json / fetched_at
    friends 表 流 A 解析出的好友（name+uin 唯一）
    meta 表    流程级杂项（如全量完成标记）

C1 断点仍走 ResumeUtil 的 JSON 文件（原样复用，不迁库——SQLite 版按 docs/02 原设计只管数据）。
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
        os.makedirs(STORE_DIR, exist_ok=True)
        self.conn = sqlite3.connect(_db_path(uin))
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
                action     TEXT           -- 互动动作词（赞了/评论/赞了我的说说…），E3 起采集
            );
            CREATE TABLE IF NOT EXISTS friends (
                qq   TEXT PRIMARY KEY,
                name TEXT,
                page TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT
            );
        """)
        # E3 起采集动作词；老库（B 线早期建的）没有 action 列，补上。
        # 列位置在新建库与迁移库之间可能不同（ALTER 追加在末尾），所以写入一律
        # 显式列名，不用位置 INSERT。
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(feeds)")}
        if "action" not in cols:
            self.conn.execute("ALTER TABLE feeds ADD COLUMN action TEXT")
        self.conn.commit()

    # ---------- feeds ----------
    def upsert_feed(self, feed_key, source, time_s=None, content=None,
                    pictures=None, comments=None, raw_json=None, action=None):
        """幂等写入：同 feed_key 重复抓到时保留首条，只补 action 空值。

        重抓回填：老库（E3 之前）action 全是 NULL，若沿用 INSERT OR IGNORE，
        重抓时所有 feed_key 都已存在 → 一条都不会回填，E3 就白采了。所以冲突时
        用 DO UPDATE，只在新 action 非空且旧值为空时写入，不动其它列。
        feed_key 为空/None 时退化为 hash(时间+内容)（标注非幂等，见 docs/02 B 风险节）。
        返回是否有行被写入或回填。
        """
        if not feed_key:
            feed_key = "hash_" + str(abs(hash(f"{time_s}|{content}")))
        cur = self.conn.execute(
            "INSERT INTO feeds "
            "(feed_key, time, content, pictures, comments, source, raw_json, fetched_at, action) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(feed_key) DO UPDATE SET action=excluded.action "
            "WHERE excluded.action IS NOT NULL AND excluded.action != '' "
            "  AND (feeds.action IS NULL OR feeds.action = '')",
            (feed_key, time_s, content, pictures,
             json.dumps(comments, ensure_ascii=False) if comments is not None else None,
             source, raw_json,
             time.strftime("%Y-%m-%d %H:%M:%S"),
             (action or "").strip() or None))
        self.conn.commit()
        return cur.rowcount > 0

    def upsert_feeds(self, rows, source):
        """批量写入。rows: [(feed_key, time, content, pictures, comments, raw_json[, action]), ...]
        第 7 项 action 可省（老调用方/测试仍传 6 元组）。返回写入或回填条数。"""
        new = 0
        for row in rows:
            key, t, content, pics, comments, raw = row[:6]
            action = row[6] if len(row) > 6 else None
            if self.upsert_feed(key, source, t, content, pics, comments, raw, action):
                new += 1
        return new

    def load_rows(self, columns="time, content, pictures, comments",
                  order="created_order", source=None):
        """读回导出视图用的行。order: created_order=落库顺序（旧行为），time_desc=按时间倒序。
        source: None=三流全部，否则只取指定流（pc/mobile/taotao）。"""
        order_by = "rowid" if order == "created_order" else "time DESC"
        sql = f"SELECT {columns} FROM feeds"
        params = ()
        if source:
            sql += " WHERE source=?"
            params = (source,)
        sql += f" ORDER BY {order_by}"
        out = []
        for row in self.conn.execute(sql, params):
            if len(row) >= 4 and "comments" in columns:
                row = list(row)
                row[3] = json.loads(row[3]) if row[3] else []
            out.append(row)
        return out

    def count(self, source=None):
        if source is None:
            return self.conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM feeds WHERE source=?", (source,)).fetchone()[0]

    def remove_rows_matching(self, moments):
        """剔除互动流（pc/mobile）里与可见说说重复的条目。

        moments 是 get_visible_moments_list 的行（[时间,内容,...]，内容以「昵称 ：」开头）。
        旧逻辑用 is_any_mutual_exist 做相似匹配；库版按「同昵称前缀 + 内容互含」匹配。
        返回删除条数。
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

    # ---------- meta ----------
    def get_meta(self, key):
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
        self.conn.commit()

    def close(self):
        self.conn.close()
