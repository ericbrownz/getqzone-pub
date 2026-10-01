import json
import random
import time

import requests
from tqdm import tqdm

import util.DumpUtil as Dump
import util.ResumeUtil as Resume

# 登录态由调用方显式传入 session（SessionUtil.get_session），本模块不再 import 即登录。


def get_message(session, start, count, rate_limiter=None,
                set_='0', scope='1', begin_time='0', end_time='0'):
    """抓取 PC 互动流一页，返回 response 或 None（可恢复错误）；登录态失效/WAF 拦截等
    不可恢复错误抛 Resume.FatalFetchError。

    set_ 可换：深区（util/DeepUtil）依赖它——深带是概率服务，同一 (offset,set) 空↔满翻转，
    只打 set0 会系统性漏检。其余参数默认值即历史硬编码值（浅区行为不变）。
    """
    if rate_limiter is not None:
        rate_limiter.wait_for_slot()
    try:
        time.sleep(random.uniform(3.5, 4.5))  # 请求间隔，避免频繁请求导致被封IP
        response = requests.get(
            'https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds2_html_pav_all',
            params=dict(
                uin=session.uin,
                begin_time=begin_time,
                end_time=end_time,
                getappnotification='1',
                getnotifi='1',
                has_get_key='0',
                offset=start,
                set=set_,
                count=count,
                useutf8='1',
                outputhtmlfeed='1',
                scope=scope,
                format='jsonp',
                # 浏览器实际请求带两个 g_tk（见 test/request.txt），保持一致
                g_tk=[session.g_tk, session.g_tk],
            ),
            cookies=session.cookies,
            headers=session.headers,
            timeout=(5, 10)  # 连接 5s/读 10s，慢网兜底
        )
    except requests.Timeout:
        print("请求超时")
        return None

    reason = Resume.classify_error(response.status_code, response.text[:2000])
    # 留档（默认关）：连 WAF/登录失效页也留——那正是最需要事后看清的响应。
    Dump.save(f"pav_o{start}_c{count}_set{set_}_scope{scope}", response.content)
    if reason in Resume.FATAL_REASONS:
        raise Resume.FatalFetchError(f"PC 互动流 HTTP {response.status_code}: {reason}")
    return response


def get_login_user_info(session):
    response = requests.get(
        'https://r.qzone.qq.com/fcg-bin/cgi_get_portrait.fcg?g_tk=' + str(session.g_tk) + '&uins=' + session.uin,
        headers=session.headers, cookies=session.cookies)
    info = response.content.decode('GBK')
    # JSONP 外壳剥离：响应尾部带换行，标点有 ')' / ');' 两种形态，须先 strip 再按字符集剥
    info = info.strip()[info.index('(') + 1:].rstrip(');')
    info = json.loads(info)
    # 登录态失效时该接口回 {"error":...}：必须抛出，调用方才能回退扫码而不是崩在取值处
    if "error" in info:
        raise RuntimeError(info["error"].get("msg", "登录态失效"))
    return info


def get_message_count(session, rate_limiter=None):
    # 初始的总量范围
    lower_bound = 0
    upper_bound = 10000000  # 假设最大总量为10000000
    total = upper_bound // 2  # 初始的总量为上下界的中间值
    with tqdm(desc="正在获取消息列表数量...") as pbar:
        while lower_bound <= upper_bound:
            try:
                response = get_message(session, total, 100, rate_limiter=rate_limiter)

                if not response:
                    print(f"无效的响应对象: {response}")
                    break
                # "li" 有货说明该 offset 之下还有条目，往上探；否则往下收
                if "li" in response.text:
                    lower_bound = total + 1
                else:
                    upper_bound = total - 1

            except Exception as e:
                print(f"请求发生异常: {e}")
                break

            total = (lower_bound + upper_bound) // 2
            pbar.update(1)

    return total
