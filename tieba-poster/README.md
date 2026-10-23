# 百度贴吧自动发帖脚本（tieba_poster）

把你本地编辑好的内容，一条命令批量发到指定贴吧。支持主题帖、跟帖、签到、预览。

**发帖走真实浏览器（Edge + CDP）**，因为新版发帖接口带 JS 签名风控，纯 `requests` 已经被封死；
查等级、签到、看帖子这些轻量操作仍然是 `requests`，不启动浏览器。

---

## 1. 目录结构

```
tieba-poster/
├─ browser_poster.py      ★ 发帖脚本（浏览器方案，用这个）
├─ tieba_poster.py        查询工具（check / forum / sign / reply / dry-run 预览）
├─ setup_cookie.py        Cookie 填写向导（粘贴即校验）
├─ config.json            你的配置（只需填 cookie）
├─ config.example.json    配置模板
├─ run.bat                Windows 双击启动（cmd）
├─ run.sh                 Git Bash 启动
├─ .venv/                 项目专用 Python 环境（已装 requests + websocket-client）
├─ requirements.txt
└─ posts/
   ├─ example.md          帖子文件示例
   └─ 我的第一个帖子.md     改这个就能发
```

## 2. 安装（关键是选对解释器）

项目里已经建好 `.venv/`（自带 requests），**推荐直接用它**，不要依赖 PATH 里的 `python`：

| 终端 | 怎么用 |
| --- | --- |
| cmd | 双击 `run.bat`，或 `.venv\Scripts\python.exe tieba_poster.py check` |
| PowerShell | `.\.venv\Scripts\python.exe tieba_poster.py check` |
| Git Bash | `./run.sh`，或 `.venv/Scripts/python.exe tieba_poster.py check` |

⚠️ **别用 Anaconda 那个 python 跑**。实测同一台机器、同一个网址、都是直连：

| 解释器 | 请求 `tieba.baidu.com` |
| --- | --- |
| `D:\anaconda\python.exe`（requests 2.32.3 / urllib3 2.2.3） | **10 秒超时** |
| `.venv` 里的 python（requests 2.34.2） | 66 毫秒成功 |

两者 TLS 握手都正常、都能连上 443，但 Anaconda 那个发不出请求 —— 这种时候报的 `Read timed out` 跟 Cookie 一点关系都没有。真要用 Anaconda，先试 `pip install -U requests urllib3`。

想自己重建环境：

```bash
python -m venv .venv
.venv/Scripts/pip.exe install requests     # PowerShell / cmd
.venv/Scripts/pip install requests         # Git Bash
```

## 3. 获取 Cookie（关键一步）

脚本靠 Cookie 里的 `BDUSS` + `STOKEN` 维持登录态，只需要做一次（Cookie 有效期通常几个月）。

1. Chrome / Edge 打开 <https://tieba.baidu.com> 并确认**已登录**；
2. 按 `F12` → 切到 **Network（网络）** 面板 → 按 `F5` 刷新页面；
3. 点开任意一个发往 `tieba.baidu.com` 的请求（比如第一个 `f?kw=...`）；
4. 在 **Request Headers（请求标头）** 里找到 `Cookie:`，右键 → **Copy value**；
5. 整段粘到 `config.json` 的 `cookie` 字段里（要含 `BDUSS=` 和 `STOKEN=`）。

> 手机端抓不到也没关系，用电脑网页版即可。Cookie 等同于账号密码，**不要发到群里或提交到 git**。

### 懒得手改 JSON？用向导

```bash
python setup_cookie.py
```

它会把光标停在等待状态，你把整串粘进去回车就行 —— 自动检查 `BDUSS`/`STOKEN`/`BAIDUID` 齐不齐、写回 `config.json`、并立刻联网验证登录态。

⚠️ 两个最容易翻车的点：

- **别在 Console 里敲 `document.cookie`** —— `BDUSS` 是 HttpOnly 的，那里根本读不到。
- **别从 Application/Cookie 表格里抄单个值** —— 列宽会把长值截断显示（末尾变 `...`），抄下来一定是坏的。请从 **标头(Headers) → Request Headers → Cookie** 右键 **Copy value** 复制整行。

截图里常见的几条：`BDUSS`（登录凭证，必须有）、`STOKEN`（发帖必须）、`BAIDUID`、`BDUSS_BFESS`、`BAIDUID_BFESS`（后三条是风控用的，一起带上更稳）。整串复制就全带上了，不用挑。

## 4. 配置

```bash
cp config.example.json config.json   # Windows: copy config.example.json config.json
```

仓库里已经放好了一份空的 `config.json`，**只需要把 `cookie` 这一行填上**，其余保持默认就能跑。

`config.json` 里常改的几项：

| 字段 | 说明 |
| --- | --- |
| `cookie` | **必填**，上面复制的那串 |
| `default_kw` | 默认吧名，帖子文件没写 `kw` 时用它 |
| `delay_range` | 多帖之间的随机间隔秒数，默认 `[8, 20]`，别调太小 |
| `sign_in` | 发帖前是否顺带给该吧签到 |
| `content_limit` | 单楼字上限，超了自动拆成跟帖，默认 9000 |
| `upload_images` | 是否尝试上传本地图片（实验性，见下） |
| `fid_map` | 兜底用：手动指定 `{"吧名": "fid"}`，查不到 fid 时填 |
| `extra_fields` | 应急口子：追加到发帖表单的额外字段 |

也支持环境变量 `TIEBA_COOKIE`，优先级高于配置文件。

## 5. 写内容

一个帖子 = 一个文件（`.md` / `.txt`），开头用 `---` 包一段元信息，后面全是正文：

```markdown
---
kw: python
title: 我写的一个小工具，分享给大家
# images: 1.png, 2.png     ← 可选
# sign: true               ← 可选，发帖前先签到
---

这里写正文，想写多长写多长，超过 9000 字会自动拆成「主题帖 + 跟帖」。
```

- `kw` 是**吧名**，不要带「吧」字（`python` 而不是 `python吧`）
- 吧名就跟网址里那个一样：打开该吧，地址栏 `tieba.baidu.com/f?kw=XXXX` 里的 `XXXX` 就是吧名
  - 中文吧原样写中文：`数学`、`原神`、`考研`、`机器学习`
  - 英文吧写英文：`python`、`nba`、`programming`
  - **按吧的真实名字写，不是按帖子语言写** —— 在"英语吧"发中文帖，`kw` 还是 `英语`
  - 中文吧名可以放心写，脚本会自动做 URL 编码（v1.0.1 修复：早版本会因 `Referer` 头里塞中文报 `UnicodeEncodeError: 'latin-1' codec can't encode`）
- `title` 超过 31 字会被自动截断
- 也可以用 `.json` 批量写：

```json
[
  {"kw": "python", "title": "标题一", "content": "正文一"},
  {"kw": "cpp",    "title": "标题二", "content": "正文二"}
]
```

## 6. 运行

**先切到项目目录**，下面所有命令都在这里敲：

```bash
cd "C:/Users/15547/WorkBuddy/2026-10-22-17-21-40/tieba-poster"
```

`python` 指的是 **`.venv` 里那个**。三种终端怎么让它生效：

```bash
# Git Bash：激活后直接用 python
source .venv/Scripts/activate

# PowerShell：激活后直接用 python
.\.venv\Scripts\Activate.ps1
# 若报「无法加载文件，禁止运行脚本」，先跑一次：
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# cmd 或懒得激活：直接用完整路径
.venv\Scripts\python.exe tieba_poster.py check
```

> 懒得 `cd` 也行：脚本会先按当前目录找 `config.json` 和帖子文件，
> 找不到就自动回退到**脚本所在目录**再找一次。所以下面这样从任意目录跑也能成：
>
> ```bash
> "C:/Users/15547/WorkBuddy/2026-10-22-17-21-40/tieba-poster/.venv/Scripts/python.exe" \
>   "C:/Users/15547/WorkBuddy/2026-10-22-17-21-40/tieba-poster/browser_poster.py" \
>   post "posts/我的第一个帖子.md"
> ```
>
> 注意 `.venv/Scripts/python.exe` 这个**相对路径**本身还是依赖当前目录的，
> 所以要么 `cd` 进去，要么写成绝对路径。

```bash
# 卡在超时/连不上时，先跑这个（不碰 Cookie，只查网络）
python tieba_poster.py netcheck

# 先验证登录态
python tieba_poster.py check

# 预览（只打印清单，不启动浏览器、不发帖）
python tieba_poster.py post posts/我的第一个帖子.md --dry-run

# 真正发出去 —— 走浏览器，会弹出一个 Edge 窗口自己点按钮
python browser_poster.py post posts/我的第一个帖子.md

# 不想看窗口（后台跑，适合批量 / 定时任务）
python browser_poster.py --headless post posts/我的第一个帖子.md --yes

# 批量：把 posts/ready/ 下所有文件依次发出去（一个文件里也可以写多篇）
python browser_poster.py post posts/ready --yes

# 页面元素定位出问题时，抓一下现场（会开编辑器，但不提交）
python browser_poster.py probe --kw 孙笑川

# 发完想看看它刚才点了什么，可以让窗口留着自己检查
python browser_poster.py post posts/x.md --keep-open

# 回复某个帖子（网址 /p/ 后面那串数字就是 tid）
python tieba_poster.py reply --kw python --tid 812345678 --text "顶一下"

# 签到
python tieba_poster.py sign --kw python --kw cpp

# 查你在某个吧的等级 / 关注状态
python tieba_poster.py forum --kw python

# 网络没问题但就是超时？强制直连，绕过系统代理
python tieba_poster.py check --ignore-proxy

# 生成新帖子模板
python tieba_poster.py template posts/new.md
```

Windows 上也可以直接双击 `run.bat`（cmd）或运行 `./run.sh`（Git Bash），按提示操作。这两个启动器都会自动挑 `.venv` 里的解释器，不用管激活。

**安全设计**：真正发帖前会打印待发清单并要求输入 `y` 确认；非交互环境（脚本/定时任务）必须显式加 `--yes` 才会发。

### 成功是怎么判定的

发帖不再靠猜。点完「发布」后依次用三个独立判据确认：

1. 抓 `POST /c/c/thread/add_pc` 的响应，`no == 0` 即成功，并取回 `tid`；
2. 抓不到就用移动版吧页（SSR）**回查标题是否真的出现在列表里**，查到就连 `https://tieba.baidu.com/p/<tid>` 一起打出来；
3. 最后才退到页面提示语（比如「发送成功」，可能还在审核队列里）。

所以「成功 N 条 / 失败 N 条」这行是可以信的。

## 7. 报错怎么看

### 先分清是「网络问题」还是「Cookie 问题」

看报错关键词：

| 报错 | 属于 | 怎么处理 |
| --- | --- | --- |
| `Read timed out` / `ConnectionError` | **网络** | 见下面「超时排查」，跟 Cookie 无关 |
| `101 未登录` / `服务器返回 null` | Cookie | 重新复制 Cookie |
| `1 内容不合法` | 内容 | 敏感词、外链、纯表情、内容太短 |

**`Read timed out` 特别容易被误判**：它说明 TCP 连接是**建立成功**了的（如果是连不上会是 `ConnectTimeout`）。连上了却读不到数据，最常见的就是——你连的其实是代理端口，代理进程没把请求转发出去。

### 超时排查（三步）

```bash
python tieba_poster.py netcheck
```

它会依次检查：代理设置（含 Windows 注册表里的系统代理）、DNS、TCP 443 裸连、以及「走代理」和「强制直连」两组 HTTPS 请求的成败与耗时，最后直接给结论。

如果结论是「走代理失败、直连正常」，说明代理是元凶：

```bash
python tieba_poster.py check --ignore-proxy     # 临时绕过
```

或在 `config.json` 里设 `"ignore_proxy": true` 永久生效。长期方案是在代理软件里给 `*.baidu.com` 加一条直连规则（Clash 用 `DOMAIN-SUFFIX,baidu.com,DIRECT`），或者干脆关掉代理再跑。

### 错误码对照

脚本会把贴吧返回的 `no` 码翻译成中文提示，常见几种：

| 现象 | 原因 & 处理 |
| --- | --- |
| `101 未登录` | Cookie 过期或复制不全，重新复制 `BDUSS` + `STOKEN` |
| `1 内容不合法` | 敏感词、外链、纯表情、内容太短；先换个说法试试 |
| `4 / 40 需要验证码` | 账号或该吧触发风控。手动去网页发一条、过几小时再跑，别硬怼 |
| `10 发帖过于频繁` | 把 `delay_range` 调大，或换 IP |
| `9 吧不存在` | fid 无效，吧名写错了（记得去掉「吧」字） |
| `34 该吧禁止发帖` | 吧被封 / 等级不够 / 被拉黑，脚本无解 |
| `273 被拒` | 老接口已对不上新风控（见下节）；也可能确实等级不够或没关注。先跑 `python tieba_poster.py forum --kw 吧名` 看等级 |
| `1011 未加入该吧或等级不够` | 同上，签到/关注/升等级 |
| 日志里出现 `百度安全验证` | 请求被风控拦了。确认 Cookie 里带上 `BAIDUID`，或先手动打开一次该吧页面 |

### 273 到底是怎么回事（一段踩坑记录，顺手纠正一个常见误解）

网上（包括这个 README 的旧版本）都说 `273` = 等级不够。**不一定。** 实测就是这样：

- 账号在孙笑川吧只有 1 级、当天刚签到，等级肯定不够；
- 但**手动用网页发帖一次就成功了** —— 说明服务端根本没在卡等级；
- 同一时刻换脚本发，稳定返回 `273`，`err_code=230273`，且回显里 `content` 是空的。

抓包之后真相是：网页端已经换成新接口 `POST /c/c/thread/add_pc`（multipart 表单），
请求里带三个由前端 JS 现算的字段：

| 字段 | 位置 | 作用 |
|---|---|---|
| `sign` | 表单字段 | MD5 签名，盐值混在压缩过的 JS 里 |
| `jt` | 表单字段 | 加密串，内容跟当前会话/时间绑定 |
| `Acs-Token` | 请求头 | 风控 token |

旧接口 `/f/commit/post/add` 还在，但它已经跟新接口的风控对不上，于是统一回一个含糊的 `273`。
签名部分我试过 46 种组合（不同盐值 × 键值拼接顺序 × urlencode × 字段子集），全部不命中 ——
**纯 `requests` 复现这条路是死的**，不是参数没调对。

所以现在的做法是：让真实浏览器去点「发贴 / 发布」按钮，签名由页面自己算。
**能不能发帖跟等级无关，跟你是不是真人操作有关。**

> 补充：贴吧官方的 4 级门槛规则确实存在（「本吧目前仅允许 4 级头衔以上会员发贴」），
> 部分吧也真的会拦。所以如果浏览器方案仍然失败，先跑 `python tieba_poster.py forum --kw 吧名`
> 看看等级和关注状态，再判断是不是吧本身的门槛。

### 为什么不用 selenium

本来该用 selenium，但**本机 msedgedriver 155 有个 bug**：任何导航（连 `about:blank`）都返回
`unhandled request`。已经排除网络、系统代理、沙箱、`pageLoadStrategy`、`--remote-allow-origins`、
手动起 Edge 再 `debuggerAddress` attach —— 全都一样失败。

所以 `browser_poster.py` 直接说 CDP 协议：自己拉起 Edge（`--remote-debugging-port`），
用 websocket 发 `Page.navigate` / `Runtime.evaluate` / `Input.insertText` 这些命令。
好处是少一层依赖，坏处是得自己处理事件缓冲（见文件里 `Edge.events`）。

### 编辑器是 Quill，不能直接改 DOM

发帖弹层里标题和正文都是 Quill（`.ql-editor.ql-blank`），靠 `data-placeholder` 区分：

- 标题：`必填，请输入贴子标题(5-31个字)`
- 正文：`请输入正文（建议200-2000字）`

**直接 `el.textContent = ...` 是没用的** —— Quill 内部有独立的数据模型，改 DOM 它不知道，
提交时读模型还是空的。必须用 CDP 的 `Input.insertText` 发真实输入事件，
Quill 收到 `beforeinput` 才会更新模型。这也是为什么不走「构造请求」那条路。

## 8. 关于图片

**浏览器方案目前不传图**：发布时如果帖子文件里写了 `images:`，脚本会提示「会被忽略」，正文照发。

`tieba_poster.py --upload-images` 那条老路还在（调上传接口），但**该接口百度随时会改**，
成功率不保证，失败只提示不中断。真需要配图，最稳的是发完自己手动补 —— 或者用
`browser_poster.py post x.md --keep-open`，让它发完停在那儿，你顺手拖图进去。

## 9. 几条必须提醒的事

1. **这是自动化操作**，贴吧风控会识别。别 24 小时连发、别同一分钟发十条，默认 `delay_range` 别往下调。
2. **只发你自己的原创/有权发布的内容**。批量灌水、广告、引流很容易封号封 IP，脚本不负责兜底。
3. **账号风险自担**：一旦触发风控可能被要求短信验证、限制发帖甚至封禁。
4. Cookie 明文存在 `config.json`，用完记得删掉或者把文件加进 `.gitignore`。
5. 接口是逆向网页端得来的，百度改版后可能失效 —— 失效时先跑 `check` 看是不是登录态的问题，再看发帖接口的返回原文。
