# AAMS Windows 使用说明

解压 ZIP 后运行 `AAMS.exe`。看板只绑定本机 `127.0.0.1`，默认使用端口 `8766` 并打开浏览器。首次打开看板、登记项目和浏览岗位卡不需要 Agent Autoloop Hub，也不需要另装 Python。安装目录可以只读。

如果浏览器没有自动打开，请使用控制台打印的本地地址手动打开。备用会话密钥在本次运行期间有效，请勿分享；自动打开浏览器使用的短期启动票据才是一次性的。保持控制台窗口打开，直到准备正常关闭服务。

## 数据和正常关闭

默认项目数据库为 `%LOCALAPPDATA%\AAMS\sync.sqlite3`，属于当前 Windows 用户；项目实录和委托实录分别保存在同一目录下的 `project-evidence` 和 `delegation-evidence`。数据不写进 EXE 或安装目录。同一 Windows 用户再次运行时，选择「打开项目」即可找回先前登记的项目。

正常关闭时，聚焦 AAMS 控制台并按 Ctrl+C。确认本次 `AAMS.exe` 进程已经退出、监听端口已经释放后，再运行 EXE 并打开同一项目。默认端口为 8766 时，可在 PowerShell 查看：

~~~powershell
Get-Process AAMS -ErrorAction SilentlyContinue
Get-NetTCPConnection -State Listen -LocalPort 8766 -ErrorAction SilentlyContinue
~~~

两条命令均无匹配结果表示没有对应进程或监听；若启动时使用了其他端口（例如 `$env:AAMS_PORT = '0'`），请改查控制台打印的实际端口。`AAMS dashboard stopped.` 提示和退出码是本次运行的观察值，单独出现其中任何一项都不能证明进程和监听已经退出。不要用任务管理器结束进程，也不要把关闭窗口当作退出确认。

需要指定其他本机证据目录时，在启动 EXE 的 PowerShell 中设置绝对路径：

~~~powershell
$env:AAMS_PROJECT_EVIDENCE_ROOT = 'D:\AAMS-data\projects'
$env:AAMS_DELEGATION_EVIDENCE_ROOT = 'D:\AAMS-data\delegations'
.\AAMS.exe
~~~

EXE 会为这些目录创建 `field` 子目录。目录应为当前用户可写，并按用户和项目分开保存。

## 登记项目与岗位

1. 在看板新建或打开业务项目，并在项目下新建、打开一个部门。
2. 打开部门，展开「工具与诊断」，选择「导入岗位登记」。
3. 选择 UTF-8 JSON 文件；文件中的 `project.id` 必须与当前打开的项目编号一致。导入后回到项目页；登记了 `manager` 的项目会显示项目委托入口。

下面是虚构的最小示例。事件类型和规则名要与所选 Hub 中的路由一致；示例名称不会连接任何真实执行者。

~~~json
{
  "project": {
    "id": "demo-mart",
    "name": "演示商店",
    "manager": {
      "id": "demo-manager",
      "event_type": "demo.project",
      "rule": "demo-project-manager"
    }
  },
  "departments": [
    {
      "id": "demo-ops",
      "name": "演示运维组",
      "lead": "demo-lead",
      "mainline": "维护测试项目的演示记录",
      "delegation_only": true,
      "scope_type": "demo.department",
      "scope_rule": "demo-department-lead",
      "employees": [
        {
          "id": "demo-lead",
          "name": "虚构负责人",
          "task_id": "demo-triage",
          "task_title": "拆分演示委托",
          "event_type": "demo.staff",
          "rule": "demo-staff-router"
        },
        {
          "id": "demo-worker",
          "name": "虚构执行者",
          "task_id": "demo-readme",
          "task_title": "核对示例说明",
          "event_type": "demo.execute",
          "rule": "demo-worker"
        }
      ]
    }
  ]
}
~~~

## 员工卡：查看状态和发起纠正

打开 AAMS，选择业务项目，再打开所属部门。员工卡会显示岗位登记任务、员工本人任务和最近心跳；摘要区把「最新指令」「本次领取」「本次回报」对应到同一条最新命令。展开「工作详情」可查看心跳来源、本人自报说明、最近收件箱轮询、本次命令 ID，以及领取人、时间和回报内容。若旧命令已领取或回报、但更新的命令才刚排队，详情会把旧记录标为「上次领取」「上次回报」并保留它自己的命令 ID，不会显示成新命令的结果。模型和思考级别如果来自员工自己的心跳，会标为「本人自报」，不能当作独立核实的模型身份。

符合条件时，点击员工卡上的「纠正此 Agent」提交该岗位的个人纠正。若按钮不可用，查看该岗位是否处于活动状态、最近有心跳且确实轮询过同一项目的收件箱，以及项目和部门是否暂停。跨项目、演示、仅观察或 Loop 镜像岗位不会因此变成普通收件箱员工。Loop 岗位沿原有 Loop 路由处理。

### 接入已经登记的本地岗位

AAMS 看板不会自动接管本机聊天，也不会因为开启一个标记就启动消费者。若已有本地 worker 支持 AAMS 收件箱协议，可在**原登记、原项目和原数据库**中原位开启协作；不要重新注册同一个岗位或重置 generation。岗位的 `team` 必须与登记部门一致。worker 所属程序还须实际、持续地更新心跳并轮询同一库和项目，领取后处理并回报；一次 opt-in 或一次性 CLI 查询不会代替这个消费者。

执行 opt-in 只需要 Python 3.10+ 和标准库，不需要 pip 第三方包。Windows EXE 的看板不需要 Python。`sync.py` 可从[公开源码仓库](https://github.com/WhatDoUMing/codex-agents-team-sync/blob/main/sync.py)取得。先在 PowerShell 中填写本机 EXE 使用的数据库、准确项目编号和已有 worker ID：

~~~powershell
$sync = 'D:\AAMS-source\sync.py'
$db = Join-Path $env:LOCALAPPDATA 'AAMS\sync.sqlite3'
python -B $sync --db $db --project '<实际项目编号>' opt-in '<已有worker ID>'
~~~

命令会在本机隐藏提示输入该 worker 的现有 generation。只在本机提示中输入，不要把它写进聊天或分享给他人。此操作只启用原登记的收件箱，不新建岗位、不替代 Agent、Hub、事件路由或执行端。请确认原消费者正在同一个项目和数据库上保持心跳与实轮询；满足活动状态、心跳和近期轮询条件后，刷新项目并打开部门，员工卡会显示「近期轮询」，符合范围时才可点「纠正此 Agent」。

### 接入 Loop 员工

如需把个人纠正派到 Agent Autoloop worker，需选定本机 Hub、已配置的匹配路由和可用执行端；Codex 执行端还需要该 Windows 用户已登录 Codex。若 Hub 使用非默认目录，按 Hub 自身说明启动并确认路由。登记岗位、打开看板和浏览员工卡本身不需要 Hub。

员工收到工作后，最终回复的**第一行**必须原样使用事件详情 `detail.receipt_format` 给出的完整当次绑定回执，说明可以放在下一行。例如：

~~~text
AAMS/2 <本次实际 event.id> APPLIED_RESULT | 实际结果 | 实际证据路径
~~~

提交时要把占位符替换成事件给出的实际事件码和真实结果。若本阶段是提交调整，按事件提供的格式使用 `APPLIED`；遇到阻塞时使用 `BLOCKED` 和实际卡点/证据。不要在绑定回执前加摘要，否则看板可能无法把结果关联到当前事件。

## 选择本机 Hub 并实际派单

默认不启用项目委托派发。要明确使用一个本机 Hub，在启动 EXE 前同时设置 Hub 目录和监听端口：

~~~powershell
$env:AAMS_PROJECT_HUB_HOME = 'D:\AAMS-Hub'
$env:AAMS_PROJECT_HUB_PORT = '34350'
.\AAMS.exe
~~~

所选目录中的 `config.json` 必须将 `url` 设为 `http://127.0.0.1:<端口>`，并将 `hub.listen` 设为 `127.0.0.1:<端口>`。两个 Hub 参数都未设置时，项目委托入口保持禁用。只设置一个参数，或参数与 `config.json` 不匹配时，AAMS 会在开始监听前退出；先修正环境变量和配置再启动。参数和配置都正确但 Hub 当前离线时，看板可以启动，实际派单会失败；重新启动可用 Hub 后再发单。

AAMS 不会自动连接固定的 3350 端口。测试或个人使用时，为 AAMS、Hub 选择自己的数据目录和空闲回环端口，不要指向其他正在运行的 Hub。需要改变 AAMS 看板端口时，可在启动前设置 `$env:AAMS_PORT`；设为 `0` 会选择空闲端口，实际地址显示在控制台。实际派单还要求 Hub 有匹配的角色路由和可用执行端，AAMS EXE 不会安装 Hub、启动模型或代替用户创建路由。

## 每用户数据与隔离使用

每个 Windows 用户有独立的 `%LOCALAPPDATA%\AAMS\sync.sqlite3`、项目证据和委托证据。若要使用另一套隔离数据目录，可在启动前设置当前 PowerShell 的 `LOCALAPPDATA`、证据根和 Hub 目录；再用空闲回环端口启动 AAMS 和测试 Hub。确保这些路径和端口只属于本次隔离运行，关闭后按上文确认进程和监听已释放，再以同一隔离路径重开核对同一项目与事件。

Windows 下载入口与 SHA-256 校验见[仓库首页](https://github.com/WhatDoUMing/codex-agents-team-sync/blob/main/README.md)。随包包含 Agent Autoloop MIT 许可证和运行所需第三方许可证文本。
