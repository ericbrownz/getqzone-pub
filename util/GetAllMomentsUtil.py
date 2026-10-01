import json
import math
import re
import time

import requests
from tqdm import tqdm

import util.SessionUtil as SessionUtil
import util.ToolsUtil as Tools

USER_QZONE_INFO = 'user_qzone_info.json'
QZONE_MOMENTS_ALL = 'qzone_moments_all.json'


def _workdir(session):
    return f"./resource/fetch-all/{session.uin}"


# 获取所有可见的未删除的说说+高清图片（包含2014年之前）
def get_visible_moments_list(session=None, resumable=None, fresh=False, store=None):
    if session is None:
        session = SessionUtil.get_session()
    assert session.uin, "未登录，uin 为空"

    workdir = _workdir(session)

    # 1. 获取说说总条数（本地缓存优先，仅首次请求）
    user_qzone_info = Tools.read_txt_file(workdir, USER_QZONE_INFO)
    if not user_qzone_info:
        user_qzone_info = get_user_qzone_info(session, 1)
        Tools.write_txt_file(workdir, USER_QZONE_INFO, user_qzone_info)

    if not Tools.is_valid_json(user_qzone_info):
        print("获取QQ空间信息失败")
        return None
    total_moments_count = json.loads(user_qzone_info)['total']
    print(f'你的未删除说说总条数{total_moments_count}')

    # 当前未删除说说总数为0, 直接返回
    if total_moments_count == 0:
        return None

    # 2. 获取所有说说数据（断点续传，按页码 pos 续）
    print("开始获取所有未删除说说")
    qzone_moments_all = Tools.read_txt_file(workdir, QZONE_MOMENTS_ALL)
    start_page = 0
    all_page_data = []
    resumed = False
    if resumable is not None:
        if fresh:
            resumable.clear_checkpoint()
        else:
            start_page = resumable.checkpoint_pos() + 1
            if start_page > 0:
                all_page_data = resumable.load_texts()
                resumed = True
                print(f"从断点第 {start_page} 页继续（已完成 {len(all_page_data)} 条）")
    if resumed or not qzone_moments_all:  # 完整缓存命中则直接跳过抓取
        default_page_size = 30
        total_page_num = math.ceil(total_moments_count / default_page_size)
        seen_tids = {item.get('tid') for item in all_page_data if item.get('tid')}
        for current_page_num in range(start_page, total_page_num):
            pos = current_page_num * default_page_size
            qq_userinfo_response = get_user_qzone_info(session, default_page_size, pos)
            if qq_userinfo_response is None:
                if resumable is not None:
                    resumable.record_skip(current_page_num, "empty", "msglist 页返回 None")
                    print(f"第 {current_page_num} 页失败，已记入 skips，继续")
                continue
            current_page_data = json.loads(qq_userinfo_response)["msglist"]
            if current_page_data:
                # 断点在 append 与 checkpoint 落盘之间中断会重复一页，按 tid 去重兜底
                new_items = [it for it in current_page_data
                             if not it.get('tid') or it.get('tid') not in seen_tids]
                for it in new_items:
                    if it.get('tid'):
                        seen_tids.add(it['tid'])
                if new_items:
                    all_page_data.extend(new_items)
                    if resumable is not None:
                        resumable.append_texts(new_items)
                        resumable.save_checkpoint(current_page_num)
            time.sleep(0.02)
        Tools.write_txt_file(workdir, QZONE_MOMENTS_ALL,
                             json.dumps({"msglist": all_page_data}, ensure_ascii=False, indent=2))
        qzone_moments_all = Tools.read_txt_file(workdir, QZONE_MOMENTS_ALL)
        if resumable is not None:
            resumable.clear_checkpoint()  # 全量完成，断点使命结束

    if not Tools.is_valid_json(qzone_moments_all):
        print("获取QQ空间说说失败")
        return None
    qzone_moments_list = json.loads(qzone_moments_all)['msglist']
    print(f'已获取到数据的说说总条数{len(qzone_moments_list)}')

    # 3. 添加说说列表
    texts = []
    for item in tqdm(qzone_moments_list, desc="获取未删除说说", unit="条"):
        content = item['content'] or ""
        nickname = item['name']
        create_time = Tools.format_timestamp(item['created_time'])
        picture_urls = [pic['url1'] for pic in item.get('pic', [])]
        picture_urls += [video['url1'] for video in item.get('video', [])]
        pictures = ",".join(picture_urls)

        comments = [
            # 时间，内容，昵称，QQ号
            [c['createTime2'], c['content'], c['name'], c['uin']]
            for c in item.get('commentlist', [])
        ]

        # 格式：时间、内容、图片链接、评论
        row = [create_time, f"{nickname} ：{content}", pictures, comments]
        if store is not None:
            store.upsert_feed(item.get('tid'), source="taotao",
                              time_s=create_time, content=row[1],
                              pictures=pictures, comments=comments,
                              raw_json=json.dumps(item, ensure_ascii=False))
        texts.append(row)
    return texts


# 获取用户QQ空间相关信息
def get_user_qzone_info(session, page_size, offset=0):
    url = 'https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6'

    params = {
        'uin': str(session.uin),
        'ftype': '0',
        'sort': '0',
        'pos': str(offset),
        'num': str(page_size),
        'replynum': '100',
        'g_tk': str(session.g_tk),
        'callback': '_preloadCallback',
        'code_version': '1',
        'format': 'jsonp',
        'need_private_comment': '1'
    }
    try:
        response = requests.get(url, headers=session.taotao_headers(), params=params)
    except requests.RequestException as e:
        print(e)
        return None
    raw_response = response.text
    # 响应是 jsonp 包裹 _preloadCallback(...)，剥掉外壳只留里面的 JSON
    raw_txt = re.sub(r'^_preloadCallback\((.*)\);?$', r'\1', raw_response, flags=re.S)
    json_dict = json.loads(raw_txt)
    if json_dict['code'] != 0:
        print(f"错误 {json_dict['message']}")
        return None
    return json.dumps(json_dict, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    get_visible_moments_list()
