# AAMS Windows 使用说明

解压 ZIP 后运行 `AAMS.exe`。看板只绑定本机 `127.0.0.1`，默认使用端口 `8766` 并在浏览器打开本地地址；安装目录可以只读。首次打开看板、新建项目和导入岗位登记不需要 Agent Autoloop Hub。

如果浏览器没有自动打开，请使用控制台打印的本地地址手动打开。可以手动连接的**备用会话密钥在本次运行期间有效，请勿分享**。它不是一次性票据；自动打开浏览器使用的短期启动票据才是一次性的。保持控制台窗口打开，直到按下 Ctrl+C 并看到 `AAMS dashboard stopped.`。

## 数据和正常关闭

默认项目数据库为 `%LOCALAPPDATA%\AAMS\sync.sqlite3`，属于当前 Windows 用户；项目实录和委托实录分别保存在同一目录下的 `project-evidence` 和 `delegation-evidence`。数据不写进 EXE 或安装目录。以同一 Windows 用户再次运行时，选择「打开项目」即可找回之前登记的项目。

正常关闭时，先聚焦 AAMS 控制台窗口并按 Ctrl+C，等控制台显示 `AAMS dashboard stopped.` 再关闭窗口。随后再次运行 `AAMS.exe`，在「打开项目」中选择原项目。不要用任务管理器结束进程，也不要把关闭窗口当作退出确认。

需要指定其他本机证据目录时，在启动 EXE 的 PowerShell 中设置绝对路径：

~~~powershell
$env:AAMS_PROJECT_EVIDENCE_ROOT = 'D:\AAMS-data\projects'
$env:AAMS_DELEGATION_EVIDENCE_ROOT = 'D:\AAMS-data\delegations'
.\AAMS.exe
~~~

EXE 会为这些目录创建 `field` 子目录。目录应为当前用户可写，并按用户和项目分开保存。

## 登记四级岗位

1. 在看板新建或打开项目，并在该项目下新建一个部门。
2. 打开部门，展开「工具与诊断」，选择「导入岗位登记」。
3. 选择一个 UTF-8 JSON 文件。文件中的 `project.id` 必须与当前打开的项目编号一致。导入后回到项目页；配置了 `manager` 的项目会显示项目委托入口。

给员工岗位编写任务提示时，要求执行者把该事件 `detail.receipt_format` 指定的完整回执直接放在最终回复第一行，填入本次实际事件码；解释和说明从第二行开始。格式示意：`AAMS/2 <event.id> APPLIED_RESULT | 实际结果 | 证据`。`<event.id>`、状态、结果和证据均按本次事件的实际值填写；若该事件允许 `APPLIED` 或 `BLOCKED`，按它提供的回执格式选择。

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

## 配置本机 Hub 并实际派单

项目登记、部门配置和本地看板可以单独使用。实际下达委托还需要本机 Agent Autoloop Hub、匹配的事件路由和可用执行端；使用 Codex 员工时还需要该 Windows 用户已登录 Codex。若 Codex 使用非默认目录，同时把 `CODEX_HOME` 设为该用户的 Codex 数据目录。若所选 Hub 依赖 Node，请先按 Hub 自身说明安装并启动。AAMS EXE 不安装 Hub、不启动模型、不代替用户创建岗位路由。

默认不启用项目委托派发。要明确选择一个本机 Hub，在启动前同时设置 Hub 目录和监听端口：

~~~powershell
$env:AAMS_PROJECT_HUB_HOME = 'D:\AAMS-Hub'
$env:AAMS_PROJECT_HUB_PORT = '34350'
.\AAMS.exe
~~~

所选目录中的 `config.json` 必须声明 `url` 为 `http://127.0.0.1:<端口>`，并声明 `hub.listen` 为 `127.0.0.1:<端口>`；AAMS 会在启动时核对这两个值，并在实际派单时确认本机 Hub 的路由和岗位。Hub 目录或端口只设一个、配置不匹配或 Hub 不可用时，派单会失败并提示核对配置。未明确配置时，派单入口保持禁用。

EXE 不读取旧 CAS 控制开关，也不会自动连到固定的 3350 端口。测试时请为 AAMS 和 Hub 选择自己的数据目录及空闲回环端口，不要指向其他正在运行的 Hub。若需把 AAMS 看板放到空闲端口，可先设置 `$env:AAMS_PORT = '0'`；控制台会打印本次实际地址。

首次打开看板只依赖 EXE 和本地浏览器。实际派单还取决于 Hub、已经配置的角色路由、执行端和各自的登录状态。用户可在 PowerShell 中使用 `$env:LOCALAPPDATA` 指定隔离的 Windows 用户数据目录，再设置以上证据根和 Hub 目录，完成独立的登记、发令、收件与重开检查。

## 构建

在 Windows 上安装 Python 和 PyInstaller 后运行：

~~~powershell
pyinstaller --noconfirm --clean aams_windows.spec
~~~

EXE 输出到 `dist\AAMS.exe`。ZIP 中包含 Agent Autoloop MIT 许可证和随包运行所需的第三方许可证文本。
