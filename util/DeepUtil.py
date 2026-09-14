"""pc 互动流深区模式（opt-in，由 main.py --deep 调用）。

深带（offset ~20500-21500）是**已删除旧说说的互动流残留**，内容最早到 2014-08（建号期），
即库底 2015-10-11 之前那一年的补洞目标。它的服务特性与浅区截然不同，本模块把
test/14-22 探针里验证过的方法学固化成生产代码：

  - **概率服务**：同一 (offset,set) 请求结果逐次翻转（空↔满），命中多为 set2/set3。
    故每格轮试 set0..3——**单发/单 set 证不了"无货"**（只打 set0 会系统性漏检）。
  - **软限流 ≠ 空页**：`-10001 "network busy"` 是服务端限流，干净空页是
    `code:0,total_number:0,data:[undefined]`。把限流当空页会得出假"配额耗尽"结论
    （test/20 首次落库即栽在这）→ 限流退避重试；退避耗尽则中止且**不前进断点**。
  - **浅区 control 门**：先打 offset=0；它都不活就整体不可判，一次深请求都不发，
    否则会把"整体被限流"误判成"该带没数据"。

收获**直写库**（source="pc"，feed_key 幂等）：resource/temp/deep_harvest_new.jsonl 只有
key/time/text、无 pictures/raw，把它导库会把图位锁死（feed_key 已存在，后续补不上图），
所以必须重抓一遍拿全字段。

用法: main.py --deep [--deep-offsets 20500,20800] [--deep-rate 60]
"""
import collections
import json
import time

import util.RequestUtil as Request
import util.ResumeUtil as Resume

# test/16 run6 的 9 个命中格 (offset, 记录到的 set)，已与 deep_harvest_new.jsonl 逐格核对。
# 记录到的 set 只是轮试的首选（深带概率服务，哪个 set 有货会变）。
DEEP_CELLS = [
    (20500, 2), (20600, 3), (20800, 2), (20900, 3), (21100, 2),
    (21200, 2), (21300, 0), (21400, 2), (21500, 3),
]
CONTROL_OFFSET = 0  # 浅区对照：稳定有货，用来区分"深带冷"与"整体被限流"
SETS = (0, 1, 2, 3)
THROTTLE_BACKOFF = (20, 45)  # 秒；实测两次退避可让 2/3 的限流请求恢复
EMPTY_STREAK_ABORT = 2  # 连续几格"四 set 干净空页"才判该带本次不服务
MAX_REQUESTS = 120  # 硬顶：控制流最坏 3 发 + 每格最坏 4 set × 3 发 × 9 格 = 111
DEEP_WINDOW_PAGES = 60  # 每 10 分钟页数上限（比浅区 --rate 300 严得多）


class PavFetcher:
    """把 Request.get_message 适配成探针契约：fetch_pav_raw(offset, count, set_, scope)
    → (text|None, reason|None)。

    reason 语义与 test/14 的 Probe.fetch_pav_raw / test/22 的 FakeProbe 一致，
    这样深区逻辑与探针脚本共用同一套判定，test/23 也能塞假 fetcher 离线验收。
    """

    def __init__(self, session, rate_limiter=None, count=100):
        self.session = session
        self.rate_limiter = rate_limiter
        self.count = count
        self.n_requests = 0

    def fetch_pav_raw(self, offset, count=None, set_=0, scope=1):
        self.n_requests += 1
        # need_login/waf_block 由 get_message 直接抛 FatalFetchError，上抛给 main 的 dispatch
        resp = Request.get_message(self.session, offset, count or self.count,
                                   rate_limiter=self.rate_limiter,
                                   set_=str(set_), scope=str(scope))
        if resp is None or not hasattr(resp, "content"):
            return None, "timeout"
        text = resp.content.decode("utf-8", errors="replace")
        return text, Resume.classify_error(resp.status_code, text[:2000])


def fetch_cell(fetcher, offset, set_pref, parse_fn, throttle_backoff=THROTTLE_BACKOFF):
    """轮试 set 直到有货，返回 (batch, friends, used_set, status)。

    status ∈ {hit, empty, throttled}。**限流(network busy) ≠ 空页**：被限流时退避重试
    同一 (offset,set)，绝不能当"该格无货"——否则会误判整带到底或配额耗尽。"""
    order = [set_pref] + [s for s in SETS if s != set_pref]
    for set_ in order:
        text, reason = fetcher.fetch_pav_raw(offset, 100, set_=set_, scope=1)
        if reason == "throttled":
            for wait in throttle_backoff:
                print(f"  offset={offset} set{set_} 被限流(network busy)，退避 {wait}s 重试",
                      flush=True)
                time.sleep(wait)
                text, reason = fetcher.fetch_pav_raw(offset, 100, set_=set_, scope=1)
                if reason != "throttled":
                    break
            if reason == "throttled":
                return [], [], None, "throttled"
        if reason is not None:
            continue
        if text:
            batch, friends = parse_fn(text, f"deep{offset}")
            if batch:
                return batch, friends, set_, "hit"
    return [], [], None, "empty"


def control_alive(fetcher, parse_fn, throttle_backoff=THROTTLE_BACKOFF):
    """浅区对照：offset=0 单发（含一次退避）。返回 (alive, status)。

    control 不活时任何"深带无货"的结论都不成立——那只是整体不可判。"""
    text, reason = fetcher.fetch_pav_raw(CONTROL_OFFSET, 100, set_=0, scope=1)
    if reason == "throttled":
        for wait in throttle_backoff:
            print(f"  control(offset={CONTROL_OFFSET}) 被限流，退避 {wait}s 重试", flush=True)
            time.sleep(wait)
            text, reason = fetcher.fetch_pav_raw(CONTROL_OFFSET, 100, set_=0, scope=1)
            if reason != "throttled":
                break
    if reason == "throttled":
        return False, "throttled"
    if reason is not None:
        return False, reason
    if text and parse_fn(text, "control")[0]:
        return True, "hit"
    return False, "empty"


def fetch_deep_band(session, store, parse_fn, resumable, cells=None,
                    throttle_backoff=THROTTLE_BACKOFF):
    """深区主循环：control 门 → 逐格轮 set 收获 → 落库。返回摘要 dict。

    cells: [(offset, 首选set|None), ...]；None 表示首选未知（--deep-offsets），从 set0 轮。
    断点存 offset（不是格序号），所以换格表后仍能正确续跑。"""
    cells = sorted({(int(c[0]), c[1]) for c in (cells or DEEP_CELLS)})
    fetcher = PavFetcher(session, rate_limiter=resumable)
    before_keys = {r[0] for r in store.conn.execute("SELECT feed_key FROM feeds")}
    before_pc = store.count("pc")

    cp = resumable.checkpoint_pos()
    if cp >= 0:
        todo = [c for c in cells if c[0] > cp]
        print(f"深区断点 offset={cp}：本轮剩 {len(todo)}/{len(cells)} 格", flush=True)
    else:
        todo = cells

    print(f"深区格表 {len(cells)} 格：{[c[0] for c in cells]}", flush=True)
    results, added, pics_added = {}, 0, 0
    new_years = collections.Counter()
    empty_streak = 0
    t0 = time.time()
    aborted = None

    alive, ctl_status = control_alive(fetcher, parse_fn, throttle_backoff)
    print(f"[control] offset={CONTROL_OFFSET}: {ctl_status}", flush=True)
    if not alive:
        print("control 不活 → 整体不可判（先冷却；多半是登录态或服务端限流），未发任何深区请求",
              flush=True)
        return _summary(fetcher, ctl_status, results, before_pc, store, added, pics_added,
                        new_years, t0, "control 不活，整体不可判")

    try:
        for offset, set_pref in todo:
            if fetcher.n_requests >= MAX_REQUESTS:
                aborted = f"已达请求硬顶 {MAX_REQUESTS}"
                print(f"{aborted}，中止（断点已保存，可重跑续传）", flush=True)
                break
            batch, friends, used_set, status = fetch_cell(
                fetcher, offset, set_pref if set_pref is not None else 0, parse_fn,
                throttle_backoff)
            results[offset] = status

            if status == "throttled":
                resumable.record_skip(offset, "throttled", "深区退避后仍被限流")
                aborted = "退避后仍被限流（配额窗口耗尽）"
                print(f"  offset={offset} {aborted} → 中止；限流≠无货，未把该格记为无内容",
                      flush=True)
                break
            if status == "empty":
                empty_streak += 1
                resumable.save_checkpoint(offset)
                print(f"  offset={offset} 四 set 全空（真·空页，code:0 total_number:0），跳过",
                      flush=True)
                if empty_streak >= EMPTY_STREAK_ABORT:
                    aborted = "连续 2 格真·空页"
                    print(f"{aborted} → 该带本次不服务，中止以免空耗配额。", flush=True)
                    break
                continue

            empty_streak = 0
            for name, qq, link in friends:
                store.upsert_friend(qq, name, link)
            fresh = [b for b in batch if b[0] not in before_keys]
            store.upsert_feeds(batch, source="pc")
            added += len(fresh)
            pics_added += sum(1 for b in fresh if b[3])
            for b in fresh:
                new_years[b[1][:5]] += 1
            before_keys.update(b[0] for b in batch)  # 防跨格重复统计
            resumable.save_checkpoint(offset, extra={"used_set": used_set})
            print(f"  offset={offset} set{used_set}: 抓到 {len(batch)} 条，其中新 {len(fresh)}"
                  f"（带图 {sum(1 for b in fresh if b[3])}）", flush=True)
    except KeyboardInterrupt:
        aborted = "手动中断"
        print("\n深区手动中断，断点已保存（可重跑续传）", flush=True)

    if aborted is None:
        resumable.clear_checkpoint()
        print("格表跑完 → 已清深区断点（下次 --deep 重新整表扫）", flush=True)
    return _summary(fetcher, ctl_status, results, before_pc, store, added, pics_added,
                    new_years, t0, aborted or "跑完")


def _summary(fetcher, ctl_status, results, before_pc, store, added, pics_added,
             new_years, t0, verdict):
    after_pc = store.count("pc")
    times = sorted(r[0] for r in store.load_rows(columns="time", source="pc") if r[0])
    out = {
        "requests": fetcher.n_requests,
        "control": ctl_status,
        "cells": results,
        "pc_before": before_pc, "pc_after": after_pc, "pc_delta": after_pc - before_pc,
        "new_keys": added, "new_with_pic": pics_added,
        "verdict": verdict,
        "elapsed_min": round((time.time() - t0) / 60, 1),
    }
    print("\n===== 深区摘要 =====", flush=True)
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    if times:
        print(f"pc 库底: {times[0]} → 库顶 {times[-1]}", flush=True)
    if new_years:
        print("新增行年月分布(前 12):", dict(sorted(new_years.items())[:12]), flush=True)
    return out
