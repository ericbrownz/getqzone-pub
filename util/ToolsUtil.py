import html
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import unquote


def share_url(raw):
    """分享卡片的落地链接：`mqqapi://…` 微应用链接里真正的地址在 `fakeUrl` 参数（也见过带
    `http://` 前缀的双协议串，一样走 fakeUrl）；非 microapp 链接原样返回。
    """
    raw = (raw or "").strip()
    if "mqqapi://" in raw:
        matched = re.search(r"[?&]fakeUrl=([^&]+)", raw)
        if matched:
            return html.unescape(unquote(matched.group(1)))
    return raw


# 从一个 batch 的原始 JSONP 响应中提取所有条目各自的 html 字段
# （上游只取第一条 html，会导致每批 10 条只解析出 1 条）
def extract_all_html_fields(message):
    return [html for _, html in extract_items_with_keys(message)]


# 同上，但同时返回每条 item 的稳定 key（B 线 feed_key 用，形如 时间戳_uin_十六进制；
# 与 html 同层且 key 先于 html，一一对应；纯文本类事件个别为空串）。见 docs/02 B 线风险节。
def extract_items_with_keys(message):
    def replace_hex(match):
        hex_value = match.group(0)
        try:
            return bytes(hex_value, "utf-8").decode("unicode_escape")
        except Exception:
            return hex_value

    # 逐字符扫描，尊重反斜杠转义，只有未转义的 ' 才结束字段（同 JS 单引号字符串）。
    # 必须在 hex 替换前进行，否则 \x27 变成字面 ' 会把边界正则骗到、提前截断记录。
    def read_quoted(s, i):
        buf = []
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s):
                buf.append(c + s[i + 1])
                i += 2
                continue
            if c == "'":
                i += 1
                break
            buf.append(c)
            i += 1
        return "".join(buf), i

    items = []
    i = 0
    while True:
        # 只认 data.data[] 的 item 内 key：item 以 {ver:'1',appid 开头，
        # 跳过响应头部 main{...key:''} 的干扰
        i = message.find("key:'", i)
        if i == -1:
            break
        if message.rfind("{ver:'1',appid", 0, i) == -1:
            i += len("key:'")
            continue
        i += len("key:'")
        key, i = read_quoted(message, i)
        j = message.find("html:'", i)
        if j == -1:
            continue
        j += len("html:'")
        raw_html, j = read_quoted(message, j)
        i = j
        text = re.sub(r"\\x[0-9a-fA-F]{2}", replace_hex, raw_html)
        text = (
            text.replace("\\/", "/")
            .replace("\\t", " ")
            .replace("\\n", " ")
            .replace("\\r", " ")
            .replace("\\'", "'")
        )
        text = html.unescape(text)
        text = re.sub(
            r'<([a-zA-Z0-9]+)[^>]*?style=["\']?[^>]*?display\s*:\s*none[^>]*?>.*?</\1>',
            "",
            text,
            flags=re.I | re.S,
        )
        text = remove_hidden_short_elements(text)
        text = re.sub(r"<td[^>]*>\s*</td>", "", text, flags=re.I)
        text = text.replace("\t", " ").replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        items.append((key, text))

    return items


# 移除带 ui-mr8/none 类的隐藏字符元素，但保留长内容（如时间 <span class=" ui-mr8 state">）
# QQ 防爬会把单个隐藏字符塞进 <i class="ui-mr8"> 等标签里，用内容长度区分
def remove_hidden_short_elements(text):
    def _cb(match):
        inner = match.group(2)
        if len(re.sub(r"\s+", "", inner)) <= 4:
            return ""
        return match.group(0)

    return re.sub(
        r'<([a-zA-Z0-9]+)[^>]*?class=["\']?[^>]*?\b(ui-mr8|none)\b[^>]*?>.*?</\1>',
        _cb,
        text,
        flags=re.I | re.S,
    )


def show_author_info():
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    RESET = "\033[0m"
    RED = "\033[31m"

    author_art = r"""
 ▒▓██████▓▒░░▒▓████████▓▒░▒▓████████▓▒░▒▓██████▓▒░░▒▓████████▓▒░░▒▓██████▓▒░░▒▓███████▓▒░░▒▓████████▓▒
▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░         ░▒▓█▓▒░  ░▒▓█▓▒░░▒▓█▓▒░      ░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒
▒▓█▓▒░      ░▒▓█▓▒░         ░▒▓█▓▒░  ░▒▓█▓▒░░▒▓█▓▒░    ░▒▓██▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒
▒▓█▓▒▒▓███▓▒░▒▓██████▓▒░    ░▒▓█▓▒░  ░▒▓█▓▒░░▒▓█▓▒░  ░▒▓██▓▒░  ░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓██████▓▒
▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░         ░▒▓█▓▒░  ░▒▓█▓▒░░▒▓█▓▒░░▒▓██▓▒░    ░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒
▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░         ░▒▓█▓▒░  ░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒
 ▒▓██████▓▒░░▒▓████████▓▒░  ░▒▓█▓▒░   ░▒▓██████▓▒░░▒▓████████▓▒░░▒▓██████▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓████████▓▒
                                         ░▒▓█▓▒░
                                          ░▒▓██▓▒░
"""

    print(CYAN + author_art + RESET)

    author_info = f"{YELLOW}Forked from{RESET} {BLUE}bilibili@高数带我飞{RESET} {YELLOW}getqzone v1.0.1{RESET}"
    print(author_info)
    print(f"{RED}Always free and open-source!{RESET}")


def format_timestamp(timestamp):
    return time.strftime("%Y年%m月%d日 %H:%M:%S", time.localtime(timestamp))


# PC 互动流只对本年的动态省年份（「5月8日 14:24」），跨年才给完整串。
_YEARLESS_TIME = re.compile(r"^(\d{1,2})月(\d{1,2})日(\s+\d{1,2}:\d{2}(?::\d{2})?)?$")


def fill_missing_year(time_str, year):
    """给无年份的时间串补上 year；已带年份或 year 为空则原样返回。

    year 必须来自**绝对**来源（li id 时间戳、落库时刻），不能用「现在」：重放老留档时
    「现在」早已不是抓取当年，会补出错的年份。
    """
    text = (time_str or "").strip()
    matched = _YEARLESS_TIME.match(text)
    if not year or matched is None:
        return text
    return f"{int(year)}年{int(matched.group(1))}月{int(matched.group(2))}日{matched.group(3) or ''}"


def year_from_li_id(li_id):
    """互动流 li 的 id（`fct_{uin}_{类型}_{ts}_1_1`）→ 时间戳所在年份；取不到返回 None。

    ts 恒在第 5 段且为 10 位；兜底扫描要排除第 2 段 uin——10 位 QQ 号也存在。
    """
    parts = str(li_id or "").split("_")
    candidates = [parts[4]] if len(parts) > 4 else []
    candidates += [part for i, part in enumerate(parts) if i not in (1, 4)]
    for part in candidates:
        if len(part) == 10 and part.isdigit() and 1_000_000_000 <= int(part) < 2_200_000_000:
            return time.localtime(int(part)).tm_year
    return None


def time_parts(time_str):
    """时间串 → (年, 月) 字符串，供网页版右侧导航分档；解不出来返回 ("", "")。"""
    parsed = safe_strptime(time_str)
    if parsed is not None:
        return str(parsed.year), str(parsed.month)
    matched = re.match(r"(\d{4})年(\d{1,2})月", str(time_str or ""))
    if matched:
        return matched.group(1), str(int(matched.group(2)))
    return "", ""


def date_part(time_str):
    """时间串的日期部分：「2024年6月18日 14:20」→「2024年6月18日」。"""
    return str(time_str or "").strip().split(" ")[0]


_LI_ID = re.compile(r'<li[^>]*\bid="([^"]*)"')


def li_id_of(raw_html):
    """从留档的 li 片段里取出 li 的 id；取不到返回空串。"""
    matched = _LI_ID.search(raw_html or "")
    return matched.group(1) if matched else ""


def repair_yearless_time(time_str, raw_html="", fetched_at=""):
    """补全无年份的时间串：能补返回新串，不需要补/补不了返回 None。

    年份优先取留档 li id 的时间戳，没有留档才退回落库时刻的年份（服务端「省年份」= 本年）。
    """
    if _YEARLESS_TIME.match(str(time_str or "").strip()) is None:
        return None
    year = year_from_li_id(li_id_of(raw_html))
    if year is None:
        matched = re.match(r"(\d{4})", str(fetched_at or ""))
        year = int(matched.group(1)) if matched else None
    return fill_missing_year(time_str, year) if year else None


# 解析动态时间字符串（接口落盘/Excel 读回共用）；失败返回 None
def safe_strptime(date_str):
    if not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    if not date_str or date_str.lower() == "nan":
        return None
    formats = [
        "%Y年%m月%d日 %H:%M:%S",
        "%Y年%m月%d日 %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    return None


# 昵称哨兵：parse_li 在 get_text() 前裹上原文并记下 uin（uin 在 q_namecard 的 link 里，
# 扁平化后就没有了）。私用区字符，正文不会自然出现。
# 形如 \ue000uin\ue002昵称\ue001；uin 缺（老库行、非 q_namecard）时退化成 \ue000昵称\ue001。
NAME_OPEN = ""
NAME_SEP = ""
NAME_CLOSE = ""
_NAME_SPAN = re.compile(NAME_OPEN + "(?:([^" + NAME_SEP + "]*)" + NAME_SEP + ")?(.*?)" + NAME_CLOSE, re.S)


def split_names(text):
    """正文 → [(昵称, uin 或 ""), (普通文本, None), ...]，哨兵已剥。

    老库行没有哨兵，整体就是一段普通文本。
    """
    parts = []
    pos = 0
    for matched in _NAME_SPAN.finditer(text or ""):
        if matched.start() > pos:
            parts.append((text[pos:matched.start()], None))
        parts.append((matched.group(2), matched.group(1) or ""))
        pos = matched.end()
    if pos < len(text or ""):
        parts.append((text[pos:], None))
    return parts or [(text or "", None)]


def strip_names(text):
    """去掉哨兵，还原纯文本（Excel 等不该见到哨兵的出口用）。"""
    return "".join(part for part, _ in split_names(text))

def is_valid_json(json_data):
    try:
        json.loads(json_data)
        return True
    except ValueError:
        return False


def write_txt_file(workdir, file_name, data):
    os.makedirs(workdir, exist_ok=True)
    base_path_file_name = os.path.join(workdir, file_name)
    with open(base_path_file_name, "w", encoding="utf-8") as file:
        file.write(data)


def read_txt_file(workdir, file_name):
    base_path_file_name = os.path.join(workdir, file_name)
    if os.path.exists(base_path_file_name):
        with open(base_path_file_name, "r", encoding="utf-8") as file:
            return file.read()
    return None


# QQ空间表情替换 [em]xxx[/em] 为 <img src="http://qzonestyle.gtimg.cn/qzone/em/{code}.gif">。
# 码来自抓取数据、进 HTML 属性前必须转义（含 " 即可逃出属性注入 onerror）。
def replace_em_to_img(match):
    code = html.escape(match.group(1), quote=True)
    return (f'<img src="http://qzonestyle.gtimg.cn/qzone/em/{code}.gif" '
            f'alt="{code}">')


def get_content_from_split(content):
    content_split = str(content).split("：")
    return content_split[1].strip() if len(content_split) > 1 else content.strip()


# 判断两个字符串冒号后正文是否相等
def is_any_mutual_exist(str1, str2):
    return (get_content_from_split(str1) == get_content_from_split(str2))
