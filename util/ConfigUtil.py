import ast
import configparser
import json
import os

config = configparser.ConfigParser()
_found = config.read('./resource/config/config.ini')
if not _found or not config.has_section('File'):
    # 未配置（装成 tool 后 cwd 下没有 resource/）：整包建在 cwd 的 resource/ 里，
    # 用户在哪运行，哪就是仓库根。config.ini 模板里写的也是相对路径，语义一致。
    config.read_dict({'File': {'temp': './resource/temp/', 'user': './resource/user/',
                               'result': './resource/result/'}})

temp_path = config.get('File', 'temp')
user_path = config.get('File', 'user')
result_path = config.get('File', 'result')


def save_user(cookies):
    with open(os.path.join(user_path, cookies.get('uin')), 'w') as f:
        json.dump(dict(cookies), f, ensure_ascii=False, indent=2)


def ensure_dirs():
    for path in (temp_path, user_path, result_path):
        os.makedirs(path, exist_ok=True)


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


def select_saved_login(name=None):
    files = os.listdir(user_path)
    if not files:
        return None
    if name:
        # --user 指定了就直接用：无 TTY（后台/重定向）时 input() 会 EOFError
        if name not in files:
            raise FileNotFoundError(f"未找到登录态 {os.path.join(user_path, name)}（现有：{files}）")
        return _load_cookie_file(os.path.join(user_path, name))
    print("已登录用户列表:")
    for i, file in enumerate(files):
        print(f"{i + 1}. {file}")

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

    return _load_cookie_file(os.path.join(user_path, files[choice - 1]))
