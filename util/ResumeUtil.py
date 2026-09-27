"""C 线健壮性设施：断点续传（C1）+ 坏页跳过/定向重试（C2）+ 速率窗口（C3）。

状态文件统一放 resource/temp/，按 uin 隔离，且每条流有自己的前缀（pc_ / taotao_ /
mobile_ / pcdeep_ / probe_ 等），互不覆盖：
    <prefix><uin>_checkpoint.json   断点：{"pos": 已完成的最大批次下标}
    <prefix><uin>_texts.jsonl       抓到的条目（JSON Lines，逐行追加，中断不丢）
    <prefix><uin>_skips.jsonl       坏页记录：{"pos": offset/页码, "reason": 类型, "detail": str}
    <prefix><uin>_ratelimit.json    速率窗口状态：{"window_start": ts, "count": N}

流 A（PC 互动流）用 pos=**已完成的最大批次下标**（`offset = pos × 10`，count 固定 10）；
流 B（taotao 说说）用 pos=页码；mobile 源用
pos=已完成页数且 checkpoint 额外存游标 attachinfo（自包含可序列化，参照
QzoneArchive advance_feed_cursor 的用法：存下页游标即可续传）。
"""
import json
import os
import time

WINDOW_SECONDS = 600
DEFAULT_WINDOW_PAGES = 300

# 可恢复错误 → 记 skips 等重试；不可恢复错误 → 抛 FatalFetchError 中止主循环
RETRYABLE_REASONS = {"timeout", "http_5xx", "parse", "empty", "throttled"}
FATAL_REASONS = {"need_login", "forbidden", "waf_block"}


class FatalFetchError(Exception):
    """登录态失效/风控等不可恢复错误，主循环捕获后中止（C2：不盲重试）。"""


def classify_body(text):
    """按响应体判别业务级错误（状态码之外的信息）。"""
    if '"code":-3000' in text or "请先登录" in text:
        return "need_login"
    if "waf.tencent.com" in text or "waf.tencent-qcloud.com" in text:
        return "waf_block"
    # -10001 "network busy"：服务端软限流。**语义上 ≠ "该页为空"**——不识别的话
    # 调用方会把限流响应当空页，据此误判"已到底"或"配额耗尽"（F1 落库即栽在这）。
    # 归为可重试：走 skips 记录，冷却后由 retry_skips 补回。
    # 匹配用带引号的 JSON 形态，不用裸子串 "network busy"——否则某条说说正文里恰好
    # 出现这俩词就会被误判成限流（mock 验收 test/22 暴露的假阳性）。
    if '"code":-10001' in text or '"message":"network busy"' in text:
        return "throttled"
    return None


def classify_error(status, text):
    """把响应归类为 None（正常）/可恢复 reason/不可恢复 reason（C2）。"""
    if status is None:
        return "timeout"
    if status >= 500:
        # WAF 挑战页（如 501 跳 waf.tencent.com）是风控，不是普通 5xx
        return classify_body(text) or "http_5xx"
    if status == 403 or status == 429:
        return "forbidden"
    return classify_body(text)


class ResumeUtil:
    def __init__(self, uin, prefix="", rate_limit=None):
        """prefix 按流隔离状态文件（pc/taotao/mobile 各一套，防互相覆盖）。"""
        self.uin = uin
        base = "./resource/temp/"
        self.prefix = prefix
        self.checkpoint_path = f"{base}{prefix}{uin}_checkpoint.json"
        self.texts_path = f"{base}{prefix}{uin}_texts.jsonl"
        self.skips_path = f"{base}{prefix}{uin}_skips.jsonl"
        self.rate_path = f"{base}{prefix}{uin}_ratelimit.json"
        self._rate_limit = None
        if rate_limit is not None:
            self.enable_rate_limit(rate_limit)

    # ---------- C1 checkpoint ----------
    def load_checkpoint(self):
        try:
            with open(self.checkpoint_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def save_checkpoint(self, pos, extra=None):
        """原子写断点（先写临时文件再替换，防止中断写坏 JSON）。

        extra 是附加状态 dict（如 mobile 源的 {"cursor": attachinfo}），与 pos 一起存。
        """
        tmp = self.checkpoint_path + ".tmp"
        doc = {"pos": pos}
        if extra:
            doc.update(extra)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, self.checkpoint_path)

    def clear_checkpoint(self):
        for path in (self.checkpoint_path, self.texts_path, self.skips_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def checkpoint_pos(self):
        cp = self.load_checkpoint()
        return cp["pos"] if cp else -1

    def checkpoint_extra(self):
        cp = self.load_checkpoint()
        return {k: v for k, v in cp.items() if k != "pos"} if cp else {}

    # ---------- C1 条目落盘 ----------
    def append_texts(self, items):
        """逐行追加 JSONL；items 是已解析的 list，每个元素可 JSON 序列化。"""
        with open(self.texts_path, "a", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def load_texts(self):
        """读回全部条目（列表的列表）；空文件返回 []。"""
        texts = []
        try:
            with open(self.texts_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        texts.append(json.loads(line))
        except FileNotFoundError:
            pass
        return texts

    # ---------- C2 skips ----------
    def record_skip(self, pos, reason, detail=""):
        entry = {"pos": pos, "reason": reason, "detail": detail[:200],
                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(self.skips_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_skips(self):
        skips = []
        try:
            with open(self.skips_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        skips.append(json.loads(line))
        except FileNotFoundError:
            pass
        return skips

    def remove_skips(self, positions):
        """重试成功后从 skips 中移除对应 pos 的记录。"""
        remaining = [s for s in self.load_skips() if s.get("pos") not in positions]
        with open(self.skips_path, "w", encoding="utf-8") as f:
            for s in remaining:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # ---------- C3 速率窗口 ----------
    def enable_rate_limit(self, pages):
        self._rate_limit = max(1, int(pages))

    def wait_for_slot(self, quiet=False):
        """获取一个请求配额；窗口超限则倒计时等待。落盘状态支持跨进程/中断恢复。"""
        if self._rate_limit is None:
            return
        state = self._load_rate_state()
        now = time.time()
        start = state.get("window_start", now)
        if now - start >= WINDOW_SECONDS:
            start, count = now, 0
        else:
            count = state.get("count", 0)
        if count >= self._rate_limit:
            remaining = WINDOW_SECONDS - (now - start)
            self._sleep_countdown(remaining, quiet)
            start, count = time.time(), 0
        self._save_rate_state(start, count + 1)

    def _load_rate_state(self):
        try:
            with open(self.rate_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_rate_state(self, window_start, count):
        with open(self.rate_path, "w", encoding="utf-8") as f:
            json.dump({"window_start": window_start, "count": count}, f)

    @staticmethod
    def _sleep_countdown(seconds, quiet):
        seconds = int(seconds) + 1
        while seconds > 0:
            if not quiet:
                print(f"\r[速率窗口] 已达上限，{seconds:>4d}s 后继续...", end="", flush=True)
            time.sleep(1)
            seconds -= 1
        if not quiet:
            print("\r[速率窗口] 窗口重置，继续抓取        ")
