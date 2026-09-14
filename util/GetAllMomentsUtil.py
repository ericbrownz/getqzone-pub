import json
import math
import re
import time

import requests
from tqdm import tqdm

import util.SessionUtil as SessionUtil
import util.ToolsUtil as Tool

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

    # 1. 获取说说总条数
    user_qzone_info = Tool.read_txt_file(workdir, USER_QZONE_INFO)
    if not user_qzone_info:
        # 样本缓存未找到，开始请求获取样本
        qq_userinfo_response = get_user_qzone_info(session, 1)
        Tool.write_txt_file(workdir, USER_QZONE_INFO, qq_userinfo_response)
        user_qzone_info = Tool.read_txt_file(workdir, USER_QZONE_INFO)

    if not Tool.is_valid_json(user_qzone_info):
        print("获取QQ空间信息失败")
        return None
    json_dict = json.loads(user_qzone_info)
    total_moments_count = json_dict['total']
    print(f'你的未删除说说总条数{total_moments_count}')

    # 当前未删除说说总数为0, 直接返回
    if total_moments_count == 0:
        return None

    # 2. 获取所有说说数据（C1：断点续传，按页码 pos 续）
    print("开始获取所有未删除说说")
    qzone_moments_all = Tool.read_txt_file(workdir, QZONE_MOMENTS_ALL)
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
    if qzone_moments_all and not resumed:
        pass  # 完整缓存命中，直接用
    else:
        default_page_size = 30  # 默认一页30条
        total_page_num = math.ceil(total_moments_count / default_page_size)  # 总页数
        seen_tids = {item.get('tid') for item in all_page_data if item.get('tid')}
        for current_page_num in range(start_page, total_page_num):
            # 数据偏移量
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
        qq_userinfo = json.dumps({"msglist": all_page_data}, ensure_ascii=False, indent=2)
        Tool.write_txt_file(workdir, QZONE_MOMENTS_ALL, qq_userinfo)
        qzone_moments_all = Tool.read_txt_file(workdir, QZONE_MOMENTS_ALL)
        if resumable is not None:
            resumable.clear_checkpoint()  # 全量完成，断点使命结束

    if not Tool.is_valid_json(qzone_moments_all):
        print("获取QQ空间说说失败")
        return None
    json_dict = json.loads(qzone_moments_all)
    qzone_moments_list = json_dict['msglist']
    print(f'已获取到数据的说说总条数{len(qzone_moments_list)}')

    # 3. 添加说说列表
    texts = []
    for item in tqdm(qzone_moments_list, desc="获取未删除说说", unit="条"):
        content = item['content'] if item['content'] else ""
        nickname = item['name']
        create_time = Tool.format_timestamp(item['created_time'])
        pictures = ""
        # 如果有图片
        if 'pic' in item:
            for index, picture in enumerate(item['pic']):
                pictures += picture['url1'] + ","
        if 'video' in item:
            for index, picture in enumerate(item['video']):
                pictures += picture['url1'] + ","

        # 去除最后一个逗号
        pictures = pictures[:-1] if pictures != "" else pictures
        comments = []
        if 'commentlist' in item:
            for index, commentToMe in enumerate(item['commentlist']):
                comment_content = commentToMe['content']
                comment_create_time = commentToMe['createTime2']
                comment_nickname = commentToMe['name']
                comment_uin = commentToMe['uin']
                # 时间，内容，昵称，QQ号
                comments.append([comment_create_time, comment_content, comment_nickname, comment_uin])

        # 格式：时间、内容、图片链接、转发内容、评论内容
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
        'uin': f'{session.uin}',
        'ftype': '0',
        'sort': '0',
        'pos': f'{offset}',
        'num': f'{page_size}',
        'replynum': '100',
        'g_tk': f'{session.g_tk}',
        'callback': '_preloadCallback',
        'code_version': '1',
        'format': 'jsonp',
        'need_private_comment': '1'
    }
    try:
        response = requests.get(url, headers=session.taotao_headers(), params=params)
    except Exception as e:
        print(e)
        return None
    rawResponse = response.text
    # 使用正则表达式去掉 _preloadCallback()，并提取其中的 JSON 数据
    raw_txt = re.sub(r'^_preloadCallback\((.*)\);?$', r'\1', rawResponse, flags=re.S)
    # 再转一次是为了去掉响应值本身自带的转义符http:\/\/
    json_dict = json.loads(raw_txt)
    if json_dict['code'] != 0:
        print(f"错误 {json_dict['message']}")
        return None
    return json.dumps(json_dict, indent=2, ensure_ascii=False)


if __name__ == '__main__':
    get_visible_moments_list()
