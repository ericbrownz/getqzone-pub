"""网页版导出：把「原动态」列表渲染成一个自包含的 HTML 文件（不联网、不读全局）。

模板与渲染器同文件——class/占位符必须与渲染结构同步改，分开放会漏。
"""
import html
import re
from urllib.parse import unquote

import util.RedownloadUtil as Redownload
import util.ToolsUtil as Tools


def get_html_template():
    """网页版的四份模板：外壳 / 说说 / 小框 / 中框。

    外壳用 `{tabs}`/`{posts}` 哨兵占位、`str.replace` 填，不用 `.format()`：外壳里大段 CSS/JS，
    `.format()` 会逼着每个 `{` 写成 `{{`。
    """
    html_template = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>QQ空间动态</title>
        <style>
            :root {
                --rail-w: 58px;   /* 右侧日期导航的宽度，正文靠 body 的 padding-right 让开 */
                /* Chrome/macOS 的覆盖式滚动条平时隐形、一靠近就浮出来，正好压在贴边的
                   拖动球上（球在 right:1px）。把它算进右边距，让导航整体让开这条「滚动条
                   车道」——球的大小不动，只是不再和滚动条抢同一块像素。
                   scrollbar-gutter 在有经典滚动条的平台上留出等宽空间；覆盖式的平台上
                   该值恒为 0，故取两者较大值。 */
                --scrollbar-w: max(0px, 15px);
            }
            * {
                box-sizing: border-box;
            }
            body {
                font-family: Arial, sans-serif;
                background-color: #f5f5f5;
                margin: 0;
                padding-right: calc(var(--rail-w) + var(--scrollbar-w));
            }
            /* 大类标签：吸顶，滚到哪儿都能切 */
            #cats {
                position: sticky;
                top: 0;
                z-index: 30;
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                padding: 10px 20px;
                background-color: rgba(245, 245, 245, 0.95);
                border-bottom: 1px solid #e0e0e0;
            }
            #cats button {
                font: inherit;
                font-size: 0.9em;
                padding: 5px 14px;
                border: 1px solid #ccc;
                border-radius: 999px;
                background-color: #fff;
                color: #444;
                cursor: pointer;
            }
            #cats button[aria-pressed="true"] {
                background-color: #333;
                border-color: #333;
                color: #fff;
            }
            #cats .n {
                margin-left: 5px;
                opacity: 0.55;
                font-size: 0.9em;
            }
            .post {
                background-color: #333;
                color: #fff;
                padding: 20px;
                margin: 20px;
                border-radius: 10px;
            }
            .avatar {
                float: left;
                margin-right: 20px;
            }
            .avatar img {
                width: 50px;
                height: 50px;
                border-radius: 50%;
            }
            .content {
                overflow: hidden;
                /* 让整个正文区（含行内昵称气泡）压在后面的 .comments 之上——气泡朝下弹，
                   正好落在点赞框的地盘上，而 .comments 是 .content 的后继兄弟、默认后画。 */
                position: relative;
                z-index: 20;
            }
            .nickname {
                font-size: 1.2em;
                font-weight: bold;
            }
            .time {
                color: #999;
                font-size: 0.9em;
            }
            .message {
                margin-top: 10px;
                font-size: 1.1em;
            }
            /* 正文里的裸链接（转发卡片带的多）：默认是 `overflow-wrap:break-word` 都救不了的
               超长串（最长 437 字符），一挂上去就撑破右栏。渲染时统一换成 a.link —— 只显示
               「站名 + 省略标题」，整串进 title，鼠标悬停才现身。
               display:block + width:fit-content：换到正文下方独占一行，不追着文字尾巴。 */
            .message a.link {
                display: block;
                width: fit-content;
                max-width: min(100%, 46ch);
                margin-top: 6px;
                padding: 3px 9px;
                border: 1px solid #666;
                border-radius: 6px;
                background-color: #2b2b2b;
                color: #9cdcfe;
                font-size: 0.9em;
                text-decoration: none;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .message a.link:hover {
                background-color: #3a3a3a;
            }
            /* 正文里的原po昵称，照空间原版：蓝字带下划线、无底色。字号字重跟正文走——
               它内联在句子里，跟卡片头的 .nickname（1.2em/粗体）不一样。
               昵称本身可能带冒号（`magnet:?xt=urn:btih: ☭☭☭☭☭`），不能按冒号切，只能用
               服务端标好的 q_namecard 边界（parse_li 落库时裹的哨兵）。
               转发语不同色——它是本人写的字，不是别人的名字。 */
            .message .msg-name {
                color: #5aa9e6;
                font-size: inherit;
                font-weight: inherit;
                text-decoration: underline;
                text-underline-offset: 2px;
                position: relative;   /* 气泡的定位基准 */
            }
            .message .msg-name[data-qq] {
                cursor: copy;
            }
            .message a.link::after {
                content: " ↗";
                opacity: 0.6;
            }
            .image {
                margin-top: 10px;
                display: grid;
                grid-template-columns: repeat(3, 1fr); /* 将图片分成3列 */
                grid-gap: 10px; /* 设置图片之间的间距 */
                justify-items: center; /* 居中显示图片 */
            }
            .image img {
                width: 100%; /* 图片宽度100%填充父容器 */
                height: auto; /* 固定高度150px */
                object-fit: cover; /* 保持比例裁剪图片 */
                max-width: 33vw; /* 限制图片的最大宽度 */
                max-height: 33vh; /* 限制图片的最大高度 */
                border-radius: 10px;
                cursor: pointer;
            }
            .comments {
                margin-top: 5px; /* 调整这里的值来减少间距 */
                margin-bottom: 10px;
                background-color: #444;
                padding: 2px 10px 10px 10px;
                border-radius: 10px;
            }
            .box-title {
                margin-top: 8px;
                color: #bbb;
                font-size: 0.85em;
            }
            .comment {
                margin-top: 10px; /* 调整单个评论之间的间距 */
                padding: 10px;
                background-color: #555;
                border-radius: 10px;
                color: #fff;
            }
            .comment .avatar {
                /* 30px 的小头像仍吃 .avatar 那 20px 右边距，名字被推远；连同行内布局下
                   模板换行产生的空白一起收掉。 */
                margin-right: 10px;
            }
            .comment .avatar img {
                width: 30px;
                height: 30px;
            }
            .comment .nickname {
                font-size: 1em;
                font-weight: bold;
            }
            /* 只有名字和头像该响应点击/悬停——.nickname 是块级 div、.avatar 是浮动，
               默认整行都会命中，鼠标滑到那行空白也弹气泡。
               用 inline-block 而非「块级 + width:fit-content」：.comment 的 .avatar 左浮动且
               没有 BFC，收窄的块盒放不进浮动旁边的余量时会被挤到下一行（名字掉到头像下面），
               inline-block 参与行内布局，跟着浮动文字流走。
               名字改行内后不再撑满整行，头像必须自己挂 data-qq 才有点击/悬停区（气泡挂 img 上）。 */
            .comment .nickname,
            .content > .nickname {
                display: inline-block;
                width: fit-content;
            }
            /* 名字改行内后不再撑满整行，头像得自己挂 data-qq 才有点击/悬停区 */
            .comment .nickname[data-qq],
            .comment .avatar[data-qq],
            .content > .nickname[data-qq] {
                cursor: copy;
            }
            .comment .time {
                font-size: 0.8em;
                color: #aaa;
            }
            /* 匿名点赞说明卡没有时间行，卡片只剩一行昵称高（42px），比实名点赞卡（60px）矮
               一截，整列扫下来忽高忽低；补到同高。 */
            .comment.anon {
                min-height: 60px;
            }
            /* 气泡朝下左——作者昵称在 .content{overflow:hidden} 里，朝上会被裁；
               朝下则贴着名字走，不挡上方的头像。
               选择器用裸 [data-qq]：挂它的既有昵称也有头像（评论头像要点得到）。 */
            [data-qq]:not(.post) {
                position: relative;
            }
            [data-qq]::after {
                content: "QQ " attr(data-qq);
                position: absolute;
                top: calc(100% + 6px);
                left: 0;
                padding: 2px 8px;
                border-radius: 4px;
                background-color: #000;
                color: #fff;
                font-size: 0.75em;
                font-weight: normal;
                white-space: nowrap;
                opacity: 0;
                visibility: hidden;
                pointer-events: none;   /* 否则重复点击会落在伪元素上 */
                transition: opacity 0.15s ease;
                z-index: 10;
            }
            [data-qq]:hover::after,
            [data-qq]:focus-visible::after {
                opacity: 1;
                visibility: visible;
            }
            /* 复制成功：气泡就地变成对勾，比换成一句「已复制」短，也更少动眼睛。
               保持可见（点击后鼠标还在原地，气泡不该突然消失）。 */
            [data-copied]::after {
                content: "✓ 已复制";
                background-color: #2e7d32;
                opacity: 1;
                visibility: visible;
            }
            /* 右侧日期导航：刻度按**可见帖子的序号**排布（不是按时间线性），
               这样拖动条的哪个位置，就跳到那个位置的帖子——与手机版空间的手感一致。 */
            #rail {
                position: fixed;
                top: 0;
                right: var(--scrollbar-w);
                bottom: 0;
                width: var(--rail-w);
                z-index: 40;
                user-select: none;
                -webkit-user-select: none;
                touch-action: none;
            }
            #rail-track {
                position: absolute;
                top: 24px;
                bottom: 24px;
                right: 4px;
                width: 50px;
                cursor: grab;
            }
            #rail-track.dragging {
                cursor: grabbing;
            }
            #rail .tick {
                position: absolute;
                right: 0;
                pointer-events: none;
                white-space: nowrap;
            }
            /* 月份刻度：短横线 */
            #rail .tick.month {
                width: 9px;
                height: 2px;
                border-radius: 1px;
                background-color: #ccc;
            }
            /* 年份刻度：文字 */
            #rail .tick.year {
                right: 13px;
                font-size: 11px;
                font-weight: bold;
                color: #777;
            }
            #rail-knob {
                position: absolute;
                right: 1px;
                width: 20px;
                height: 20px;
                margin-top: -10px;
                border-radius: 50%;
                background-color: #333;
                opacity: 0.85;
                pointer-events: none;
            }
            #rail-bubble {
                position: fixed;
                right: calc(var(--rail-w) + var(--scrollbar-w) + 6px);
                padding: 5px 11px;
                border-radius: 8px;
                background-color: #333;
                color: #fff;
                font-size: 0.85em;
                white-space: nowrap;
                opacity: 0;
                pointer-events: none;
                transition: opacity 0.12s ease;
            }
            #rail-bubble.on {
                opacity: 1;
            }
            #to-top {
                position: fixed;
                right: calc(var(--rail-w) + var(--scrollbar-w) + 8px);
                bottom: 24px;
                width: 38px;
                height: 38px;
                border: none;
                border-radius: 50%;
                background-color: rgba(51, 51, 51, 0.8);
                color: #fff;
                font-size: 17px;
                cursor: pointer;
                z-index: 35;
            }
        </style>
    </head>
    <body>

        <header id="cats">{tabs}</header>
        <main id="posts">{posts}</main>
        <aside id="rail">
            <div id="rail-track">
                <div id="rail-ticks"></div>
                <div id="rail-knob"></div>
            </div>
            <div id="rail-bubble"></div>
        </aside>
        <button id="to-top" type="button" title="回到顶部" hidden>↑</button>
        <script>
            // 为所有图片添加点击事件
            document.querySelectorAll(".image img").forEach(img => {
                img.addEventListener("click", function() {
                    window.open(this.src, '_blank');  // 打开图片链接并在新标签页中展示
                });
            });

            // file:// 下 Safari/WebKit 不暴露 Clipboard API，execCommand 才是主路径，
            // 必须在点击手势内同步调。
            function copyQQ(text) {
                var ta = document.createElement("textarea");
                ta.value = text;
                ta.setAttribute("readonly", "");
                ta.style.position = "fixed";
                ta.style.top = "-1000px";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.select();
                var ok = false;
                try {
                    ok = document.execCommand("copy");
                } catch (err) {
                    ok = false;
                }
                document.body.removeChild(ta);
                return ok;
            }

            // 委托在 body 上——昵称数量与互动数同阶，不逐元素 onclick
            document.body.addEventListener("click", function(e) {
                var el = e.target && e.target.closest
                    ? e.target.closest("[data-qq]") : null;
                if (!el) return;
                var qq = el.getAttribute("data-qq");
                if (!qq) return;
                // 复制成功就地给回执：气泡文字换成对勾，1.4s 后恢复。没有回执时用户
                // 分不出「复制到了」和「没反应」（file:// 下 API 会静默失败）。
                var mark = function(ok) {
                    if (!ok) return;
                    if (el.dataset.copied) return;   // 连点不叠定时器
                    el.dataset.copied = "1";
                    clearTimeout(el._copyTimer);
                    el._copyTimer = setTimeout(function() {
                        delete el.dataset.copied;
                    }, 1400);
                };
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(qq)
                        .then(function() { mark(true); })
                        .catch(function() { mark(copyQQ(qq)); });
                } else {
                    mark(copyQQ(qq));  // 同步：仍在点击手势内
                }
            });

            var allPosts = [].slice.call(document.querySelectorAll("#posts .post"));
            var header = document.getElementById("cats");
            var railTrack = document.getElementById("rail-track");
            var railTicks = document.getElementById("rail-ticks");
            var railKnob = document.getElementById("rail-knob");
            var railBubble = document.getElementById("rail-bubble");
            var toTop = document.getElementById("to-top");

            function visiblePosts() {
                return allPosts.filter(function(p) { return !p.hidden; });
            }

            // 拖动条的纵向位置 <-> 可见帖子的序号（0 = 最新一条）
            function indexFromClientY(clientY) {
                var posts = visiblePosts();
                if (!posts.length) return 0;
                var box = railTrack.getBoundingClientRect();
                var ratio = (clientY - box.top) / box.height;
                ratio = Math.max(0, Math.min(1, ratio));
                return Math.round(ratio * (posts.length - 1));
            }

            function knobTopPercent(index, total) {
                return (total ? (index / total) * 100 : 0) + "%";
            }

            function scrollToIndex(index, behavior) {
                var posts = visiblePosts();
                var post = posts[Math.min(index, posts.length - 1)];
                if (!post) return;
                window.scrollTo({
                    top: post.getBoundingClientRect().top + window.scrollY
                         - header.offsetHeight - 8,
                    behavior: behavior || "auto"
                });
            }

            // 年份/月份刻度：只在「第一次出现该年/月」的那条帖子上打点
            function rebuildRail() {
                var posts = visiblePosts();
                railTicks.textContent = "";
                document.getElementById("rail").hidden = posts.length === 0;
                toTop.hidden = window.scrollY < 400;
                var lastMonth = null;
                var seenYears = {};
                posts.forEach(function(post, i) {
                    var year = post.dataset.year;
                    var month = post.dataset.month;
                    if (!year) return;
                    var monthKey = year + "-" + month;
                    if (monthKey === lastMonth) return;
                    lastMonth = monthKey;
                    var tick = document.createElement("div");
                    tick.style.top = (i / posts.length) * 100 + "%";
                    if (seenYears[year]) {
                        tick.className = "tick month";
                    } else {
                        seenYears[year] = true;
                        tick.className = "tick year";
                        tick.textContent = year;
                    }
                    railTicks.appendChild(tick);
                });
                syncKnob();
            }

            // 滚到哪儿，圆点跟到哪儿：二分找第一条还露在吸顶栏下面的帖子
            function syncKnob() {
                var posts = visiblePosts();
                if (!posts.length) return;
                var line = header.offsetHeight + 1;
                var lo = 0;
                var hi = posts.length - 1;
                var found = 0;
                while (lo <= hi) {
                    var mid = (lo + hi) >> 1;
                    if (posts[mid].getBoundingClientRect().bottom > line) {
                        found = mid;
                        hi = mid - 1;
                    } else {
                        lo = mid + 1;
                    }
                }
                railKnob.style.top = knobTopPercent(found, posts.length);
                toTop.hidden = window.scrollY < 400;
            }

            var scrubbing = false;
            function scrubTo(clientY) {
                var posts = visiblePosts();
                if (!posts.length) return;
                var index = indexFromClientY(clientY);
                scrollToIndex(index);
                railKnob.style.top = knobTopPercent(index, posts.length);
                var box = railTrack.getBoundingClientRect();
                railBubble.style.top = (box.top + (index / posts.length) * box.height - 14) + "px";
                railBubble.textContent = posts[index].dataset.date || "";
                railBubble.classList.add("on");
            }

            railTrack.addEventListener("pointerdown", function(e) {
                scrubbing = true;
                railTrack.classList.add("dragging");
                if (railTrack.setPointerCapture) railTrack.setPointerCapture(e.pointerId);
                scrubTo(e.clientY);
                e.preventDefault();
            });
            railTrack.addEventListener("pointermove", function(e) {
                if (scrubbing) scrubTo(e.clientY);
            });
            ["pointerup", "pointercancel"].forEach(function(name) {
                railTrack.addEventListener(name, function() {
                    scrubbing = false;
                    railTrack.classList.remove("dragging");
                    railBubble.classList.remove("on");
                });
            });

            toTop.addEventListener("click", function() {
                window.scrollTo({ top: 0, behavior: "smooth" });
            });

            var scrollQueued = false;
            window.addEventListener("scroll", function() {
                if (scrollQueued) return;
                scrollQueued = true;
                requestAnimationFrame(function() {
                    scrollQueued = false;
                    if (!scrubbing) syncKnob();
                });
            }, { passive: true });
            window.addEventListener("resize", function() { if (!scrubbing) syncKnob(); });

            // 大类标签：点一下只留该类，右侧导航按剩下的条目重排
            var catButtons = [].slice.call(document.querySelectorAll("#cats button"));
            function setCategory(cat) {
                allPosts.forEach(function(post) {
                    post.hidden = cat !== "all" && post.dataset.cat !== cat;
                });
                catButtons.forEach(function(btn) {
                    btn.setAttribute("aria-pressed", String(btn.dataset.cat === cat));
                });
                rebuildRail();
                window.scrollTo(0, 0);
            }
            catButtons.forEach(function(btn) {
                btn.addEventListener("click", function() { setCategory(btn.dataset.cat); });
            });

            rebuildRail();
        </script>
    </body>
    </html>
    """

    post_template = """
    <div class="post" data-cat="{cat}" data-year="{year}" data-month="{month}" data-date="{date}">
        <div class="avatar">
            <img src="{avatar_url}" alt="头像">
        </div>
        <div class="content">
            <div class="nickname"{qq_attr}>{nickname}</div>
            <div class="time">{time}</div>
            <div class="message">{message}</div>
            {image}
        </div>
         {comments}
    </div>
    """

    # 小框：单个互动人。`{message}` 由调用方拼成完整 div 或空串（空 div 会白占 margin）。
    # 标签之间不留换行缩进：.nickname 改 inline-block 后，模板里的空白会渲染成一个真空格，
    # 把名字从头像旁推开。data-qq 同挂头像（名字不再撑满整行，头像得自己有点击/悬停区）。
    comment_template = """<div class="comment{cls}">
        <div class="avatar"{qq_attr}><img src="{avatar_url}" alt="评论头像"></div><div class="nickname"{qq_attr}>{nickname}</div>
        <div class="time">{time}</div>
        {message}
    </div>"""

    # 中框：一类互动（点赞 / 评论·回复）一个，内并列 N 个小框（旧版每条互动各带一个 .comments，刷屏）。
    comments_template = """
    <div class="comments">
        <div class="box-title">{title}</div>
        {items}
    </div>
    """

    return html_template, post_template, comment_template, comments_template


def split_head_body(content):
    """内容 → (作者前缀, 分隔符, 正文)。分隔符取**第一个**「：」或 `||`，谁先到算谁。

    转发说说有两代写法：`本人 转发： 原po名 ： 原文` 与 `本人 转发说说 || 原po名 ： 原文`。
    只按「：」切会把后者的原po**昵称**并进作者前缀，昵称的蓝色与悬停气泡全丢（真库 37 行）；
    `||` 是库里的内部接线，不显示。
    """
    text = str(content or "")
    i_colon = text.find("：")
    i_bar = text.find("||")
    if i_bar != -1 and (i_colon == -1 or i_bar < i_colon):
        return text[:i_bar], "||", text[i_bar + 2:]
    return text.partition("：")


def post_category(content, pictures=""):
    """网页版的大类标签：转发 / 相册 / 图片 / 说说（互斥，顺序即优先级）。

    「转发」只在**作者前缀**里找——正文提到「转发」两个字的普通说说不该被误分。
    「相册」看整串：相册动态没有冒号，整串就是作者前缀。
    """
    text = str(content or "")
    if "转发" in split_head_body(text)[0]:
        return "转发"
    if "的相册" in text:
        return "相册"
    if any(url.startswith("http") for url in str(pictures or "").split(",")):
        return "图片"
    return "说说"


# 标签页顺序；空档不渲染，省得点开什么都没有
CATEGORY_TABS = ["all", "说说", "图片", "相册", "转发"]


def render_html(posts, output_file, uin, nickname="", pic_dir=None):
    """把「原动态」列表渲染成网页版。

    posts: [{"time","content","pictures","interactors":[{name,qq,time,action,kind}],"comments":[...]}]
    互动人按 kind 分两框（点赞 / 评论·回复），内并列 N 个小框，不再「每条互动各带一个
    .comments」刷屏。

    pic_dir 给了就**优先本地**：按 URL 指纹算文件名，命中则写相对路径 `pic/xxx.jpg`，没命中
    才回落到远程 CDN。本地图是原图、也不依赖会过期的签名，故正常路径（先跑 RedownloadUtil
    落盘）下这里全部走本地。
    """
    def local(name, url, fallback=None):
        return Redownload.local_first(pic_dir, name, url, fallback)

    html_template, post_template, comment_template, comments_template = (
        get_html_template()
    )
    avatar_url = local(*Redownload.avatar(uin))
    # 互动人 uin 拿不到时（墙外存量行）用灰底占位，不能用本人头像冒充
    nameless_avatar = (
        "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
        "width='50' height='50'%3E%3Crect width='50' height='50' "
        "fill='%23888888'/%3E%3C/svg%3E"
    )

    # 表情图也走「本地优先」：gtimg 那几张小 GIF 不下到本地的话，页面永远留着远程引用
    _EMOJI_SRC = re.compile(r'src="(https?://qzonestyle\.gtimg\.cn/qzone/em/[^"]+)"')

    def em(text):
        """转义 + 表情图管线：escape → [em] 码 → <img>（本地优先）。

        调用契约：**先跑 with_links 再跑本函数**——with_links 生成的 <a> href 里的
        gtimg 域名不会撞 _EMOJI_SRC（那个只匹配 qzonestyle/em/ 路径）；反过来（先 em
        再 with_links），em 生成的 http src 会被 _URL 当裸链接再包一层，<a> 标签
        本身也会被二次转义成一串字面文本。"""
        out = html.escape(str(text), quote=False)
        out = re.sub(Redownload.EMOJI_TOKEN, Tools.replace_em_to_img, out)
        return _EMOJI_SRC.sub(
            lambda m: 'src="' + local(*Redownload.emoji(m.group(1))) + '"',
            out,
        )

    # 空间站内跳转链接只在 QQ 客户端可用，网页版挂着是死文本（相册卡片整串就是它）
    _INTERNAL_URL = re.compile(r"https?://user\.qzone\.qq\.com/\S+")
    # em() 的表情 <img> 回落远程 URL 时 src 里也是 http 串，(?<!=") 挡住属性值内的匹配
    _URL = re.compile(r'(?<!=")https?://\S+')

    def link_label(url):
        """链接的展示文字：`站名` + `·` + 路径末段（去掉 query）。太长交给 CSS 省略。"""
        bare = re.sub(r"^https?://", "", url)
        bare = bare.split("?", 1)[0].rstrip("/")
        host, _, path = bare.partition("/")
        host = host.removeprefix("www.")
        tail = unquote(path.rsplit("/", 1)[-1]) if path else ""
        if len(tail) > 36:
            tail = tail[:36] + "…"
        return f"{host} · {tail}" if tail else host

    def with_links(escaped_text):
        """在**已转义**正文里把裸链接换成紧凑的 <a>。

        转义把 URL 里的 & 变成 &amp;，matcher 会带尾缀截到转义串，先 unescape 还原
        原始 URL 再 escape 放进 href/title，恰好抵消。调用方契约：text 已过 html.escape。"""
        def sub(match):
            url = html.unescape(match.group(0))
            return (f'<a class="link" href="{html.escape(url, quote=True)}" '
                    f'title="{html.escape(url, quote=True)}" '
                    f'target="_blank" rel="noopener noreferrer">'
                    f'{html.escape(link_label(url))}</a>')
        return _URL.sub(sub, escaped_text)

    def avatar(uin):
        if not uin:
            return nameless_avatar
        # uin 来自抓取数据，进 src 属性必须转义（与 qq_attr 同标准）
        return html.escape(local(*Redownload.avatar(uin)), quote=True)

    def qq_attr(uin):
        """昵称尾部的 data-qq + tabindex。tabindex 是承重的：div 默认不可聚焦，不挂它
        :focus-visible 那条选择器就是死的。无 uin 则不挂，气泡自然不出。"""
        if not uin:
            return ""
        return f' data-qq="{html.escape(str(uin), quote=True)}" tabindex="0"'

    def card(uin, name, time_str, message, anon=False):
        message = em(message or "")
        # 昵称/message 都来自抓取的外部文本，em() 内已转义；时间串也是外部数据，单独转义。
        return comment_template.format(
            avatar_url=avatar(uin),
            nickname=em(str(name or uin or "?")),
            time=html.escape(str(time_str or ""), quote=False),
            message=f'<div class="message">{message}</div>' if message else "",
            qq_attr=qq_attr(uin),
            cls=" anon" if anon else "",
        )

    post_html = ""
    category_counts = {key: 0 for key in CATEGORY_TABS}
    category_counts["all"] = len(posts)
    for p in posts:
        time_str = str(p["time"] or "").strip()
        if not time_str:
            continue
        author, sep, body = split_head_body(p["content"])
        # 相册/转发卡片行没有「：」，整串落进 author——里面的空间导航链接只在 qq 客户端
        # 有用，网页版挂着是死文本，剥掉（库与 Excel 不动，仅显示层）。
        author_name = _INTERNAL_URL.sub("", author).strip() or nickname or ""
        # 作者前缀本身也可能被服务端裹了哨兵（它也是 q_namecard），显示前剥掉
        shown_name = em(Tools.strip_names(author_name))
        # 正文的站内链接剥掉，外链换成 .link（顺序见 with_links 注释），昵称哨兵换成胶囊。
        # 正文的原po昵称：蓝字下划线（空间原版的样子），有 uin 的挂气泡可悬停看/点击复制 QQ 号。
        # 顺序：剥站内链接 → em()（转义+表情图，src 已本地化成相对路径）→ with_links。
        # 必须先 em 后 with_links：反过来 with_links 生成的 <a> 会被 em 二次转义；
        # 正过来 with_links 只碰剩下的 http 串，不会把 em 的 <img> 包进 <a>。
        message = with_links(em(_INTERNAL_URL.sub("", body.strip())))
        message = Tools.strip_names(message)
        for part, uin in Tools.split_names(body.strip()):
            if uin is None:
                continue
            attr = qq_attr(uin) if uin else ' tabindex="0"'
            message = message.replace(
                html.escape(part, quote=False),
                f'<span class="msg-name nickname"{attr}>{html.escape(part)}</span>', 1
            )

        image_html = '<div class="image">'
        # 归一化走 iter_pics，与下载器同一份实现——各写一遍就会有一边对不上文件名
        for _fp, img_url in Redownload.iter_pics(p["pictures"]):
            # 本地没有才回落 CDN，此时要缩略图（/s）：签名可能已过期，但外网看图总比空好
            thumb = img_url.replace("/m&ek=1&kp=1", "/s&ek=1&kp=1").replace("!/m/", "!/s/")
            src = local(*Redownload.photo(img_url), fallback=thumb)
            image_html += f'<img src="{html.escape(src, quote=True)}" alt="图片">\n'
        image_html += "</div>"

        interactors = p.get("interactors", [])
        likes = [a for a in interactors if a.get("kind") == "like"]
        talks = [a for a in interactors if a.get("kind") != "like"]

        comment_html = ""
        if likes:
            # 点赞框标题已写明「点赞」，逐条再挂 action（"赞了我的说说"）是重话，故不传。
            named = [a for a in likes if a.get("qq") or a.get("name")]
            unknown = len(likes) - len(named)
            items = "".join(card(a.get("qq"), a["name"], a["time"], "") for a in named)
            if unknown:
                # 源没给点赞人 uin 的行（真库 382 条）不逐条画「?」——那会在 208 个帖子上堆
                # 382 张灰头像，而「?」不含任何信息。塌成一张说明卡，计数留在标题里。
                items += card("", f"{unknown} 人（昵称未知）", "", "", anon=True)
            comment_html += comments_template.format(
                title=f"点赞（{len(likes)}）",
                items=items,
            )
        talk_items = []
        talkers = []  # 已出框的 uin，评论正文并进对应小框后不再重复出框
        for a in talks:
            # 已带评论正文的行，动作词「评论」是冗余——只有「有动作、没正文」的行才显示动作词。
            # 只按 uin 匹配：事件行与评论行的时间格式不同（有/无秒），拿时间当联接键永远配不上。
            # 一律按字符串比：存量库里有 int 型 uin（mobile 接口原样存的），直接 == 会永远落空，
            # 正文丢成动作词「评论」。
            target = str(a.get("qq") or "")
            body = next((c[1] for c in p.get("comments", [])
                         if len(c) >= 4 and c[3] and str(c[3]) == target), "")
            talk_items.append(card(a.get("qq"), a["name"], a["time"],
                                   body or a.get("action")))
            talkers.append(target)
        for c in p.get("comments", []):
            if len(c) >= 4:
                c_time, c_content, c_nickname, c_uin = c
                # 该评论人的事件行已并入 talk_items（同人），不再重复出小框
                if any(u == str(c_uin) for u in talkers):
                    continue
                talk_items.append(card(c_uin, c_nickname, c_time, c_content))
        if talk_items:
            comment_html += comments_template.format(
                title=f"评论·回复（{len(talk_items)}）",
                items="".join(talk_items),
            )

        category = post_category(p["content"], p["pictures"])
        category_counts[category] = category_counts.get(category, 0) + 1
        year, month = Tools.time_parts(time_str)
        post_html += post_template.format(
            avatar_url=avatar_url,
            nickname=shown_name,
            qq_attr=qq_attr(uin),
            time=html.escape(time_str, quote=False),
            message=message,
            image=image_html,
            comments=comment_html,
            cat=category,
            year=year,
            month=month,
            date=Tools.date_part(time_str),
        )

    tabs_html = "".join(
        f'<button type="button" data-cat="{key}" '
        f'aria-pressed="{"true" if key == "all" else "false"}">'
        f'{"全部" if key == "all" else key}'
        f'<span class="n">{category_counts.get(key, 0)}</span></button>'
        for key in CATEGORY_TABS
        if key == "all" or category_counts.get(key)
    )
    final_html = html_template.replace("{tabs}", tabs_html).replace("{posts}", post_html)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_html)
