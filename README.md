# TelegramForwardX

将指定 Telegram 频道的新消息（文字、图片、视频）转发到 X/Twitter。需要 Python 3.10+。

## 本地配置与隐私

首次运行先复制 `config.example.ini` 为 `config.ini`，再填写自己的 Telegram API ID、API Hash 和频道用户名。X 账号可通过 `expected_username` 设置。

`config.ini`、API 凭据文件、登录会话、`state/`、日志和构建产物均不提交到 GitHub；请勿将这些本地文件添加到提交中。

## Windows EXE：浏览器发布

X 发布通过 Playwright 操作本机 Edge/Chrome 网页，不使用 X 开发者 API，不读取 `x Api.txt` 或旧的 `[Twitter]` 凭据。Telegram 仍使用 Telethon，需要 Telegram API ID、Hash 和登录会话。

把 `MemeBot.exe`、`telegram.bat` 和 `config.ini` 放在同一个目录。Windows 默认使用已安装的 Microsoft Edge，无需下载额外浏览器。

第一次先关闭其他转发实例，直接双击 `MemeBot.exe` / `telegram.bat`，或在 PowerShell 运行：

```powershell
.\MemeBot.exe --check-config  # 仅检查本地配置
.\MemeBot.exe                 # 按提示完成 Telegram / X 登录，核对成功后自动开始转发
```

默认使用 `manual` 模式：正常启动本机专用 Edge，再由 Playwright 连接。前台启动会等待你在专用窗口手动完成 X 登录、验证码和安全检查，最长为 `login_timeout_seconds`（默认 600 秒）。登录核对期间不发帖；账号核对成功后自动进入转发流程，已有登录状态则直接继续。账号不符时停止，不会覆盖已绑定账号。专用窗口不继承日常浏览器的登录，也不会读取或复制日常浏览器的 Cookie。

只想先登录、不开始转发时，仍可使用以下命令；也可先在未连接 Playwright 的窗口登录，再做检查：

```powershell
.\MemeBot.exe --open-browser  # 仅打开专用 Edge，手动登录
.\MemeBot.exe --login-x       # 核对 X 登录和账号，不发帖
.\MemeBot.exe --login         # 完成 X / Telegram 登录，不发帖
```

普通浏览器已登录，而专用窗口未登录，是因为资料目录不同。若普通启动的专用窗口也被 X 限制登录，请先处理 X 的提示；程序不会绕过登录限制。关闭专用浏览器后，下次仍用 `--open-browser` 打开同一目录，保留成功登录后的状态。

启动时会自动打开或连接专用浏览器，复用其中已有的 X/登录标签页；首次启动也会复用初始页面，避免程序额外打开第二个标签页。这是转发实际使用的窗口，可以最小化。若在点击发布前关闭了浏览器或任务标签页，任务保留为待发送，几秒后自动重连并重试；若 X 留下了草稿，仍需手动清理后重试，不会覆盖草稿。若关闭发生在发布点击之后且无法确认结果，仍会记录为 `uncertain` 并停止，防止重复发帖。

打开发布框时，以账号和可见编辑框是否就绪为准，不等待整页脚本加载完成。尚未输入文字或上传媒体时遇到导航／编辑框等待超时，会在检查页面没有草稿后重试一次。点击发布之前的任何等待超时（发帖框、上传、发布按钮被临时遮挡等，例如刚打开浏览器、网络较慢）都不会停止转发：程序关闭这个装着本条未发送内容的标签页，任务保持待发送，按退避间隔（最长 5 分钟）自动重新编写并重试。登录失效、X 安全验证、发现他人草稿、文字核对不一致或发布结果不明确仍会停止等待人工处理。

确认帖子链接并将成功记录写入磁盘后，程序会在当前已保存的待发队列全部处理完时正常关闭专用 Edge；连续任务、等待收齐的相册或延迟重试期间保持窗口。下一条任务到来时自动打开原资料目录，沿用原登录状态和浏览器网络设置。只关闭专用实例，不影响日常 Edge；关窗失败时保留窗口，不修改已成功的发送记录。刚启动且尚未发过帖时，窗口保持打开。默认 `manual` 模式中，登录失败、发帖失败、结果不明确或手动停止转发时，只断开连接，相关窗口保留供核对。

直接双击 EXE 遇到需人工处理的错误时，会显示日志路径并等待按回车，避免窗口瞬间消失；从 BAT、终端或后台启动时保持原有退出行为。登录检查、账号不符、已有草稿等仍需按提示处理。输入末尾的话题/提及自动补全会在上传前关闭；若此时 X 弹出“Save post?”（保存帖子），程序会自动取消离开编辑器，保留待发文字和媒体继续发布，无需手动点 Discard。正式发送前还会检查发布按钮是否被浮层遮挡。话题/提及建议列表若在上传期间才弹出，点击发布前会再次关闭。X 提示达到每日发帖上限（“You’ve hit the daily post limit”）且编辑框仍保留本条内容时，视为确认未发出：关闭该未发出的编辑标签页，任务保持待发送，整个队列按顺序暂停 1 小时后自动重试，不会标记为 `uncertain`，也无需手动 `--retry`。

`config.ini` 可添加：

```ini
[Browser]
mode = manual
debug_port = 9223
channel = msedge
expected_username = example_user
headless = false
timeout_seconds = 45
upload_timeout_seconds = 180
login_timeout_seconds = 900
```

- `manual` 模式的 `channel` 支持 `msedge` 和 `chrome`，使用本机已安装的浏览器。`managed` 模式保留由 Playwright 启动浏览器的旧行为，并额外支持 `chromium`。
- 当前版本不支持 Firefox。你日常仍可使用 Firefox，MemeBot 单独使用专用 Edge；将 `channel` 改为 `chrome` 可选择已安装的 Chrome，但需要在它的专用窗口重新登录。
- `manual` 模式使用浏览器自身的网络设置，和普通启动一致；`Settings` 里的代理仍用于 Telegram，只有 `managed` 模式会将该代理显式传给浏览器。
- `expected_username` 填 X 用户名，不是显示名称。匹配不到就停止，不会向另一个账号发帖。留空时，首次前台启动或 `--login-x` 会把登录的账号固定下来。
- 专用浏览器的资料保存在本机 `%LOCALAPPDATA%\MemeBot\x-browser-manual-msedge`（Chrome 为 `x-browser-manual-chrome`；managed 模式为 `x-browser`），不在 exe 所在目录，详见下文“程序目录与本机数据”。账号记录在 `state/x-browser-account.json`。这些登录数据仅留在本机，不要共享或提交到 Git。
- 浏览器控制端口仅监听 `127.0.0.1`，默认 9223，不能向局域网或公网开放。不要在这个专用浏览器中登录其他敏感服务。端口已被别的进程占用时程序会拒绝接管；可调整 `debug_port`。
- `manual` 模式要求 `headless = false`，后台转发也会打开或连接可见的专用浏览器。新机器请先前台运行一次完成登录；`--background` / `--non-interactive` 保持不交互，登录缺失时提示并停止。`managed` 模式可设置 `headless = true`，但前台启动的登录检查及登录诊断始终使用可见窗口。
- 首次登录后更换浏览器不保证登录状态可迁移；应重新运行 `--login-x`。
- 专用窗口被缩小（例如显示器休眠后 Windows 自动调整窗口）时，X 会把左侧“个人资料”入口收进“More”菜单，账号核对会误判为登录失效。`manual` 模式在每次使用页面前检查可视区域，小于 1280×800 时按 1280×900 的桌面布局渲染该页面；窗口本身和日常浏览器不受影响。

### 程序目录与本机数据（适合放进 OneDrive）

程序目录（exe 所在目录）只放小文件：`MemeBot.exe`、`config.ini`、`telegram.bat`、Telegram 会话、`state/deliveries.sqlite3`、`state/*.log` 和 `state/x-browser-account.json`。运行时每条帖子只改动几 KB，可以放在 OneDrive 等同步目录中。

体积大、变化快的数据只放在每台电脑自己的 `%LOCALAPPDATA%\MemeBot`，不会被同步：

- 专用浏览器资料目录：X 登录数据约 20 MB，加上 X 已安装的离线程序（`Service Worker` 与 `Code Cache`，约 150 MB）。离线程序在两次启动之间保留，否则每次打开浏览器都要重新下载，X 在开头一两分钟里加载极慢，发帖框会超时；只有资料目录超过 400 MB 时才一并清掉。
- 浏览器本机控制端点 `x-browser-endpoint-*.json`。
- `tmp`：准备一条帖子时从 Telegram 下载的媒体，发完即删。

升级后第一次打开浏览器时，会自动把旧版放在 `state/` 里的浏览器资料迁移到本机：先清理可重建的缓存，再复制，逐个核对文件和大小一致后才启用新位置，然后删除旧目录，X 登录保持不变。核对失败、浏览器仍在使用旧目录、或本机已经有同名资料时，保留旧目录不删除、不合并，下次启动再处理或在日志中说明。每台电脑首次使用仍需手动登录 X 一次（Edge 的 Cookie 用本机 Windows 账号加密，本来就无法跨电脑同步）。

若曾在某个应用内置的终端里启动 MemeBot（这类 MSIX 打包应用会把 `%LOCALAPPDATA%` 的写入悄悄重定向到 `AppData\Local\Packages\<应用>\LocalCache`），程序会检测到这种重定向，此时不迁移任何数据并在日志中提示直接启动 `MemeBot.exe` 或 `telegram.bat`。之后正常启动时，若本机还没有浏览器资料，会自动把被应用私有目录截留、且含 X 登录的资料（多份时取最新）按同样的复制—核对—切换流程迁回本机，无需重新登录；本机已有资料时不会覆盖。

同一份程序目录不要在两台电脑上同时运行：实例锁只在本机有效，同时运行可能重复发帖，SQLite 与 Telegram 会话也会冲突。轮流使用时，先在一台停止并等 OneDrive 同步完成，再到另一台启动。

### 体积控制与安全清理

专用浏览器的资料目录是唯一会明显增长的部分：Edge 会在其中下载组件模型、遥测、崩溃转储，并不断累积网页缓存，可以涨到近 1 GB。程序采用零缓存方式，且不需要重新登录：

- 启动专用浏览器时附加参数：关闭后台组件下载与更新、崩溃上报和同步，并把 HTTP/媒体缓存上限设为 1 字节（即不缓存；设 0 反而表示浏览器默认大小）。登录本身是普通前台请求，不受影响；`managed` 模式的 Playwright 本来就使用同类参数。
- 每次打开专用浏览器之前自动清理：可重建的组件、遥测、崩溃数据以及网页缓存。X 的离线程序（`Service Worker`、`Code Cache`）只在资料目录超过 400 MB、迁移资料或运行 `--prune-browser` 时清理。Cookie（`Default/Network`）、`Local State`、`Local Storage`、`Session Storage`、`IndexedDB`、`Preferences`、`Login Data` 等登录与站点数据永远不在清理列表内，因此 X 登录状态保留。
- 清理后资料目录若仍超过 400 MB，只在日志中警告并列出最大的几个文件夹，不会删除清单以外的任何内容。
- 启动转发时还会清理两类残留：被强制结束时留下的 `forward-xxxxxxxx` 临时媒体文件夹（本机 `tmp` 和旧版的程序目录中），以及被强制结束的 `MemeBot.exe` 留在 `%TEMP%\_MEI…` 的解压文件（每份约 120 MB）。后者只删带有 MemeBot 标记、且对应进程都已退出的目录，其他程序的 `_MEI` 目录不会被碰。

所有自动删除都遵守同样的保护：只删名字在清单上的项目；路径必须位于预期的根目录内；不跟随符号链接或目录联接；只在持有实例锁、且浏览器未使用该资料目录时进行（除检查调试端口外，还检查 Edge 运行时独占的 `lockfile`，即使浏览器没有开调试端口也不会在运行中的目录上删文件）；每次删除都写入日志并注明回收的大小。

- 旧版配置里的 `max_profile_mb` 已不再使用，保留在 `config.ini` 中也会被忽略。
- 需要立即回收时（例如升级前），在浏览器窗口已关闭的情况下运行 `.\MemeBot.exe --prune-browser`：立即清掉全部可重建数据，同样保留登录。转发器正在运行时由实例锁拦截；专用窗口仍开着时会提示先关闭。
- `.\MemeBot.exe --status` 会列出程序目录和本机数据各自的占用、当前设置未使用的资料目录，以及 `state` 中不属于当前版本的文件（只提示，不自动删除）。
- 由于不保留缓存，每次打开 x.com 都要重新下载页面脚本，会稍慢几秒、多用少量流量，功能不受影响。切换过 `channel` 或 `mode` 后，未使用的资料目录不会被自动删除，可在确认不再需要该登录后手动删除。

浏览器发布流程：下载 Telegram 媒体 → 打开发帖窗口 → 核对账号 → 填写文字、上传媒体 → 等待可发布 → 单次点击发布 → 从网页成功提示提取该账号的新帖子链接。已有草稿不会自动覆盖；请在浏览器手动清理后再重试。

Telegram 相册按频道和 `grouped_id` 识别：图片/视频按消息顺序上传，非空说明文字按顺序合并到主帖。同样文字但不同相册不会合并。历史扫描到末尾且相册至少 3 秒没有新成员后才允许发送；通常会等待下一次 30 秒历史检查，避免实时消息和历史分页将相册拆开。

单条 X 帖子最多按 4 个媒体处理；超过 4 个时，前 4 个随说明文字发主帖，剩余媒体每批最多 4 个，依次在主帖评论区回复，不重复说明文字。例如 10 张图片发为主帖 4 张、两条评论分别 4 张和 2 张。上传后会核对网页媒体预览数量。网页若拒绝某种视频或混合媒体组合，也会停止，需人工处理。

相册的各 Telegram 消息仍分别保存在队列中，每批共用该批的 X 帖子 ID，发送、失败和重试按批原子记录。`--status` 的计数是源消息数量，并不等于 X 帖子数量。按成员执行 `--retry` 或 `--mark-sent` 只处理该成员所在批次；已成功的主帖和评论不会重发，重启后继续剩余批次。结果不明确的批次仍需核对，后续批次不会越过它。旧版本仅因“超过 4 个媒体”而失败的记录会在更新后启动时自动重新入队；其他失败仍需人工处理。升级保留旧发送记录。

只有取得明确的新帖子链接才记为 `sent`。点击后的超时、窗口关闭、网页返回错误或缺少成功链接都保留为 `uncertain` 并停止，需要核对 X。不要手动点击自动化窗口的发布按钮；那会绕开队列的发送记录。网页改版、登录失效、验证码或风控仍可能需要人工处理，程序不会绕过这些检查。

其他命令：

```powershell
.\MemeBot.exe --check-proxy
.\MemeBot.exe --status
.\MemeBot.exe --prune-browser
.\MemeBot.exe --background
```

`telegram.bat --background` 启动异常重启模式。登录会话 `meme_bot_session.session` 和完整 `state` 目录应在升级时保留（浏览器登录在本机 `%LOCALAPPDATA%\MemeBot`，升级不受影响）。重复启动由实例锁拦截。

从源码重新打包：安装 `requirements.txt` 和 `pyinstaller` 后，运行 `powershell -ExecutionPolicy Bypass -File .\build.ps1`，结果在 `dist`。构建包含 Playwright 驱动，不打包账号、配置、浏览器登录数据或 Telegram 会话，仍依赖本机 Edge/Chrome。

## 安装与源码运行

```powershell
Copy-Item config.example.ini config.ini  # 仅首次配置；已有配置请保留
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe memebot.py
# 首次按提示完成登录，随后自动进入转发流程
```

源码运行时，配置和 `state` 位于项目根目录；EXE 运行时则在 `dist`。请在你实际使用的目录登录，两个目录不会自动同步会话。

## Clash 代理配置

默认配置：

```ini
[Settings]
proxy_mode = auto
proxy_type = auto
proxy_host = 127.0.0.1
proxy_port = 7890
```

- `auto`：先检查环境变量和系统代理（Windows 注册表），再检查配置的地址/端口，最后探测本机 `7890、7891、7892、7897、1080、10808、10809`。不会扫描全部端口。
- `manual`：只使用指定地址/端口；不可用时在启动阶段报错。自定义 Clash 端口可设置 `proxy_mode = manual` 和相应 `proxy_port`。
- `off`：不使用应用层代理；由系统路由处理流量，适用于 TUN/VPN。

`proxy_type` 支持 `auto`、`http`、`socks5`。HTTP 专用端口不能作为 SOCKS5 使用；mixed 端口支持两种协议。自动探测验证 HTTP CONNECT 或 SOCKS5 握手，不会仅因端口开放就认为它是代理。Telegram 使用该代理；`managed` 浏览器也使用它，`manual` 浏览器则沿用浏览器自身网络设置。`managed` 浏览器不支持带用户名/密码的 SOCKS5 代理，这种情况请改用 HTTP 代理或系统 TUN。

需要代理认证时，可设置 `proxy_url = socks5://用户名:密码@127.0.0.1:7897` 或 `http://...`，特殊字符需要 URL 编码。此字段优先于手动 host/port/type；要强制使用它，请选 `manual`。日志不输出该 URL 中的密码。

`proxy_mode` 优先于旧字段 `enable_proxy`；没有 `proxy_mode` 的旧配置仍按 `True=manual`、`False=off` 处理。程序不修改系统代理或环境变量。

**边界：** 自动检测读取静态系统代理，不执行 PAC 脚本，也不枚举 VPN 网卡或读取 Clash 配置文件。仅开 TUN 时通常没有系统代理端口，自动模式找不到代理会提示并使用系统路由。HTTP 探测会尝试向一个 Telegram DC 建立 CONNECT 隧道后立即关闭；SOCKS5 探测只验证协议协商。探测成功不代表账号认证、节点规则或所有 API 连接都可用。

## 后台常驻和重启恢复

先用 `load_Api_x.py` 在终端完成一次 Telegram 登录。需要自动重启时，使用后台启动器：

```powershell
.\.venv\Scripts\python.exe run_background.py
```

启动器会在异常退出后按 5、10、20…秒重启转发进程，最长间隔 300 秒。运行稳定 5 分钟后重置退避。浏览器登录、网页发布结果不明确或配置问题需要人工处理时停止自动重启；修正并处理保存的失败任务后，重新启动即可。后台模式不会弹出验证码输入提示。

需要在 **Windows 重启并登录当前用户后**自动运行，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\background.ps1 -Action Install
powershell -ExecutionPolicy Bypass -File .\background.ps1 -Action Start
```

`Install` 只注册当前用户的登录启动任务，`Start` 立即启动。脚本使用项目内的虚拟环境和绝对路径，以普通用户权限隐藏运行。没有设置开机后、登录前运行，也没有修改睡眠设置。

管理任务：

```powershell
.\background.ps1 -Action Status
.\background.ps1 -Action Stop
.\background.ps1 -Action Remove
```

移走项目目录前先 `Remove`，移动后重新 `Install`。前台、后台启动器共用同一份队列；实例锁避免多个进程同时恢复和消费它。停止任务或重启可能强制中断正在发送的请求，其结果按下面的“待核对”规则处理。

## 持久化与补发规则

`state/deliveries.sqlite3` 保存消息文本、媒体类型、Telegram 频道/消息 ID、转发状态、重试时间和成功后的 X 帖子 ID。待发任务保存在磁盘上，不再受原来的 100 条内存队列限制；媒体仍然逐条下载并通过网页上传。

- **首次运行**：以频道当前最新消息为起点，不自动发送旧历史。旧版本没有保存的任务无法直接恢复。
- **后续启动/重连**：按保存的频道进度从旧到新补录停机期间的消息。实时事件触发同步，另每 30 秒核对一次历史，以覆盖漏掉的事件。
- **Telegram 下载的临时网络错误、FloodWait**：保存任务并等待重试，重试时间跨重启保留。
- **源媒体被删除**：保留为 `failed`，继续处理其他消息。
- **网页准备阶段登录/上传/布局异常**：没有点击发布，保留为 `failed` 并停止。修正后手动重新入队。
- **点击发布后超时、网页错误、无法确认帖子链接，或进程在发布阶段崩溃**：保存为 `uncertain`（待核对）并停止。X 可能已经发布，因此不会自动重发。
- **确认成功**：保存 `sent` 和 X 帖子 ID，恢复时不会把这个已完成任务重新执行。清理临时文件失败也不会把成功任务改回待发。

任务以“频道 ID + 消息 ID”标识，这是恢复队列处理进度所需的任务身份，不按内容去重。不同消息 ID 即使文字相同，也会分别转发。没有新增相似文本、相同图片等过滤规则。

查看任务状态（运行中也可执行）：

```powershell
.\.venv\Scripts\python.exe forwarder.py --status
```

对失败或待核对任务，先停止后台任务/前台程序，修正问题并检查 X。下面的 ID 仅为示例，用 `--status` 输出的真实 ID 替换：

```powershell
# 确认没有发出：重新入队，下次运行时补发。
.\.venv\Scripts\python.exe forwarder.py --retry=-1001234567890:42

# 确认已经发出：记录真实 X 帖子 ID，不再发送。
.\.venv\Scripts\python.exe forwarder.py --mark-sent=-1001234567890:42 --tweet-id=1234567890123456789
```

`--retry` 和 `--mark-sent` 本身不会登录或发帖。含负号的任务 ID 请按示例使用 `=` 传参。处理后重新启动后台任务。不要删除 `state` 目录来修复故障，这会丢失队列和历史进度；备份时应先停止转发器，再复制整个 `state` 目录。

纯图片/视频消息的空编辑框可能因占位 `<br>` 被浏览器读取为一个换行；校验接受这一空值表示。有文字的消息仍保留逐字校验，不忽略首尾空格、换行或不可见字符。文本不一致时，错误会包含校验阶段和双方字符数。如果旧版本因此停止，更新程序后将对应 `failed` 任务重新入队；若专用浏览器还留着失败的编辑框，先手动丢弃该未发布草稿，再启动转发器。

## 日志和重复发送核查

日志写入 `state/forwarder.log` 和 `state/supervisor.log`，每类最多保留当前文件和 3 个历史文件，每个约 2 MB。同类警告/错误在 30 秒内合并，首次错误保留。成功日志同时记录 Telegram 任务 ID 和 X 帖子 ID。

本次检查没有发现此前代码主动循环重发成功消息的逻辑；但此前重复输入同一个事件会再次发帖，多个转发实例也可能各自发布。因此在缺少对应日志时，不能确认重复发送一定是 Telegram bug，也不能排除转发流程。未额外增加内容去重功能。

## 回归测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

基础测试使用本地模拟代理和模拟浏览器发布器，不登录或发布真实消息。真实浏览器操作测试使用拦截全部网络请求的本地网页夹具，可显式开启：

```powershell
$env:MEMEBOT_BROWSER_TESTS="1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 修复依据

- [Mihomo：HTTP、SOCKS 和 mixed 端口](https://wiki.metacubex.one/en/config/inbound/port/)
- [Python：系统代理 getproxies](https://docs.python.org/3/library/urllib.request.html#urllib.request.getproxies)
- [Telethon：代理、重连和请求重试配置](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.telegramclient.TelegramClient)
- [Playwright：独立持久化浏览器目录](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [Playwright：安装和 PyInstaller 打包](https://playwright.dev/python/docs/library)
- [Chrome DevTools Protocol：正常关闭专用浏览器](https://chromedevtools.github.io/devtools-protocol/1-3/Browser/#method-close)

- [Microsoft：任务计划设置、重试和运行时限](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset)
