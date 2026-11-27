# AAMS · Agents Auto-Loop Management System

在 Windows 上运行 `start-loop-control.cmd` 可打开本机主管看板。首章鱼摊复测支持按版号下达和修订指令，跟踪管理员分派、执行者确认与两条路线的实录。[岗位配置和使用说明](LOOP-PANEL.zh.md)

本仓库使用第三方开源项目 **Agent Autoloop**（KKKKhazix/agent-autoloop）提供的 Hub、规则和 worker 软件。本仓库负责展示项目、团队与 worker 状态，提供本地收件箱，并记录派单、回执和纠偏的状态。上游 Hub 根据规则匹配事件、排队派发，由上游 worker 领取任务、启动配置的执行程序并回传结果。[Agent Autoloop 源码仓库](https://github.com/KKKKhazix/agent-autoloop)

Loop 集成调用已安装的上游 CLI 入口 `bin/agent-autoloop.ts` 和 Hub 的本机 HTTP API。核对过的上游源码为标签 `v1.9.6`、提交 `f27466d5f0ec9fc6f2f48a1fa68c748581906bdc`。本机开发检出运行的 CLI 版本自报为「未发布（提交 dev）」。[上游 v1.9.6 发布说明](https://github.com/KKKKhazix/agent-autoloop/releases/tag/v1.9.6)

Agent Autoloop 依据 MIT 许可证发布，版权为 Copyright (c) 2026 数字生命卡兹克。此处附带的 MIT 许可证适用于 Agent Autoloop。[上游 LICENSE](https://github.com/KKKKhazix/agent-autoloop/blob/f27466d5f0ec9fc6f2f48a1fa68c748581906bdc/LICENSE) · [本地许可证副本](third_party_licenses/agent-autoloop.txt)

独立项目，可复用于任意项目。全部代码与默认可写数据都在本目录。Python 标准库实现。默认不调用模型；真实 Codex 会话需显式开启并消耗额度。不会自动接管已有 agent 或上传本地文件。启用真实 worker 后，指令文本由官方 Codex 交给该账户配置的模型服务处理；不会自动发布数据。

## 能做什么，不能做什么

- 按「项目 / 团队 / worker」展示主动登记的状态。
- 向声明支持收件箱、心跳新鲜且未结束的 worker 加入本地指令队列。
- 消费者主动轮询、原子领取，再自行回报完成或拒绝。网页不会把输入直接当作 shell 命令执行。
- 显式配置只读适配器，观察另一个项目的旧 SQLite。
- 内置回显演示，证明注册 → 心跳 → 队列 → 领取 → 回报的协议能跑通。只回显文本，不是模型 agent，不执行指令。

**不支持：** 自动发现所有 Dot / Codex 内部 agent；给未接入协议的旧 worker 中途 steer；凭心跳证明进程存活；独立验证消费者是否真正完成工作。

默认没有接入任何项目、worker 或旧数据源。协作能力是调用者声明，不是自动验证的能力。

## 启动本地 HTML 面板

需要 Python 3.10+。已在本机 Python 3.13.5 测试。基本使用不需要 Node、npm 或 pip 依赖。

双击本目录的 **start-ui.cmd**，或在 PowerShell：

~~~powershell
# 从仓库根目录运行
$env:PYTHONUTF8 = '1'
python -B .\sync.py serve
~~~

1. 打开 http://127.0.0.1:8765/
2. 从启动终端复制本次 Session access key，粘贴解锁。
3. 点击「启动回显演示」，选择演示 worker、投递文本，观察状态。
4. 在启动终端按 Ctrl+C，停止服务及本服务自己的回显演示。数据与其他 worker 不会被删除或停止。

端口占用时使用：

~~~powershell
python -B .\sync.py serve --port 8766
# 或
.\start-ui.cmd --port 8766
~~~

不要双击 ui/index.html，它需要本地 Python 服务。服务只允许绑定 127.0.0.1，不开放局域网或公网。密钥不放在 URL 中，每次重启重新生成，不要转发给网站或其他人。

「锁定面板」只清除当前标签页的密钥与内容，不撤销服务器密钥，也不取消服务端已接受的在途投递。需要撤销会话时，停止并重启服务。

## CLI 与多项目

全局参数必须放在子命令前。默认库为本目录 data/sync.sqlite3；--db 可以指定另外一个独立同步器库。**不要把旧项目数据库当作 --db。**

~~~powershell
python -B .\sync.py init
python -B .\sync.py status
python -B .\sync.py --project alpha status
python -B .\sync.py --project alpha watch --interval 2
~~~

### 手工状态和协作收件箱不同

~~~powershell
# 不带 --cooperative：只登记状态，不能发指令
python -B .\sync.py --project alpha register alpha-notes-001 --team planning --role observer --task "手工登记的待办状态"

# 只有自己的程序确实准备消费 inbox，才声明 --cooperative
$w = python -B .\sync.py --project alpha register alpha-worker-001 --team build --role implementer --task "自己的协作程序" --cooperative | ConvertFrom-Json
$generation = $w.generation

python -B .\sync.py --project alpha heartbeat alpha-worker-001 $generation --status running --detail "自报：正在检查输入"
~~~

worker ID 在整个同步器库中全局唯一。不同项目或新的运行轮次用新的 ID，例如 alpha-build-20261112-001。重复注册不会覆盖旧记录或重启旧轮次。generation 绑定当前运行，错项目或错 generation 会被拒绝。

已登记的非演示岗位可以用原代次开启协作。下面的三条命令均隐藏代次输入。已有代次的协作程序可以用 `--generation-stdin` 从管道传入。

~~~powershell
python -B .\sync.py --project alpha opt-in alpha-notes-001
python -B .\sync.py --project alpha heartbeat alpha-notes-001 --status running --detail "核对当前工作"
python -B .\sync.py --project alpha inbox alpha-notes-001
~~~

启用只更新原登记的收件箱能力。项目员工卡按项目和岗位身份关联本人任务，近期心跳和轮询齐备后才能经该卡发指令。同一岗位在多个部门登记时，按本人记录的部门标识核对归属。工作详情可回查实际领取和回报。本人可在心跳说明中填写 `{"detail":"本次进展","model":"gpt-6.2-sol","effort":"max"}`，其中型号和思考档位填本人实际使用值。

状态：idle / running / blocked / completed / failed / stopped。后三个为终态，迟到的 running 心跳不能复活它们。新工作请登记新 ID。

超过 30 秒无新心跳显示 stale；明显未来时间显示 clock_skew；终态不会因时间流逝变成 stale。心跳全部是自报，不是进程探活。

### 指令：排队、领取、回报分开

~~~powershell
# 操作者投递：只是本地 queued，不是已送达或执行
python -B .\sync.py --project alpha send alpha-worker-001 "先回报当前进度与阻塞项" --id alpha-command-001

# 消费者发现队列：发现不等于领取，多消费者可以看见同一条
python -B .\sync.py --project alpha inbox alpha-worker-001 $generation

# 原子领取：同一条指令只有一个成功的 claimant
$c = python -B .\sync.py --project alpha claim alpha-worker-001 $generation alpha-command-001 --claimant alpha-consumer-001 | ConvertFrom-Json

# 消费者完成其支持的工作后回报，这里只示范协议，不代替执行
python -B .\sync.py --project alpha ack alpha-worker-001 $generation alpha-command-001 --state completed --result "消费者自报的结果或证据位置" --claimant alpha-consumer-001 --claim-token $c.claim_token

python -B .\sync.py --project alpha commands
~~~

- queued：仅本地排队，尚未领取。
- acknowledged：消费者原子领取成功，尚未回报完成。
- completed / rejected：持有领取令牌的消费者自报完成或拒绝。
- 相同 --id 只建一条指令；重试须保持项目、worker、generation、文本一致，不同内容报冲突。
- claim_token 只返回成功领取者，不出现在状态快照、流水或网页 API。不要贴到其他服务。
- 消费者仅在 claim 成功后处理；领取失败不能执行。
- 领取后崩溃会保持「已领取、未完成」，不会超时重投或重复执行副作用。**不保证 exactly-once execution。** 领取是否到达不确定时先核对，不要自行执行或发新 ID 重做。
- inbox 轮询不刷新心跳。消费者仍须定期 heartbeat。
- 未领取指令不自动删除或过期，不转投新轮次。

自己的协作程序可通过 subprocess 调 CLI，或导入 sync.py 的 register / opt_in / heartbeat / inbox / claim / ack。指令应白名单处理，消费者自行落实工作范围、权限和证据回报。没有默认任意 shell 执行器，也没有替 Dot 内部 agent 消费队列的桥接器。

### 独立 CLI 回显演示

~~~powershell
# 运行到 Ctrl+C；也可加 --seconds 30 自动停止
python -B .\sync.py --project sandbox demo
~~~

只回显文本，回报明确注明 not executed；不调用模型，不应作为真实工作完成证据。

## 接入其他项目：只读适配器

旧 SQLite 适配器为 legacy-orchestrator，不绑定具体项目名称或路径：

~~~powershell
python -B .\sync.py --project another-project source-add local-legacy --path "D:\AnotherProject\.localdata\orchestrator.sqlite3" --label "另一项目的只读调度状态"
python -B .\sync.py sources
python -B .\sync.py --project another-project status
~~~

所需表 / 列：
- tasks：id, spec（JSON object）, status, lease, candidate, round
- runs：id, task, model, effort, started, finished, status
- settings（可选）：key, value；只展示 paused

源连接使用 SQLite URI mode=ro，并设置 PRAGMA query_only=ON。不会向旧库登记 worker、发事件、改 paused 或任务，不修改旧项目文件。

running 但最新 run 已 finished 显示 needs_reconciliation；租约过期显示 stale；租约有效只标 unverified_running，不声称进程工作中。坏 JSON、二进制字段等异常按单条 invalid_record 降级；坏源不会妨碍其他源显示。

source ID 在整个库中唯一，不覆盖现有来源。此版没有删除 worker / 来源 / 数据的功能，避免误删。

一次性 CLI 只读观察可用 --source，不保存配置，固定归属 default 项目，不会因项目查询换归属：

~~~powershell
python -B .\sync.py --source "D:\AnotherProject\legacy.sqlite3" status
~~~

serve 不接受临时 --source 或 --project；请先 source-add 保存明确项目归属，再在网页切换项目。

其他工具 / 数据库 / 进程需要另写适配器或协作消费者。没有通用办法把旧 worker 变为支持中途指令的 agent。

## 真实网页 → 新自有 Codex 会话 worker（显式开启）

这不是回显演示。网页可以通过官方 App Server 控制本服务新建并持有连接的真实 Codex 会话。**不是接管已有 Dot / Codex 工作会话，也没有给模型授予现有项目的环境访问。**

Windows 推荐双击 **start-owned-codex.cmd**：它只查找并核验已安装的官方商店版 Codex 应用中的 CLI，找不到或无法访问时明确报错并退出，不安装软件，也不自动创建 worker。打开窗口显示的本机 URL，用窗口中的会话密钥解锁；不要分享密钥。真实模型请求会消耗账户额度。

如普通 start-ui.cmd 已占用默认 8765 端口，先在原窗口按 Ctrl+C 停止服务，或使用另一端口（例如 start-owned-codex.cmd --port 8766），再打开新窗口显示的 URL。普通启动器保持不变。

也可以手动指定已安装的官方 CLI：

~~~powershell
$cli = Join-Path (Get-AppxPackage OpenAI.Codex).InstallLocation 'app\resources\codex.exe'
python -B .\sync.py serve --codex-cli "$cli"
~~~

先解锁页面，再点击「新建真实 Codex 会话 worker」。初始只注册新会话，不自动发送模型任务。之后在指令表单选中它：

- 「排队」：worker 空闲时调用 turn/start，开启下一轮。
- 「追加到当前活跃轮次」：仅对当前自有 active turn 调用 turn/steer，并绑定 expectedTurnId；轮次变化就拒绝，不偷偷改发下一轮。
- 「请求停止」：仅 interrupt 本服务创建的会话，再等待轮次终态与自有进程退出。停止请求或 API 受理都不代表已经停止。

固定请求 gpt-6.1-sol / max、标准速度，不自动替换模型。真实调用会消耗账户额度，空闲监听不持续生成 token。每服务默认最多 1 个真实 worker，单轮 120 秒触发中断，输出限制 8,000 字符。额外审批、凭据和未请求工具请求一律拒绝。

新线程使用 ephemeral=true、environments=[]，每次 turn/start 再次明确 environments=[]。服务会确认官方返回的环境列表为空。还以受支持开关关闭插件、MCP、网页、浏览器、多 agent、shell、hooks 和 memories 等能力。**它是环境访问关闭的会话型 worker，可以回答和生成文本建议，不是可读写项目文件的编码 worker。** 隔离 canary 的读取负向验收已通过；这不应扩大解释为对所有未来运行时工具的通用安全证明。

注册后 worker 会一直保持本服务的自有连接，可接受多个后续轮次，直到手动停止、服务关闭或异常。工作目录由服务在独立数据目录下创建，不接受网页传入 CLI 路径、任意 threadId 或环境覆盖。已有会话不会被 resume/adopt；重启不会自动恢复旧模型 worker 或重发未确认指令。

本地队列、桥接领取、API 受理、模型轮次结束分开显示。轮次结束不是任务独立验收。中途追加可能改变原目标，同一轮次内的多条指令会共享最终模型回报。RPC 结果未知就保留 delivery_unknown，停止该 worker，不自动重试。clientUserMessageId 不当作官方 exactly-once 保证。

锁定面板不停止正在运行的 worker；请用停止按钮或关闭服务。旧动作不会被中断操作撤销。数据和新工作目录保留本地，不自动删除。

### 显式真实验收（会消耗模型额度）

普通 unittest 和基础 browser QA 不调用模型。只有显式传入受信任 CLI 的以下脚本才会创建隔离新模型会话：

~~~powershell
python -B .\run_owned_worker_qa.py --cli "$cli"
python -B .\run_owned_browser_qa.py --cli "$cli"
~~~

真实 HTTP / Chrome 验收：注册新 worker → 模型调用受控 wait → 当场生成随机 nonce 后追加 → 同一 turn 最终结果精确匹配 → 下一轮 → 活跃中中断 → 自有进程退出。不会向实际工作会话发送测试。生成报告和截图仍只在本地 verification/，不进入 Git。

## 官方 App Server：真实会话元数据，默认关闭

此版还支持用户信任的、官方已安装 Codex 的只读元数据 API，不是手工造出的演示记录。路径存在或后缀为 .exe 并不构成身份验证，不要把来历不明的程序配置为 CLI。接口依据：[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)。**它不是 Dot 平台消息接口，也不是另一个桌面进程的实时控制连接。**

必须显式指定官方 CLI 路径和精确项目目录。不同项目分别配置来源；不默认枚举全账号聊天，也不读取消息正文、preview 或历史文件。

~~~powershell
# Windows 官方已安装应用中的 CLI，无需下载或提取登录凭据
$cli = Join-Path (Get-AppxPackage OpenAI.Codex).InstallLocation 'app\resources\codex.exe'

# 一次性只读探测，不保存配置，不启动模型
python -B .\sync.py native-status --cli "$cli" --cwd "D:\YourProject"

# 确认范围后，显式加入某项目的面板数据源
python -B .\sync.py --project alpha source-add alpha-native --adapter codex-app-server --path "$cli" --cwd "D:\YourProject" --label "alpha 原生会话元数据（只读）"
~~~

如果旧版本面板仍在运行，先在原终端 Ctrl+C，再重新启动，保留已有数据库即可。

适配器只调用 initialize、thread/list 和 thread/loaded/list。thread/list 明确使用 useStateDbOnly=true，禁止为修复元数据扫描历史 JSONL；指定 cwd 精确过滤，并再次检查返回记录的 cwd。只保留 ID、标题、项目路径、来源类型、记录时间和该适配器进程返回的状态。每项目最多 200 条、15 秒缓存，页面显示实际查询时间。

**notLoaded 只表示会话未在这个短暂适配器进程中加载，不表示桌面会话已停止、已完成或空闲。** 新进程的 loaded 列表为空，也不能证明其他进程没有活跃 agent。标题缺失时显示 untitled，不从消息正文推断标题。

不会调用 thread/resume、thread/read、历史分页、turn/start、turn/steer、shell、工具审批或凭据接口；查询后优雅关闭自己启动的进程，不停止桌面或已有 daemon。原生元数据记录不会进入网页指令目标列表。若子进程清理未能确认完成，本工具进程会将该 CLI 熔断，停止继续拉起并报告不可用；不会无限等待管道锁，也不会声称未知辅助进程已经退出。需要人工检查后重启工具，不应反复盲目重试。

### 已经实证与尚未接通的部分

- 已实际通过官方 API 查询本机指定项目的保存会话元数据。
- 已通过平台会话消息接口，新建专用隔离验收任务，再投递第二条消息并读取真实固定回执。没有向任何实际工作会话发测试。
- 这只证明平台工具侧可以投递。**这个平台消息接口仍未直接连接到 localhost 后端，网页不能据此给已有原生 Dot 会话发指令。** 新增网页桥接仅控制本服务自己创建的新 Codex 会话，两者不同。
- 官方已有 daemon 的 proxy 在本机探测中未能连接。没有猜测私有 IPC、提取 token，或新建 daemon 冒充控制原会话。
- 当前网页能投递到主动接入本地协作收件箱协议的 worker，以及显式开启后由本服务新建的真实 Codex 会话。若要在网页控制已有平台任务，需要后续提供受支持的桥接连接，不能仅凭一份 supportsSendMessage 快照假装已接通。

## 数据、安全和边界

- 默认可写库：data/sync.sqlite3。备份前停止服务和写入该库的消费者，再复制整个 data 文件夹。
- 初版同步器库运行 init 做增量迁移，保留原记录。sync.initial.py.txt 是初版代码备份，不是数据备份。
- 拒绝把不相关数据库当作可写 sync 库。旧项目库只能作为只读 source。
- HTTP 检查 loopback Host、同源 Origin、Bearer 密钥、JSON 类型和请求大小；不授权跨域。用户 / 数据库内容用 textContent 输出。
- 每个视图显示最近 100 条指令；选项目时由服务端按项目取最近 100 条，不会被另一项目新消息挤掉。
- 本地数据不加密，不适用于共享不可信电脑、公网或生产环境。拥有本机账户 / 文件权限的程序仍能读数据。
- 没有后台自启、跨机器联网、权限分级、通用进程探活、任意命令执行或推送。真实模型仅限显式启用的新自有会话，不调度或接管已有会话。
- 正常停止服务会停止自身演示；强制结束进程后，旧演示记录稍后变 stale，不虚报在线。

## 验证与复现

~~~powershell
# 从仓库根目录运行
$env:PYTHONUTF8 = '1'
python -B -m unittest -v test_sync test_native_adapter test_codex_bridge test_owned_launcher
node --check .\ui\app.js
python -B .\run_browser_qa.py
~~~

测试使用隔离临时库，不写旧项目。浏览器 QA 额外需要 Node 22+（内置 WebSocket）和本机 Chrome，默认：
C:\Program Files\Google\Chrome\Application\chrome.exe

可用 TEAM_SYNC_CHROME 指定已安装的官方浏览器可执行文件。QA 只用自己的临时 headless 配置，不读取日常浏览器配置或登录状态；结束后清理临时输入。

截图和不含密钥的记录在 verification/。原生会话 UI 截图使用明确标注的隔离 fixture，不包含真实私有标题或正文；真实元数据与平台消息验收只保存不含私有内容的结果摘要。这些测试不证明任何实际项目工作已经完成。

## Windows 下载

[下载 AAMS Windows ZIP](https://github.com/WhatDoUMing/codex-agents-team-sync/raw/refs/heads/main/release/aams-windows/AAMS-Windows.zip) · [中文使用指南](release/aams-windows/README.md) · [SHA-256 校验](release/aams-windows/SHA256SUMS.txt)

下载后，在 AAMS 选择业务项目和部门即可查看员工卡、本人任务和最近心跳；卡片把最新命令的排队、当次领取与当次回报分开展示，旧命令则保留自己的 ID 并明确标成上次结果。符合条件的岗位可点「纠正此 Agent」。接入已登记的本地 worker 时，按[中文使用指南](release/aams-windows/README.md) 对原岗位原位 opt-in，并确认原消费者继续在同一数据库和项目中更新心跳、实际轮询、领取及回报。opt-in 不会接管或启动聊天会话。
