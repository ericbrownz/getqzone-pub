import re
import json
import os
import time
import html


# 去除多余的空格
def replace_multiple_spaces(string):
    pattern = r"\s+"
    replaced_string = re.sub(pattern, " ", string)
    return replaced_string


# 从一个 batch 的原始 JSONP 响应中提取所有条目各自的 html 字段
# （上游只取第一条 html，会导致每批 10 条只解析出 1 条）
def extract_all_html_fields(message):
    return [html for _, html in extract_items_with_keys(message)]


# 同上，但同时返回每条 item 的稳定 key（B 线 feed_key 用，形如 时间戳_uin_十六进制；
# 与 html 同层且 key 先于 html，一一对应；纯文本类事件个别为空串）。
# 见 docs/02 B 线风险节 2026-09-06 验证。
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
        text = replace_multiple_spaces(text)
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


def get_html_template():
    # HTML模板
    html_template = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>QQ空间动态</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background-color: #f5f5f5;
            }}
            .post {{
                background-color: #333;
                color: #fff;
                padding: 20px;
                margin: 20px;
                border-radius: 10px;
            }}
            .avatar {{
                float: left;
                margin-right: 20px;
            }}
            .avatar img {{
                width: 50px;
                height: 50px;
                border-radius: 50%;
            }}
            .content {{
                overflow: hidden;
            }}
            .nickname {{
                font-size: 1.2em;
                font-weight: bold;
            }}
            .time {{
                color: #999;
                font-size: 0.9em;
            }}
            .message {{
                margin-top: 10px;
                font-size: 1.1em;
            }}
            .image {{
                margin-top: 10px;
                display: grid;
                grid-template-columns: repeat(3, 1fr); /* 将图片分成3列 */
                grid-gap: 10px; /* 设置图片之间的间距 */
                justify-items: center; /* 居中显示图片 */
            }}
            .image img {{
                width: 100%; /* 图片宽度100%填充父容器 */
                height: auto; /* 固定高度150px */
                object-fit: cover; /* 保持比例裁剪图片 */
                max-width: 33vw; /* 限制图片的最大宽度 */
                max-height: 33vh; /* 限制图片的最大高度 */
                border-radius: 10px;
                cursor: pointer;
            }} 
            .comments {{
                margin-top: 5px; /* 调整这里的值来减少间距 */
                background-color: #444;
                padding: 2px 10px 10px 10px;
                border-radius: 10px;
            }}
            .comment {{
                margin-top: 10px; /* 调整单个评论之间的间距 */
                padding: 10px;
                background-color: #555;
                border-radius: 10px;
                color: #fff;
            }}
            .comment .avatar img {{
                width: 30px;
                height: 30px;
            }}
            .comment .nickname {{
                font-size: 1em;
                font-weight: bold;
            }}
            .comment .time {{
                font-size: 0.8em;
                color: #aaa;
            }}
        </style>
    </head>
    <body>

        {posts}
        <script>
            // 为所有图片添加点击事件
            document.querySelectorAll(".image img").forEach(img => {{
                img.addEventListener("click", function() {{
                    window.open(this.src, '_blank');  // 打开图片链接并在新标签页中展示
                }});
            }});
        </script>
    </body>
    </html>
    """

    # 生成每个动态的HTML内容
    post_template = """
    <div class="post">
        <div class="avatar">
            <img src="{avatar_url}" alt="头像">
        </div>
        <div class="content">
            <div class="nickname">{nickname}</div>
            <div class="time">{time}</div>
            <div class="message">{message}</div>
            {image}
        </div>
         {comments}
    </div>
    """

    # 评论区HTML模板
    comment_template = """
    <div class="comments">
        <div class="comment">
            <div class="avatar">
                <img src="{avatar_url}" alt="评论头像">
            </div>
            <div class="nickname">{nickname}</div>
            <div class="time">{time}</div>
            <div class="message">{message}</div>
        </div>
    </div>
    """

    return html_template, post_template, comment_template


# 格式化时间
def format_timestamp(timestamp):
    time_struct = time.localtime(timestamp)
    formatted_time = time.strftime("%Y年%m月%d日 %H:%M:%S", time_struct)
    return formatted_time


# 解析动态时间字符串（接口落盘/Excel 读回共用）；失败返回 None
def safe_strptime(date_str):
    from datetime import datetime

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


# 判断json是否合法
def is_valid_json(json_data):
    try:
        json_object = json.loads(json_data)  # 尝试解析JSON数据
        return True  # 解析成功，是有效的JSON
    except ValueError as e:  # 解析失败，捕获异常
        print(e)
        return False  # 解析失败，不是有效的JSON


# 写入信息
def write_txt_file(workdir, file_name, data):
    if not os.path.exists(workdir):
        os.makedirs(workdir)
    base_path_file_name = os.path.join(workdir, file_name)
    with open(base_path_file_name, "w", encoding="utf-8") as file:
        file.write(data)


# 读取文件信息
def read_txt_file(workdir, file_name):
    base_path_file_name = os.path.join(workdir, file_name)
    if os.path.exists(base_path_file_name):
        with open(base_path_file_name, "r", encoding="utf-8") as file:
            return file.read()
    return None


# QQ空间表情替换 [em]xxx[/em] 为 <img src="http://qzonestyle.gtimg.cn/qzone/em/xxx.gif">
def replace_em_to_img(match):
    # 获取匹配的 xxx 部分
    emoji_code = match.group(1)
    return f'<img src="http://qzonestyle.gtimg.cn/qzone/em/{emoji_code}.gif" alt="{emoji_code}">'


def get_content_from_split(content):
    content_split = str(content).split("：")
    return content_split[1].strip() if len(content_split) > 1 else content.strip()


# 判断两个字符串是否相等
def is_any_mutual_exist(str1, str2):
    str1 = get_content_from_split(str1)
    str2 = get_content_from_split(str2)
    return str1 == str2
