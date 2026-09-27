import os
import re
import subprocess
import sys
import time

import qrcode
import requests
import zxingcpp
from PIL import Image

import util.ConfigUtil as Config


def bkn(pSkey):
    # 计算bkn
    t, n, o = 5381, 0, len(pSkey)

    while n < o:
        t += (t << 5) + ord(pSkey[n])
        n += 1

    return t & 2147483647


def ptqrToken(qrsig):
    # 计算ptqrtoken
    n, i, e = len(qrsig), 0, 0

    while n > i:
        e += (e << 5) + ord(qrsig[i])
        i += 1

    return 2147483647 & e


def QR():
    # 获取 qq空间 二维码
    url = "https://ssl.ptlogin2.qq.com/ptqrshow?appid=549000912&e=2&l=M&s=3&d=72&v=4&t=0.8692955245720428&daid=5&pt_3rd_aid=0"

    try:
        r = requests.get(url)
        qrsig = requests.utils.dict_from_cookiejar(r.cookies).get("qrsig")

        with open(Config.temp_path + "QR.png", "wb") as f:
            f.write(r.content)

        im = Image.open(Config.temp_path + "QR.png")

        print(time.strftime("%H:%M:%S"), "登录二维码获取成功")

        result = zxingcpp.read_barcode(im)
        if result is not None:
            # EC=L + border=2：低纠错省一版（5→4），border=2 是屏幕扫码的最小可靠静区
            # （border=1 时手机经常识别失败）；行高间隙是终端/聊天显示端的行距，
            # print_ascii 产物本身无空行，无需处理
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
            qr.add_data(result.text)
            qr.print_ascii(invert=True)
            # 终端 ASCII 之外同时用系统默认看图程序打开原图（手机对图片的识别率
            # 远高于终端字符）。不做自动关闭：各平台窗口管理方式不一（osascript
            # 还有自动化权限/超时问题），由用户扫完自己关。
            if sys.platform == "darwin":
                subprocess.Popen(["open", Config.temp_path + "QR.png"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform == "win32":
                os.startfile(Config.temp_path + "QR.png")  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", Config.temp_path + "QR.png"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print(f"无法识别二维码，请扫描 {Config.temp_path}QR.png")

        return qrsig

    except Exception:
        raise


def cookie(user_file=None, force_qr=False):
    """取登录 cookie。user_file 指定登录态文件名（不提问）；force_qr 忽略已存登录态直接出码。"""
    Config.init_flooder()
    if not force_qr:
        select_user = Config.read_files_in_folder(user_file)
        if select_user:
            return select_user
    # 获取 QQ空间 cookie
    qrsig = QR()
    ptqrtoken = ptqrToken(qrsig)

    while True:
        url = (
            "https://ssl.ptlogin2.qq.com/ptqrlogin?u1=https%3A%2F%2Fqzs.qq.com%2Fqzone%2Fv5%2Floginsucc.html%3Fpara"
            "%3Dizone&ptqrtoken="
            + str(ptqrtoken)
            + "&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052&action=0-0-"
            + str(time.time())
            + "&js_ver=20032614&js_type=1&login_sig=&pt_uistyle=40&aid=549000912&daid=5&"
        )
        cookies = {"qrsig": qrsig}
        try:
            r = requests.get(url, cookies=cookies)
            if "二维码未失效" in r.text:
                pass
            elif "二维码认证中" in r.text:
                print(time.strftime("%H:%M:%S"), "二维码认证中")
            elif "二维码已失效" in r.text:
                # 死码再轮询也不会成功，qrshow 换一张（qrsig 与 ptqrtoken 必须一起换）
                print(time.strftime("%H:%M:%S"), "二维码已失效，重出")
                qrsig = QR()
                ptqrtoken = ptqrToken(qrsig)
            elif "登录成功" in r.text:
                print(time.strftime("%H:%M:%S"), "登录成功")
                cookies = requests.utils.dict_from_cookiejar(r.cookies)
                uin = requests.utils.dict_from_cookiejar(r.cookies).get("uin")
                regex = re.compile(r"ptsigx=(.*?)&")
                sigx = re.findall(regex, r.text)[0]
                url = (
                    "https://ptlogin2.qzone.qq.com/check_sig?pttype=1&uin="
                    + uin
                    + "&service=ptqrlogin&nodirect=0"
                    "&ptsigx="
                    + sigx
                    + "&s_url=https%3A%2F%2Fqzs.qq.com%2Fqzone%2Fv5%2Floginsucc.html%3Fpara%3Dizone&f_url=&ptlang"
                    "=2052&ptredirect=100&aid=549000912&daid=5&j_later=0&low_login_hour=0&regmaster=0&pt_login_type"
                    "=3&pt_aid=0&pt_aaid=16&pt_light=0&pt_3rd_aid=0"
                )
                try:
                    r = requests.get(url, cookies=cookies, allow_redirects=False)
                    target_cookies = requests.utils.dict_from_cookiejar(r.cookies)
                    p_skey = requests.utils.dict_from_cookiejar(r.cookies).get("p_skey")
                    Config.save_user(target_cookies)
                    break

                except Exception as e:
                    print(e)
            else:
                print(time.strftime("%H:%M:%S"), "用户取消登录")

        except Exception as e:
            print(e)

        time.sleep(3)

    return target_cookies
