# 项目和部门工作池

双击 `start-loop-control.cmd`。本机首次打开及重开会自动使用短期、一次性的浏览器票据。项目记录保存在原看板的 SQLite 中。重新打开已经运行的同一实例，也可以进入已有项目。

从首页打开或新建本地项目。项目页显示部门摘要。进入部门可查看主线、负责人和员工当前任务，可在工单详情中查看谁接收、谁已落实及谁提交了结果。

## 登记团队

在项目的工具详情导入本地岗位文件，或运行 `python aams_projects.py --db data/sync.sqlite3 import roster.json`。下面是文件格式。事件类型与规则名须与本机 Loop 上已注册的岗位一致，岗位使用本机 worker 的 Codex 后端。只登记一个名字和任务的员工会显示为观察岗位。

```json
{
  "project": {"id":"studio","name":"开发项目","reviewers":{"independent-reviewer":{}}},
  "departments": [{
    "id":"engineering","name":"工程部","lead":"engineering-lead","mainline":"验证当前改动",
    "scope_type":"studio.scope","scope_rule":"工程部主线判断",
    "employees":[
      {"id":"engineering-lead","name":"工程负责人","task_id":"ENGINEERING-SCOPE","task_title":"判断本部门影响范围"},
      {"id":"tester","name":"测试员","task_id":"TEST-01","task_title":"核对候选","event_type":"studio.apply.tester","rule":"测试员工单"}
    ]
  }]
}
```

部门调整有自己的版号。看板把本版指令、验收要求和登记的在办任务发给负责人。负责人选择受影响的岗位，首行按事件里的完整绑定码回复 `AAMS/2 <event.id> SCOPE tester | 选择原因`。多名员工用英文逗号分隔。没有受影响的员工可回 `SCOPE NONE | 原因`。看板只向选定任务派发。

「纠正此 Agent」对选中的员工及其当前任务单独编号。岗位通过 Loop 领取后，回执中保留该单的实际时间和轮次。落实调整后可回 `APPLIED | 已采用的调整 | 证据`，同版补交结果时会收到新的核对轮次。直接交结果可回 `APPLIED_RESULT | 实际结果 | 证据`。这两种格式均须带事件提供的 `AAMS/2 <event.id>` 前缀，遇到卡点按 `BLOCKED | 卡点 | 证据` 回报。暂停部门只阻止以后的派发。恢复后继续核对原事件。

卡片模型和思考档位取自该次 Loop 领取和结束之间、同一 Codex 会话的 `turn_context`。无法核实的字段显示未知。管理员可以通过岗位登记的 `history_run` 关联一轮先前工作，详情会说明历史来源，本轮接单仍由本轮回执判断。

## 工单证据与独立审核

启动前将 `AAMS_PROJECT_EVIDENCE_ROOT` 指向本机业务证据目录。执行者可提交该目录下的 `field/record-<change_id>.json`。记录写入顶层 `change_id`，如包含 `task` 或 `task_id`，内容要和这份回执的当前任务一致。已核验并跨版复用的基线 `field/candidate.json` 使用任务号、源清单和脚本 SHA-256 标明来源。看板只读取与相应回执或复核关联的文件。

独立审核员将实录、候选、提交、PR 和结论附加到有结果的业务单。每份记录保留审核人及来源，可在工单详情重新打开。审核员须已登记，且没有在该单担任范围判断人或执行者。对接本机独立审核使用经鉴权的 `POST /api/aams/reviews`，字段为 `change_id`、`reviewer`、`review_run`、`conclusion`、`candidate`、`commit`、`pr` 和 `evidence`。本地审核员将 `review_run` 留作 JSON 的 `null`，看板标为本地自报。另有专用 Loop 审核事件时，按岗位登记的规则、版本和工作证据核对实际轮次。
