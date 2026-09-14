import ast
import configparser
import json
import os

config = configparser.ConfigParser()
config.read('./resource/config/config.ini')

temp_path = config.get('File', 'temp')
user_path = config.get('File', 'user')
result_path = config.get('File', 'result')


def save_user(cookies):
    with open(user_path + cookies.get('uin'), 'w') as f:
        json.dump(dict(cookies), f, ensure_ascii=False, indent=2)


def init_flooder():
    # 初始化temp文件夹
    if not os.path.exists(temp_path):
        os.makedirs(temp_path)
        print(f"Created directory: {temp_path}")

    # 初始化user文件夹
    if not os.path.exists(user_path):
        os.makedirs(user_path)
        print(f"Created directory: {user_path}")

    # 初始化result文件夹
    if not os.path.exists(result_path):
        os.makedirs(result_path)
        print(f"Created directory: {result_path}")


def _load_cookie_file(file_path):
    """读 cookie 文件：JSON 优先；旧版 str(dict) 明文格式一次性迁移为 JSON。"""
    with open(file_path, 'r') as file:
        content = file.read()
    if not content.strip():
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 旧格式是 repr 出来的 python dict 字面量，literal_eval 只解析字面量、不执行代码
        data = ast.literal_eval(content)
        with open(file_path, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def read_files_in_folder():
    # 获取文件夹下的所有文件
    files = os.listdir(user_path)
    # 如果文件夹为空
    if not files:
        return None
    # 输出文件列表
    print("已登录用户列表:")
    for i, file in enumerate(files):
        print(f"{i + 1}. {file}")

    # 选择文件
    while True:
        try:
            choice = int(input("请选择要登录的用户序号，重新登录输入0: "))
            if 1 <= choice <= len(files):
                break
            elif choice == 0:
                return None
            else:
                print("无效的选择，请重新输入。")
        except ValueError:
            print("无效的选择，请重新输入。")

    # 读取选择的文件
    selected_file = files[choice - 1]
    file_path = os.path.join(user_path, selected_file)
    return _load_cookie_file(file_path)
