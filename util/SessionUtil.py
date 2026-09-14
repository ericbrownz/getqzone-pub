"""唯一持有登录态（cookie/g_tk/uin）与请求指纹的模块。

用法：
    from util import SessionUtil
    session = SessionUtil.get_session()   # 显式触发登录/读缓存，替代旧 import 副作用

其它文件不再自造 headers；改指纹只改这里。
"""
import re

import requests

import util.LoginUtil as Login


# 统一的现代 Chromium 指纹（全仓唯一 sec-ch-ua 定义点）
FINGERPRINT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Chromium";v="131", "Not_A Brand";v="24"'
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "QQ/8.9.0.625 V1_IPH_SQ_8.9.0_1_APP_A Pixel/1170 Core/WKWebView Device/Apple(iPhone 14)"
)


class QzoneSession:
    def __init__(self, cookies=None):
        if cookies is None:
            cookies = Login.cookie()
        self.cookies = cookies
        self.uin = re.sub(r"o0*", "", cookies.get("uin"))
        self.g_tk = Login.bkn(cookies.get("p_skey"))
        self.headers = self._build_headers()
        self.mobile_headers = self._build_mobile_headers()

    def _build_headers(self):
        return {
            "authority": "user.qzone.qq.com",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
                      "application/signed-exchange;v=b3;q=0.7",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": FINGERPRINT_UA,
        }

    def _build_mobile_headers(self):
        return {
            "accept": "application/json, text/plain, */*",
            "referer": "https://mobile.qzone.qq.com/",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"iOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": MOBILE_UA,
        }

    # ---- taotao 接口专用头 ----

    def taotao_headers(self):
        """taotao.qq.com 接口专用头（带内联 cookie，referer 指向个人主页）。"""
        c = self.cookies
        return {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "cookie": f"uin={c.get('p_uin')};skey={c.get('skey')};p_uin={c.get('p_uin')};"
                      f"pt4_token={c.get('pt4_token')};p_skey={c.get('p_skey')}",
            "priority": "u=1, i",
            "referer": f"https://user.qzone.qq.com/{self.uin}/main",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": FINGERPRINT_UA,
        }


_session = None


def get_session(force_new=False):
    global _session
    if _session is None or force_new:
        _session = QzoneSession()
    return _session
