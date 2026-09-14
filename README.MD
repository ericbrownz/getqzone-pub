# getqzone

Logs in to QQ Zone via QR code, pulls your historical interaction feed and visible moments, and exports them to Excel, an offline HTML page, and local images. Every batch is written to a local SQLite store as it arrives, so an interrupted run loses nothing.

Fork of [GetQzonehistory](https://github.com/LibraHp/GetQzonehistory) v1.0.0, released here as v1.0.1. Upstream was archived on 2026-09-04 — its source removed, and its README left pointing at [Gaoshu705/QzoneArchive](https://github.com/Gaoshu705/QzoneArchive), a Rust/Tauri + Vue 3 desktop app that lost its source the same day. This fork is the surviving copy of the original Python CLI. Architectural ideas from QzoneArchive (SQLite archive store, cursor-based pagination, rate-limit windows, skip-and-retry on bad pages) are carried over here.

## Outputs

Written to `resource/result/<your-qq>/`:

| File | Content |
| --- | --- |
| `*_全部列表.xlsx` | every captured post |
| `*_说说列表.xlsx` | your own moments |
| `*_转发列表.xlsx` | forwarded moments |
| `*_留言列表.xlsx` | received messages |
| `*_其他列表.xlsx` | other activity |
| `*_好友列表.xlsx` | friends seen while scraping |
| `*_说说网页版.html` | moments rendered as a web page |
| `pic/` | offline-downloaded images |

Only content present in the feed / visible moments is available; posts visible only to yourself cannot be fetched.

## Fixes & improvements (vs upstream)

- Parse every entry per batch — upstream extracted only the first `html` field from each 10-item response
- O(n) dedup by `published time (nearest minute) + content`, replacing the old O(n²) pairwise comparison
- Strip `赞了 / 评论了 / 回复了 / 留言 / 赞过 / 觉得很赞 / 访问了空间…` prefixes to restore deleted content, keeping the forward chain
- Remove QQ anti-crawl hidden characters (e.g. `<i class="ui-mr8">`) by content length so time labels survive
- Normalize image URLs (`\/`, `&amp;`) to fix "Invalid URL" download errors
- Fix garbled friend nicknames
- Decode as UTF-8 with `errors="replace"`, so a bad byte no longer fails the whole page
- Random 3.5–4.5 s delay before each request to avoid IP bans
- QR decode now uses `zxing-cpp`; zbar is deprecated, no `DYLD_LIBRARY_PATH` needed

### Fork additions

- **SQLite store** (`resource/store/<qq>.db`) instead of in-memory lists: Excel and HTML are export views over the database, so a crash or Ctrl-C mid-run costs nothing
- **Resume by offset** — checkpoints per stream; bad pages are recorded to `skips.jsonl` and retried once the main loop finishes
- **One row per original moment** — the PC interaction feed is aggregated back into the posts it belongs to, with interactors listed underneath instead of repeated per like/comment
- **Action words preserved** (`赞了` / `评论` / `回复了` …) so you can see *how* someone interacted, not just that they did
- **Optional `mobile` source** (`get_feeds`) as an alternative or complement to the PC feed
- **`--deep`**: sparse scan of the deep band (offset ~20500–21500) for interaction traces of moments deleted years ago — the band serves probabilistically and throttles harder than the normal feed, so it has its own rate window and its own skip records

## Known limitations

- Very old (2007–2010) space logs may have unrecoverable encoding
- Some emoji are lost (`errors="replace"` turns them into `�`)
- Dead original image links cannot be recovered

## Usage

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/) (recommended) or pip.

```shell
git clone git@github.com:ericbrownz/getqzone.git
cd getqzone

uv venv .
uv pip install -r requirements.txt   # or: python -m venv .venv && pip install -r requirements.txt

python main.py
```

Scan the QR code with your phone to log in; scraping and export run automatically. On later runs you can pick a saved login or enter `0` to re-scan.

### main.py options

Everything has a working default — plain `python main.py` is a full run.

| Flag | Default | Effect |
| --- | --- | --- |
| `--source {pc,mobile,both}` | `pc` | Which interaction stream to pull. `pc` is the feed the exports are built around; `mobile` (`get_feeds`) is an alternative/complement; `both` runs both |
| `--fresh` | off | Ignore saved checkpoints and refetch from the start |
| `--rate N` | `300` | Request budget: at most N pages per 10-minute window (the PC source also waits 3.5–4.5 s between pages) |
| `--max-batches N` | no limit | Stop after N batches. The checkpoint is kept — rerun to continue |
| `--max-pages N` | until `hasmore=0` | Page cap, `mobile` source only |
| `--deep` | off | After the normal feed, scan the sparse deep band (offset ~20500–21500) for interaction traces of moments deleted years ago. `pc`/`both` only |
| `--deep-rate N` | `60` | Separate request budget for the deep band, which throttles harder than the normal feed |
| `--deep-offsets "a,b,c"` | 9 built-in cells | Override the deep-band offset grid |
| `--no-open` | off | Don't open the result folder when the export finishes |

Common runs:

```shell
python main.py                    # normal run: resume, export, open the result folder
python main.py --source both      # PC feed + get_feeds
python main.py --max-batches 20   # short run to try it out; rerun to continue
python main.py --deep             # feed first, then the deep-band backfill
```

State lives in `resource/store/<qq>.db` (moments, interactors, friends) plus checkpoints and `skips.jsonl` under `resource/temp/`. Exports are views over that store, so deleting the database is what starts a scrape over from scratch.

Design notes and probe scripts live in [docs/](docs/) and [test/](test/).

---

<details>
<summary>GetQzonehistory v1.0.0 (original README)</summary>

# GetQzonehistory(获取qq发布的历史说说)

已经打包好的单文件可以直接执行，在release页面。


<details><summary><font color="#FF0000">免责声明【必读】</font></summary>

本工具仅供学习和技术研究使用，不得用于任何商业或非法行为，否则后果自负。

本工具的作者不对本工具的安全性、完整性、可靠性、有效性、正确性或适用性做任何明示或暗示的保证，也不对本工具的使用或滥用造成的任何直接或间接的损失、责任、索赔、要求或诉讼承担任何责任。

本工具的作者保留随时修改、更新、删除或终止本工具的权利，无需事先通知或承担任何义务。

本工具的使用者应遵守相关法律法规，尊重QQ的版权和隐私，不得侵犯QQ或其他第三方的合法权益，不得从事任何违法或不道德的行为。

本工具的使用者在下载、安装、运行或使用本工具时，即表示已阅读并同意本免责声明。如有异议，请立即停止使用本工具，并删除所有相关文件。

</details>


该项目通过获取QQ空间的历史消息列表来获取该账号下发布的所有说说（当然消息列表中没有的就获取不到，例如一些仅自己可见的说说）[B站有详细食用教程](https://space.bilibili.com/1117414477)

主要实现还是通过模拟登录QQ空间来获取历史消息列表，然后进行数据分析，最后将爬取的说说存放到/resource/result目录下

由于对python编程还不是很熟悉，所以代码有很多疏漏，可以通过自己的想法来完善代码
## 目录结构

```text
project/
├── resource/                # 资源目录
│   ├── config/              # 配置目录，文件保存位置配置
│   │   └── config.ini
│   ├── result/              # 导出结果的目录，格式为“你的qq.xlsx”
│   │   ├── ...
│   │   └── ...
│   ├── temp/                # 缓存目录
│   │   ├── ...
│   │   └── ...
│   ├── user/                # 用户信息
│   │   ├── ...
│   │   └── ...
├── util/                    # 单元工具目录
│   ├── ConfigUtil.py        # 读取配置
│   ├── GetAllMomentsUtil.py # 获取未删除的所有说说
│   ├── LoginUtil.py         # 登录相关
│   ├── RequestUtil.py       # 请求数据相关
│   └── ToolsUtil.py         # 工具
├── main.py                  # 主程序入口
├── fetch_all_message.py     # 主程序入口
├── README.md                # 项目说明文件
├── requirements.txt         # 依赖项列表
└── LICENSE                  # 许可证文件
```

## 安装

#### 使用虚拟环境（推荐）
```bash
# 克隆储存库
git clone https://github.com/LibraHp/GetQzonehistory.git
# 打开目录
cd GetQzonehistory
# 创建名为 myenv 的虚拟环境
python -m venv myenv
# 激活虚拟环境。在终端或命令提示符中运行以下命令：
# 对于 Windows：
.\myenv\Scripts\activate
# 对于 macOS/Linux：
source myenv/bin/activate
# 安装依赖
pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
# 运行脚本
python main.py
```
#### 使用本机环境（不推荐）
```bash
# 克隆储存库
git clone https://github.com/LibraHp/GetQzonehistory.git
# 打开目录
cd GetQzonehistory
# 安装依赖
pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
# 运行脚本
python main.py
```


## 参考

登录方法参考自
[python-QQ空间扫码登录](https://blog.csdn.net/m0_50153253/article/details/113780595)

</details>

