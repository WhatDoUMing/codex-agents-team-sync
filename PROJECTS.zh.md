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

## 项目委托

在岗位登记的 `project` 中填写 `"manager":{"id":"project-manager","event_type":"studio.delegate","rule":"项目委托判断"}`。在单独的委托部门设置 `"delegation_only":true`、负责人及其 `scope_type` 和 `scope_rule`。该部门经项目经理分派，员工名单沿用登记的工作岗位和专用 Loop 路由。

总主管从项目页选择「提交项目委托」，填写任务号、委托目标和验收条件。该操作对应经鉴权的 `POST /api/aams/delegations`，参数为 `request_id`、`project`、`task_id`、`expected_version`、`instruction` 和 `acceptance`。首次提交的当前版号为 0，后续通过「修订委托」沿同一任务号递增。项目经理使用本次事件的 `AAMS/2 <event.id> DEPARTMENT 部门ID | 分派范围与理由`。AAMS 核对实际轮次和会话后交给独立的部门负责人。

负责人根据本次可控员工和任务范围，按 `AAMS/2 <event.id> STAFF | [["员工ID","具体子任务范围"],["员工ID","具体子任务范围"]]` 返回自己的安排。第一个竖线分隔命令和数组，数组内的 JSON 字符串可以包含竖线。每个子任务沿用父单版号并记录分派来源。执行顺序采用回执数组的顺序。后续任务等待上一笔通过并可重新核对的实录。员工可通过绑定的 `APPLIED` 或 `BLOCKED` 回传中间状态，现场证据齐备后可从看板续收同版结果。旧版的已领取工作保留原回执，尚未派发的旧版分支在修订时失效。

员工按事件提供的 `evidence_file` 保存实际 JSON，再用 `APPLIED_RESULT` 回传该路径。JSON 至少包含 `change_id`、`task_id`、`version` 与 `outcome`（`passed` 或 `failed`）。用于交付的员工可登记 `"proof_kind":"pr"`，随前序实测候选报告 `candidate`、`commit` 和 GitHub `pr`。随后负责复核的独立岗位登记 `"proof_kind":"review"`，核对同一候选、提交和 PR 并回传自己的实录。其余员工默认为 `result`。`AAMS_DELEGATION_EVIDENCE_ROOT` 指定项目委托的现场目录，默认沿用已有的项目证据目录。

项目页可回看各级范围、子任务编号、实际接单身份、Loop 事件、轮次、会话与实录。使用「暂停后续委托派发」可以冻结新派发，已领取工作按原轮次核对。

## 工单证据与独立审核

启动前将 `AAMS_PROJECT_EVIDENCE_ROOT` 指向本机业务证据目录。执行者可提交该目录下的 `field/record-<change_id>.json`。记录写入顶层 `change_id`，如包含 `task` 或 `task_id`，内容要和这份回执的当前任务一致。已核验并跨版复用的基线 `field/candidate.json` 使用任务号、源清单和脚本 SHA-256 标明来源。看板只读取与相应回执或复核关联的文件。

独立审核员将实录、候选、提交、PR 和结论附加到有结果的业务单。每份记录保留审核人及来源，可在工单详情重新打开。审核员须已登记，且没有在该单担任范围判断人或执行者。对接本机独立审核使用经鉴权的 `POST /api/aams/reviews`，字段为 `change_id`、`reviewer`、`review_run`、`conclusion`、`candidate`、`commit`、`pr` 和 `evidence`。本地审核员将 `review_run` 留作 JSON 的 `null`，看板标为本地自报。另有专用 Loop 审核事件时，按岗位登记的规则、版本和工作证据核对实际轮次。
