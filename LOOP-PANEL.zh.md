# AAMS 潮核 Loop 看板

本机操作入口先显示项目首页。新建、打开、部门主线调整、员工纠正和独立审核的用法见[项目与部门工作池](PROJECTS.zh.md)。原鱼摊任务、规则与诊断可从详情打开。

双击 `start-loop-control.cmd` 打开本机操作看板。AAMS 用当前标签页的会话密钥保护接口，默认地址为 `http://127.0.0.1:8766/`。浏览器会自动连接，手动新开标签页时可以使用终端显示的备用密钥。已有 Loop Hub 应当在本机 3350 端口运行。

## 首章鱼摊指令

在「潮核协议 · 首章鱼摊复测」填写主管指令和验收条件。第一次发布生成 v1，后续改令保留任务号 `TP-CH01-FISH-01`。状态栏分别显示最新指令和执行者已确认的版号，历史记录保留每版的事件、轮次、结果与证据。

管理员的 Loop 回执明确分派给 `tp-playtest` 后，看板才投递给执行者。执行者可确认版号、报告受阻，或交回复测结果。单个 Loop 轮次的终态回执固定。当前版最后一份执行者回执为 ACK 或 BLOCKED 时，点击「请原执行者补交同版实录」，由同一执行者在新的轮次核对本版已有实录。旧回执可从历史中对照。

启动前可将环境变量 `AAMS_EVIDENCE_ROOT` 指向该任务本机的 `qa_runs` 目录。在其上一级任务目录保留取证工具生成的 source-manifest.json，供看板核对实际候选与鱼摊脚本的指纹。执行者在回执最后一栏写出正常线和折返线各自的 `qa_runs/<运行目录>/summary.json`，用中文分号分隔。看板核对每份摘要的版号、路线、启动指纹、按键轨迹和视口 PNG，提供最后一帧预览。点击「立即刷新」可重新核对实录。两条路线使用同一候选、鱼摊脚本指纹也一致时，页面显示「实录可查」。独立审核核对技术结果，项目负责人试玩后确认操作体验。

### 配置两个 Loop 岗位

在已有本地来源 `local` 和实际已注册的 worker 上，按下例将两个岗位和对应规则加入自己的 Loop 配置。把 `your-worker` 替换为当前 worker 名，把两个 `cwd` 替换为各自的本机绝对目录。管理员只读交接，试玩岗使用独立副本保存实录。先用 `agent-autoloop check` 校验，再按本机 Loop 的生效方式载入。

```json
{
  "agents": {
    "tp-manager": {"name": "潮核项目管理员", "use": "codex", "worker": "your-worker", "access": "read", "maxActive": 1},
    "tp-playtest": {"name": "潮核试玩复测", "use": "codex", "worker": "your-worker", "access": "edit", "maxActive": 1}
  },
  "rules": [
    {
      "name": "AAMS·潮核项目管理员", "when": {"source": "local", "types": ["tp.aams.manager"]},
      "to": "tp-manager", "cwd": "<管理员本地目录>", "tags": ["tidecore", "tp-manager", "aams"],
      "prompt": "读取本轮 context.events 的 detail，核对 task、version 和实际 event.id。根据本版指令决定 DELEGATE tp-playtest 或 BLOCKED，最终首行使用 receipt_format 的完整绑定码。",
      "tell": "never", "maxActive": 1
    },
    {
      "name": "AAMS·潮核试玩复测", "when": {"source": "local", "types": ["tp.aams.playtest"]},
      "to": "tp-playtest", "cwd": "<独立试玩目录>", "tags": ["tidecore", "tp-playtest", "aams"],
      "prompt": "读取本轮 context.events 的 detail，按 instruction、acceptance 和 continuation 执行。按 receipt_format 用简短首行交 ACK、RESULT 或 BLOCKED，附实际结果及可打开的版本证据。续收时仅核验既有实录。",
      "tell": "never", "maxActive": 1
    }
  ]
}
```

2026-11-26 白天的隔离验收用独立 Loop、浏览器和标记文件验证过一次真实 Codex 读取。另有固定命令 worker 测试经理分派、版号和回执。11 月 26 日晚的首章正式任务使用真实主管、管理员和试玩执行者，结果见本机操作看板中的相应轮次。

## 只读看板与团队控制

`start-loop-panel.cmd` 打开只读看板，读取 Loop CLI 的 `status --json`，显示属于潮核的规则和轮次。它和操作版是两个启动方式。操作版保留现有岗位的运行、暂停后续派发和恢复按钮。暂停后，已领取的轮次继续运行。恢复后，已跳过的事件仍可在历史中核对。

需复测独立的浏览器标记流程时，在仓库根目录运行 `python -B verify_loop_panel.py`。可以用 `TEAM_SYNC_CODEX_CLI` 指定可信 Codex 的完整路径，公开版也可查找官方 Windows 安装。测试用当前 `CODEX_HOME` 或当前用户的默认目录。
