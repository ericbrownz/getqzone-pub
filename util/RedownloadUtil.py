"""图片补全工具：从 SQLite 库读全部图片 URL，按 URL 指纹下载到 result/<uin>/pic/。

签名问题：qpic 的 psc 链带时效签名（dis_t/dis_k/tm），过期即 403/失效；
库里 2028 个 URL 收敛到 1901 个指纹（54 组是同图不同登录会话的签名变体）。
指纹命名幂等：同图不同签名只下一次；已存在跳过；失败 URL 记入 temp 目录
miss 清单供下次重试（不阻塞后续）。

用法：python3 util/RedownloadUtil.py [--uin <uin>] [--sleep 0.5] [--dry-run]
只请求图片 CDN（qpic/photo.store），不碰任何 Qzone 接口，无登录态要求。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import util.ConfigUtil as Config
import util.StoreUtil as StoreUtil

MISS_PATH = "./resource/temp/pic_missing.jsonl"


def pic_fingerprint(url):
    """URL → 16 位指纹。psc 新链取 /psc? 与 ! 之间的路径段（同一文件的
    不同签名/尺寸变体共享该段）；photo.store 老链路径即文件名，去 query 即可。"""
    if "photo.store.qq.com" in url:
        base = url.split("?")[0]
    else:
        m = re.search(r"/(psc\?[^!]+)!", url)
        base = m.group(1) if m else url
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]


def collect_urls(store):
    """库里全部图片 URL，[(fingerprint, representative_url), ...]。
    同指纹多签名变体取第一个（下载成功率等价，签名新旧都可能已失效）。"""
    groups = {}
    for (pics,) in store.conn.execute(
            "SELECT pictures FROM feeds WHERE pictures LIKE '%http%'"):
        for u in str(pics).split(","):
            u = u.replace("\\/", "/").replace("&amp;", "&").strip()
            if u.startswith("http"):
                groups.setdefault(pic_fingerprint(u), u)
    return sorted(groups.items())


def download_all(uin, sleep_s, dry_run=False):
    store = StoreUtil.Store(uin)
    pic_dir = Config.result_path + uin + "/pic/"
    os.makedirs(pic_dir, exist_ok=True)
    items = collect_urls(store)
    existing = set(os.listdir(pic_dir))
    todo = [(fp, url) for fp, url in items if fp + ".jpg" not in existing]
    print(f"库内指纹 {len(items)} 个，本地已有 {len(existing)} 张，待下载 {len(todo)} 张")

    if dry_run:
        return

    ok = fail = 0
    with open(MISS_PATH, "a", encoding="utf-8") as miss_f:
        for i, (fp, url) in enumerate(todo):
            try:
                resp = requests.get(url, timeout=(10, 30),
                                    headers={"User-Agent": "Mozilla/5.0"})
                good = resp.status_code == 200 and resp.content[:4] not in (b"",) \
                    and len(resp.content) > 1000
            except requests.RequestException:
                good = False
            if good:
                with open(pic_dir + fp + ".jpg", "wb") as f:
                    f.write(resp.content)
                ok += 1
            else:
                fail += 1
                miss_f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "fp": fp, "url": url,
                }, ensure_ascii=False) + "\n")
            if (i + 1) % 100 == 0:
                print(f"  进度 {i + 1}/{len(todo)}  成功 {ok}  失败 {fail}", flush=True)
            time.sleep(sleep_s)

    print(f"完成：成功 {ok}，失败 {fail}（{MISS_PATH} 留痕）")
    print(f"pic/ 现有 {len(os.listdir(pic_dir))} 张")


def main():
    ap = argparse.ArgumentParser(description="按 URL 指纹补全本地图片")
    ap.add_argument("--uin", default=None)
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.uin:
        download_all(args.uin, args.sleep, args.dry_run)
        return
    files = sorted(f for f in os.listdir(Config.user_path) if not f.startswith("."))
    if not files:
        print("未指定 --uin 且无已保存登录态。")
        sys.exit(2)
    # cookie 文件名是 o+uin（SessionUtil 同款剥法得纯 uin），DB/目录都用纯 uin
    default_uin = re.sub(r"^o0*", "", files[0])
    download_all(args.uin or default_uin, args.sleep, args.dry_run)


if __name__ == "__main__":
    main()
