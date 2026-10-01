"""原始响应体留档（opt-in，由 main.py --dump-raw 打开）。

解析器改动就得重抓全量才能验证（E3 的 action 只能靠重抓回填，根因就是响应体解析完即丢），
留一份原始字节便可离线重放校正，不必再重抓 1~1.5h。

**默认关闭**。落盘含真实昵称/QQ号，故默认目录 resource/temp/dump/ 被 .gitignore 整个忽略。

用法: main.py --dump-raw resource/temp/dump
"""
import os
import re

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_state = None


class Dumper:
    def __init__(self, directory):
        self.directory = directory
        self.n = 0
        os.makedirs(directory, exist_ok=True)

    def save(self, ident, content):
        """写一个响应体，返回路径。带自增序号——同 (offset,set) 会重复请求（概率服务、
        二分、重试），只按 ident 命名会互相覆盖，反而丢掉最有价值的"空↔满翻转"证据。"""
        self.n += 1
        path = os.path.join(self.directory, f"{_UNSAFE.sub('_', ident)}_{self.n:04d}.txt")
        with open(path, "wb") as f:
            f.write(content)
        return path


def enable(directory):
    """打开留档；directory 为空则关闭。返回 Dumper 实例（关闭时 None）。"""
    global _state
    _state = Dumper(directory) if directory else None
    return _state


def save(ident, content):
    if _state is None:
        return None
    return _state.save(ident, content)
