"""图片补全工具：把库里的图片与互动人头像下载到 result/<uin>/pic/。

qpic 的 psc 链带时效签名（dis_t/dis_k/tm），过期即 403；同一张图在不同会话下的签名会变，
故按 URL 指纹（非整串）命名，幂等——同图只下一次、已存在跳过、失败记入 miss 清单供重试。
指纹取自 `!` 之前的路径段，与尺寸标记（`/m` 中图 / `/s` 小图 / `/b` 原图）无关，所以原图与
早先存下的中图**同名**，渲染器只要按 URL 算指纹就能找到文件，不需要索引表。

落盘一律 `.jpg`，即使服务端回的是 PNG——本地 HTML 走 file:// 打开，浏览器按内容嗅探，
后缀不参与解码；这样文件名才保持「URL 的纯函数」。

两类下载对象共用一套命名：
    正文图   <16 位指纹>.jpg      指纹由 pic_fingerprint(url) 算出
    互动人头像  av_<uin>.jpg        来自 qlogo，无签名、无时效

用法：python3 util/RedownloadUtil.py [--uin <uin>] [--sleep 0.5] [--force] [--dry-run]
只请求图片 CDN（qpic/photo.store/qlogo），不碰任何 Qzone 接口，无登录态要求。
"""
import argparse
import hashlib
import json
import sys
import os
import re
import time

import requests

try:
    import util.ConfigUtil as Config
    import util.StoreUtil as StoreUtil
except ModuleNotFoundError:
    # 允许 `python3 util/RedownloadUtil.py` 直跑（docstring 的用法）：脚本方式下
    # sys.path[0] 是 util/ 本身，仓库根不在路径上。包内导入不受影响。
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import util.ConfigUtil as Config
    import util.StoreUtil as StoreUtil

MISS_PATH = "./resource/temp/pic_missing.jsonl"
AVATAR_URL = "https://q.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640&img_type=jpg"
EMOJI_BASE = "http://qzonestyle.gtimg.cn/qzone/em/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
PLACEHOLDER_MAGIC = (b"GIF89a", b"GIF87a")
_PHOTO_NAME = re.compile(r"[0-9a-f]{16}\.jpg\Z")
# `psc?`/`psb?` 之后、第一个 `!` 之前那一段才是「哪张图」；两种标记形状必须一视同仁
_PS_TOKEN = re.compile(r"/ps[cb]\?([^!]+)")
# 渲染器与下载器必须用**同一个**表达式认表情，否则一边认得出、一边认不出，页面上就留远程图
EMOJI_TOKEN = re.compile(r"\[em\]([^\[\]]+)\[/em\]")


def pic_fingerprint(url):
    """URL → 16 位指纹，取自 `psc?`/`psb?` 之后、第一个 `!` 之前的路径段。

    这段是「哪张图」的身份：同一张图的签名（dis_t/dis_k/tm）与尺寸变体（`!/m` 中图 / `!/b`
    原图 / `!/s` / `!/c`）都共享它，不同的图不同。**两种域名形状一样**，必须走同一条规则——
    早先只认 `psc?`、且把 `photo.store` 一律 `url.split("?")[0]`，而 photo.store 的路径恰在
    `?` **之后**：切掉后整个相册的图都塌成同一个指纹（真库实测一条说说 4 张图全塌成一个，
    用户报的「图都变成相同的了」）。认不出的形状退回整串哈希——宁可文件名随签名漂移，也不
    让不同的图撞名。
    """
    m = _PS_TOKEN.search(url)
    return hashlib.md5((m.group(1) if m else url).encode("utf-8")).hexdigest()[:16]


def original_url(url):
    """中图链 → 原图链，并顺手剥掉时效签名。

    qpic 只在取中图时校验 dis_t/dis_k/tm；换成 `/b` 后服务端不再校验，去掉反而保证这条 URL
    永久可用（留着会在签名过期后 403）。两种尺寸写法 `!/m&ek=1`（psc 链）与 `!/m/xxxx`（psb
    链）都要换。photo.store 老链是直存路径、无尺寸变体，原样返回。实测原图是中图的 9~21 倍
    体积（同一张 10 KB → 221 KB），带不带 tm 都是 200、同字节数。
    """
    u = re.sub(r"[&?](?:dis_[tk]|tm)=[^&]*", "", url)
    return re.sub(r"!/m(?=[&!/]|$)", "!/b", u)


def is_placeholder(data):
    """腾讯「图片已失效」占位图：1.6~2.0 KB 的 GIF。真图是 JPEG/PNG，故按魔数+体积判。

    只判 GIF 不判 JPEG——失效占位里还有 2~5 KB 的 JPEG 灰图，与真照片无法区分，宁可不判。
    **只适用于照片**：头像、表情本来就是小图/小 GIF，按体积判会把它们全拒掉，故调用方
    （`download_items`）先按文件名区分。
    """
    return data[:6] in PLACEHOLDER_MAGIC and len(data) < 4096


def avatar(uin):
    """互动人头像 → (本地文件名, 远程 URL)。两者总是成对用，见 `local_first`。"""
    return "av_" + str(uin) + ".jpg", AVATAR_URL.format(uin=uin)


def emoji(url):
    """表情 URL → (本地文件名, 远程 URL)。表情是固定小 GIF，源站 URL 末段即编号。

    只收完整 gtimg URL：`emoji_codes` 与渲染器都产这个形状，少一层「编号 ↔ URL」的转换。
    """
    name = url.rsplit("/", 1)[-1]
    return "em_" + name, url


def photo(url):
    """正文图 URL → (本地文件名, 远程 URL)。本地图取原图，故 URL 走 `original_url`。"""
    return pic_fingerprint(url) + ".jpg", original_url(url)


def emoji_codes(posts):
    """正文与评论里出现过的表情（完整 gtimg URL）。表情图不是照片，只能从文本反推要下哪些。

    回落成完整 URL 而非裸编号，好让 `emoji()` 与渲染器拿到的形状一致。
    """
    urls = set()
    for p in posts:
        texts = [str(p.get("content") or "")]
        texts += [str(c[1] or "") for c in p.get("comments", []) if len(c) >= 2]
        for t in texts:
            urls |= {EMOJI_BASE + c + ".gif" for c in EMOJI_TOKEN.findall(t)}
    return urls


def iter_pics(pictures):
    """图片列（逗号分隔）→ [(指纹, URL)]，顺带修反斜杠转义与 HTML 实体。"""
    for u in str(pictures or "").split(","):
        u = u.replace("\\/", "/").replace("&amp;", "&").strip()
        if u.startswith("http"):
            yield pic_fingerprint(u), u


def collect_urls(store):
    """库里全部图片 URL，[(指纹, 代表 URL), ...]。同指纹多签名变体取第一个。"""
    groups = {}
    for (pics,) in store.conn.execute(
            "SELECT pictures FROM feeds WHERE pictures LIKE '%http%'"):
        for fp, u in iter_pics(pics):
            groups.setdefault(fp, u)
    return sorted(groups.items())


def avatar_uins(posts, extra=()):
    """渲染器会去取头像的全部 uin：互动人 + 评论人 + 本人（extra）。

    必须与渲染器逐个对齐——漏一个就是页面上多一张远程图（签名过期后就是空白）。评论人尤其
    容易漏：评论行并不总有对应的互动人行，只在 `comments` 里出现。
    """
    uins = {str(u) for u in extra if u}
    for p in posts:
        uins |= {str(i.get("qq")) for i in p.get("interactors", []) if i.get("qq")}
        uins |= {str(c[3]) for c in p.get("comments", []) if len(c) >= 4 and c[3]}
    uins.discard("")
    return uins


def local_first(pic_dir, name, url, fallback=None):
    """本地 pic/ 里有 `name` 就给相对路径 `pic/<name>`，否则给远程 URL。

    「本地优先」是导出目录能脱离网络打开的原因，渲染器与 Excel 列都得守同一条规则——所以
    这条规则只写在这一个地方。`fallback` 给个别调用方换掉回落目标（正文图回落缩略图链）。
    """
    if pic_dir and os.path.exists(os.path.join(pic_dir, name)):
        return "pic/" + name
    return url if fallback is None else fallback


def render_items(posts, extra_uins=()):
    """导出实际要显示的图片 → {本地文件名: 下载 URL}；extra_uins 传本人 uin。

    只收渲染集而非整库：HTML 只显示「说说 + 转发」两个桶，整库下下来六成永远不显示。
    **多收一点没关系**（比如别的桶里的互动人头像），少收一个就是页面上多一张远程图。
    """
    items = {}
    for p in posts:
        for _fp, u in iter_pics(p.get("pictures")):
            n, u = photo(u)
            items.setdefault(n, u)
    for uin in avatar_uins(posts, extra_uins):
        n, u = avatar(uin)
        items.setdefault(n, u)
    for code in emoji_codes(posts):
        n, u = emoji(code)
        items.setdefault(n, u)
    return items


def _http_fetch(url):
    resp = requests.get(url, timeout=(10, 30), headers=HEADERS)
    resp.raise_for_status()
    return resp.content


def download_items(pic_dir, items, sleep_s=0.5, force=False, fetch=None):
    """items: {本地文件名: URL}。已存在且未 force 的跳过。

    返回 (ok, gone, skip, fail)：gone 是服务端回占位图（图已失效），这类不落盘也不重试，
    否则一次拉底会把占位图当成真图覆盖掉本地已有的原图。
    """
    fetch = fetch or _http_fetch
    os.makedirs(pic_dir, exist_ok=True)
    existing = set(os.listdir(pic_dir))
    todo = [(n, u) for n, u in sorted(items.items()) if force or n not in existing]
    print(f"图片共 {len(items)} 张，本地已有 {len(existing)} 张，本次待下载 {len(todo)} 张")
    if not todo:
        return 0, 0, len(items), 0
    skip = len(items) - len(todo)

    ok = gone = fail = 0
    with open(MISS_PATH, "a", encoding="utf-8") as miss_f:
        for i, (name, url) in enumerate(todo):
            try:
                data = fetch(url)
            except Exception as e:  # 网络/CDN 的任何异常都只算这一张失败，不中断整批
                data, reason = b"", type(e).__name__
            else:
                reason = ""
            # 占位图判定只对照片：头像/表情本来就是小 GIF，按体积判会全被拒掉
            if data and not (_PHOTO_NAME.match(name) and is_placeholder(data)):
                with open(os.path.join(pic_dir, name), "wb") as f:
                    f.write(data)
                ok += 1
            else:
                if data:
                    gone += 1
                    reason = "placeholder"
                else:
                    fail += 1
                miss_f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "name": name, "url": url, "reason": reason or "empty",
                }, ensure_ascii=False) + "\n")
            if (i + 1) % 100 == 0:
                print(f"  进度 {i + 1}/{len(todo)}  成功 {ok}  失效 {gone}  失败 {fail}",
                      flush=True)
            time.sleep(sleep_s)

    print(f"完成：成功 {ok}，失效 {gone}，失败 {fail}，跳过 {skip}"
          f"（失败与失效清单：{MISS_PATH}）")
    print(f"pic/ 现有 {len(os.listdir(pic_dir))} 张")
    return ok, gone, skip, fail


def _default_uin():
    """没给 --uin 时从已保存的登录态文件名取。cookie 文件名是 o+uin，剥前缀得纯 uin。"""
    files = sorted(f for f in os.listdir(Config.user_path) if not f.startswith("."))
    if not files:
        return None
    return re.sub(r"^o0*", "", files[0])


def main():
    ap = argparse.ArgumentParser(description="按 URL 指纹补全本地图片（原图）")
    ap.add_argument("--uin", default=None)
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--force", action="store_true",
                    help="已存在的也重下——把早先存的中图升级成原图时用一次")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    uin = args.uin or _default_uin()
    if not uin:
        print("未指定 --uin 且无已保存登录态。")
        sys.exit(2)

    store = StoreUtil.Store(uin)
    try:
        items = dict(photo(u) for _fp, u in collect_urls(store))
    finally:
        store.close()

    if args.dry_run:
        print(f"库内图片 {len(items)} 张，预计下载体积见 docs/04")
        return
    download_items(Config.result_path + uin + "/pic/", items, args.sleep, args.force)


if __name__ == "__main__":
    main()
