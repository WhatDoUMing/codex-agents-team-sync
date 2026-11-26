"use strict";
const $ = id => document.getElementById(id);
const states = {idle:"待命（自报）",running:"工作中（自报）",blocked:"受阻（自报）",completed:"已完成（自报）",failed:"失败（自报）",stopped:"已停止（自报）",stale:"心跳 / 租约已过期",clock_skew:"时间异常",invalid_timestamp:"无有效心跳时间",needs_reconciliation:"状态冲突，待核对",unverified_running:"租约有效，进程未验证",invalid_record:"数据异常",queued:"仅本地排队",acknowledged:"消费者已领取，未完成",rejected:"消费者已拒绝"};
let token = sessionStorage.getItem("team-sync-session") || "";
let latest = null, refreshBusy = false, commandAttempt = null, demoAttempt = null;
let sessionEpoch = 0, viewEpoch = 0, refreshSequence = 0;
const knownProjects = new Set();
let ownedAttempt=null, composerFlight=null;
function deliveryMode(){
 const target=$("target").value;let worker=null;
 if(target&&latest){const [p,id]=JSON.parse(target);worker=latest.workers.find(w=>w.project===p&&w.id===id);}
 const owned=worker&&worker.native&&worker.native.owned_here;
 const busy=!!composerFlight;
 $("target").disabled=busy;
 $("instruction").disabled=busy;
 $("delivery-mode").disabled=busy||!owned;
 if(!owned)$("delivery-mode").value="queue";
 $("delivery-mode").options[1].disabled=!(owned&&worker.native.can_steer);
 $("send").disabled=busy||!(worker&&worker.can_queue)||($("delivery-mode").value==="steer"&&!(owned&&worker.native.can_steer));
}
function obsolete(error){return error && error.name==="ObsoleteSession";}
const date = value => typeof value === "number" ? new Date(value*1000).toLocaleString() : "未知";
const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; };
const id = () => crypto.randomUUID().replaceAll("-","");
function notice(text, error=false){ $("notice").textContent=text; $("notice").className=error?"error":""; }
function badge(state){ return el("span","badge "+(["stale","clock_skew","needs_reconciliation","unverified_running","blocked","acknowledged"].includes(state)?"warn":(["failed","invalid_record","invalid_timestamp","rejected"].includes(state)?"bad":(["stopped","completed"].includes(state)?"dim":""))),states[state]||state); }
function commandBadge(c){
 if(!c.native_delivery)return badge(c.state);
 const phase=c.native_delivery.phase;
 if(phase==="delivery_unknown")return el("span","badge warn","结果未确认，禁止自动重发");
 if(phase==="turn_interrupted")return el("span","badge warn","模型轮次已中断");
 if(c.state==="completed")return el("span","badge dim","模型轮次结束，非任务验收");
 if(c.state==="acknowledged")return el("span","badge warn",["turn_accepted","steer_accepted"].includes(phase)?"API已受理，模型未结束":"本地已领取，API待确认");
 if(c.state==="rejected")return el("span","badge bad","投递被拒或轮次未成功");
 return badge(c.state);
}
async function request(path, body){
  const expectedToken=token, expectedEpoch=sessionEpoch;
  const options={method:body?"POST":"GET",headers:{"Authorization":"Bearer "+token},cache:"no-store"};
  if(body){ options.headers["Content-Type"]="application/json"; options.body=JSON.stringify(body); }
  const response=await fetch(path, options);
  const data=await response.json();
  if(expectedEpoch!==sessionEpoch || expectedToken!==token){ const error=new Error("obsolete session response");error.name="ObsoleteSession";throw error; }
  if(!response.ok){ if(response.status===401)lock(); throw new Error(data.error||"请求失败"); }
  return data;
}
function lock(){
  clearOrder();
  $("loop-control").hidden=true;$("control-result").textContent="";$("loop-rule").replaceChildren();$("loop-rule").disabled=true;$("control-model").textContent="";$("loop-probe").hidden=true;$("probe-dispatch").disabled=true;$("loop-source").textContent="尚未读取 Loop 状态";$("loop-rules").textContent="";$("loop-runs").replaceChildren();sessionEpoch++;viewEpoch++;refreshSequence++;refreshBusy=false;token="";sessionStorage.removeItem("team-sync-session");latest=null;knownProjects.clear();$("login").hidden=false;$("dashboard").hidden=true;$("logout").hidden=true;$("access-key").value="";$("workers").replaceChildren();$("commands").replaceChildren();$("sources").replaceChildren();$("target").replaceChildren();$("instruction").value="";$("project").replaceChildren(el("option","","全部项目"));$("project").firstChild.value="";commandAttempt=null;demoAttempt=null;ownedAttempt=null;composerFlight=null;$("instruction").disabled=false;$("target").disabled=false;$("delivery-mode").value="queue";$("delivery-mode").disabled=true;}
function render(snapshot){
  const firstRender=$("dashboard").hidden;
  latest=snapshot;
  $("login").hidden=true;$("dashboard").hidden=false;$("logout").hidden=false; if(firstRender){clearOrder();void refreshOrder();void refreshLoop();void refreshProbe();void refreshControl();}
  const project=$("project").value;
  const enabled=!!(snapshot.owned_codex&&snapshot.owned_codex.enabled);
  $("start-owned").hidden=!enabled;
  $("owned-note").textContent=enabled?"真实会话模式：gpt-6.1-sol / max，环境访问关闭，不接管旧会话；120秒触发中断，会消耗账户额度。锁定面板不停止worker。":"真实会话桥接默认关闭；用serve --codex-cli显式启用。不会自动启动模型或接管原会话。";
  snapshot.projects.forEach(p=>knownProjects.add(p));
  const projects=new Set(knownProjects);
  if(project)projects.add(project);
  $("project").replaceChildren(el("option","", "全部项目"));
  $("project").firstChild.value="";
  [...projects].sort().forEach(p=>{const n=el("option","",p);n.value=p;$("project").append(n);});
  $("project").value=project;
  const workers=snapshot.workers.filter(w=>!project||w.project===project);
  const commands=snapshot.commands.filter(c=>!project||c.project===project);
  const sources=snapshot.sources.filter(s=>!project||s.project===project);
  $("count-workers").textContent=workers.length;
  $("count-fresh").textContent=workers.filter(w=>w.freshness==="fresh").length;
  $("count-queued").textContent=commands.filter(c=>c.state==="queued").length;
  $("count-sources").textContent=sources.length;
  $("updated").textContent="读取时间 "+date(snapshot.observed_at)+" · 每 3 秒刷新";
  $("workers").replaceChildren();
  workers.forEach(w=>{
    const card=el("article","worker");
    card.append(el("div","meta",w.project+" / "+w.team+" · "+w.role));
    const top=el("div","worker-top");top.append(el("h4","",w.id),badge(w.status));card.append(top);
    card.append(el("p","task",w.task),el("p","detail",w.detail));
    if(w.native){
      card.append(el("p","detail","自有会话状态："+w.native.state+" · 环境访问关闭 · "+(w.native.verified?"已有真实模型应答":"模型应答尚未验证")));
      if(w.native.owned_here&&!(["stopped","failed"].includes(w.native.state))){
        const stop=el("button","secondary","请求停止此 worker");
        stop.addEventListener("click",async()=>{stop.disabled=true;try{await request("/api/owned/stop",{project:w.project,worker:w.id});notice("已发送停止请求，等待轮次终态及自有进程退出确认。旧动作不会被撤销。");await refresh(true);}catch(e){if(!obsolete(e))notice("停止尚未确认："+e.message,true);}});card.append(stop);
      }
    }
    const kind=w.native?"本服务新建的真实 Codex 会话（不等于已有团队）":w.is_demo?"协议回显演示（不执行）":w.cooperative?"声明协作协议（非自动发现）":"手工登记（无收件箱）";
    card.append(el("div","capability",kind+" · "+(w.freshness==="terminal"?"已终止":w.age_seconds===null?"心跳时间异常":w.age_seconds+" 秒前心跳")+" · "+(w.inbox_observed?"曾见收件箱轮询（非进程验证）":"尚未见消费者轮询")));
    $("workers").append(card);
  });
  if(!workers.length)$("workers").append(el("div","empty","此项目还没有登记 worker。可先启动回显演示，或通过 CLI 接入你自己的协作程序。"));
  const selected=$("target").value;
  $("target").replaceChildren();
  const available=workers.filter(w=>w.can_queue);
  available.forEach(w=>{const n=el("option","",w.project+" / "+w.team+" / "+w.id+(w.native?" [真实会话 / 环境关闭]":w.is_demo?" [回显演示]":" [声明协作]"));n.value=JSON.stringify([w.project,w.id]);$("target").append(n);});
  if([...$("target").options].some(o=>o.value===selected))$("target").value=selected;
  if(!available.length){const n=el("option","","暂无新鲜且声明协作的目标");n.value="";$("target").append(n);}
  $("send").disabled=!available.length;
  deliveryMode();
  $("commands").replaceChildren();
  commands.forEach(c=>{
    const card=el("article","command"),head=el("header");
    head.append(el("strong","",c.project+" / "+c.worker),commandBadge(c));card.append(head,el("p","text",c.text));
    if(c.native_delivery){
      const d=c.native_delivery;
      card.append(el("p","meta","真实桥接："+d.mode+" · "+d.phase+" · "+d.note));
    }
    if(c.result)card.append(el("p","result",(c.native_delivery?"Codex轮次回报（不等于任务独立验收）：":"消费者回报：")+c.result));
    card.append(el("p","meta","ID "+c.id+" · 创建 "+date(c.created)+(c.claimed?" · 领取 "+date(c.claimed):"")+(c.completed?" · 结束回报 "+date(c.completed):"")));
    $("commands").append(card);
  });
  if(!commands.length)$("commands").append(el("div","empty","暂无指令。加入队列后，只有主动轮询并领取的消费者才会收到。"));
  $("sources").replaceChildren();
  sources.forEach(s=>{
    const card=el("article","source"),head=el("header");
    head.append(el("strong","",s.project+" / "+s.label),el("span","badge dim",s.available?(s.adapter==="codex-app-server"?"官方会话元数据 · 只读":"只读 · 无 steer"):"源不可读"));card.append(head);
    card.append(el("p","path",s.adapter+" · "+s.path));
    if(s.adapter==="codex-app-server"){
      card.append(el("p","detail","精确项目范围："+(s.scope_cwd||"未配置")));
      card.append(el("p","detail","notLoaded 仅表示未在本适配器的新进程内加载，不表示桌面会话已停止或空闲。这里不读取消息正文、不恢复会话、不发指令。"));
      if(s.observed_at)card.append(el("p","meta","元数据读取："+date(s.observed_at)+(s.cached?" · 15秒内缓存":"")+" · 此适配器内已加载数："+(s.loaded_count_in_adapter??"未知")));
      (s.threads||[]).forEach(t=>{
        const row=el("div","legacy-task"),head=el("div","worker-top");
        head.append(el("strong","",t.name),el("span","badge dim",t.reported_runtime_status==="notLoaded"?"未在本适配器加载":t.reported_runtime_status+"（仅此进程）"));
        row.append(head,el("p","path",t.id+" · "+(t.cwd||"未知项目路径")));
        row.append(el("p","meta","来源 "+t.source_kind+" · 记录更新 "+date(t.updated_at)+" · 不可从此网页投递"));
        card.append(row);
      });
      if(s.available&&!(s.threads||[]).length)card.append(el("p","detail","该精确项目范围没有可列出的数据库元数据。"));
      if(s.truncated)card.append(el("p","detail","仅展示此项目最多200条元数据，列表已截断。"));
    }
    if(s.error)card.append(el("p","detail",s.error));
    if(s.paused!==undefined)card.append(el("p","detail","源设置 paused = "+String(s.paused)+"（不在此修改）"));
    s.tasks.forEach(t=>{
      const task=el("div","legacy-task"),head=el("div","worker-top");
      head.append(el("strong","",String(t.id)),badge(t.status));task.append(head,el("p","",t.task||"记录无法解析"));
      task.append(el("p","",t.reason||t.error||"旧源原始状态："+String(t.reported_status)));
      if(t.latest_run)task.append(el("p","","最近 run: "+t.latest_run.model+" / "+t.latest_run.effort+" · "+t.latest_run.status));
      card.append(task);
    });
    $("sources").append(card);
  });
  if(!sources.length)$("sources").append(el("div","empty","没有自动接入任何项目。使用 source-add 为任意项目配置受支持的只读适配器。"));
}
async function refresh(quiet=false,force=false){
  if(!token||(refreshBusy&&!force))return;
  refreshBusy=true;
  const sequence=++refreshSequence, expectedView=viewEpoch, project=$("project").value;
  try{
    const snapshot=await request("/api/snapshot"+(project?"?project="+encodeURIComponent(project):""));
    if(expectedView!==viewEpoch)return;
    render(snapshot);if(!quiet)notice("已读取本地状态。所有状态均保留其证据边界。");
  }
  catch(e){if(!obsolete(e)&&expectedView===viewEpoch)notice("读取失败："+e.message,true);}
  finally{if(sequence===refreshSequence)refreshBusy=false;}
}
$("login-form").addEventListener("submit",async e=>{
  e.preventDefault();sessionEpoch++;viewEpoch++;token=$("access-key").value.trim();
  try{const data=await request("/api/snapshot");sessionStorage.setItem("team-sync-session",token);$("access-key").value="";render(data);notice("已连接本地会话。");}
  catch(e){if(!obsolete(e))notice("解锁失败："+e.message,true);}
});
$("logout").addEventListener("click",()=>{lock();notice("面板已锁定，当前标签页的会话密钥已清除。");});
$("target").addEventListener("change",deliveryMode);
$("delivery-mode").addEventListener("change",deliveryMode);
$("refresh").addEventListener("click",()=>{void refresh();void refreshOrder();});
$("project").addEventListener("change",()=>{
  viewEpoch++;latest=null;$("target").replaceChildren();$("send").disabled=true;
  $("workers").replaceChildren(el("div","empty","正在读取所选项目…"));
  $("commands").replaceChildren();$("sources").replaceChildren();
  ["count-workers","count-fresh","count-queued","count-sources"].forEach(n=>$(n).textContent="…");
  refresh(false,true);
});
$("command-form").addEventListener("submit",async e=>{
  e.preventDefault();if(composerFlight||!latest||!$("target").value||$("send").disabled)return;
  const [project,worker]=JSON.parse($("target").value),text=$("instruction").value.trim();
  if(($("project").value&&$("project").value!==project)||!latest.workers.some(w=>w.project===project&&w.id===worker&&w.can_queue)){
    notice("目标已不属于当前视图，请等待刷新后重新选择。",true);return;
  }
  if(!text){notice("请填写指令内容。",true);return;}
  const target=latest.workers.find(w=>w.project===project&&w.id===worker);
  const owned=!!(target&&target.native&&target.native.owned_here),mode=owned?$("delivery-mode").value:"queue";
  if(!commandAttempt||commandAttempt.project!==project||commandAttempt.worker!==worker||commandAttempt.text!==text||commandAttempt.mode!==(owned?mode:undefined)){
    commandAttempt={project,worker,text,command_id:id()};
    if(owned)Object.assign(commandAttempt,{mode,expected_turn:mode==="steer"?target.native.active_turn:null});
  }
  const attempt=commandAttempt, flight={attempt,epoch:sessionEpoch};
  composerFlight=flight;deliveryMode();
  try{
    const result=await request(owned?"/api/owned/commands":"/api/commands",attempt);
    if($("instruction").value.trim()===attempt.text)$("instruction").value="";
    if(commandAttempt===attempt)commandAttempt=null;
    notice(result.state==="queued"?"已加入本地队列，指令 ID："+result.id+"。这不表示消费者已接收或执行。":"该指令已存在，未重复排队。ID："+result.id+"；当前状态："+(states[result.state]||result.state)+"。");
    await refresh(true);
  }catch(e){if(obsolete(e)||flight.epoch!==sessionEpoch)return;notice("投递未确认（ID "+attempt.command_id+"）："+e.message+"。内容与目标不变时重试将使用同一指令 ID，避免重复排队。",true);}
  finally{if(composerFlight===flight){composerFlight=null;deliveryMode();}}
});
$("start-demo").addEventListener("click",async()=>{
  const project=$("project").value||"demo";
  if(!demoAttempt||demoAttempt.project!==project)demoAttempt={project,request_id:id()};
  $("start-demo").disabled=true;
  try{const result=await request("/api/demo",demoAttempt);demoAttempt=null;notice("已启动 "+result.id+"。它仅回显文本，不是实际模型 agent，不会执行指令。关闭服务会停止演示。");await refresh(true);}
  catch(e){if(obsolete(e))return;notice("演示启动未确认："+e.message,true);}
  finally{$("start-demo").disabled=false;}
});
$("start-owned").addEventListener("click",async()=>{
 const project=$("project").value||"codex";
 if(!ownedAttempt||ownedAttempt.project!==project)ownedAttempt={project,request_id:id()};
 $("start-owned").disabled=true;
 try{const r=await request("/api/owned/start",ownedAttempt);ownedAttempt=null;notice("正在创建真实会话 "+r.id+"。尚未开始模型任务；额度在实际模型调用时消耗。不会接管任何已有会话。");await refresh(true);}
 catch(e){if(!obsolete(e))notice("创建未确认："+e.message,true);}
 finally{$("start-owned").disabled=false;}
});
setInterval(()=>refresh(true),3000);
async function connectOnStartup(){
  if(!location.hash.startsWith("#launch=")){
    if(token)await refresh(true);
    return;
  }
  const ticket=location.hash.slice("#launch=".length);
  history.replaceState(null,"",location.pathname+location.search);
  const epoch=++sessionEpoch;
  token=""; // Do not poll with a previous session while redeeming the launch ticket.
  const form=$("login-form");
  const fields=Array.from(form.elements);
  fields.forEach(f=>{f.disabled=true;});
  try{
    const response=await fetch("/api/session/launch",{
      method:"POST",headers:{"Content-Type":"application/json"},
      cache:"no-store",body:JSON.stringify({ticket})
    });
    const data=await response.json();
    if(epoch!==sessionEpoch)return;
    if(!response.ok||typeof data.token!=="string")throw new Error("automatic connection failed");
    token=data.token;
    const snapshot=await request("/api/snapshot");
    sessionStorage.setItem("team-sync-session",token);
    render(snapshot);
  }catch(e){
    if(!obsolete(e)&&epoch===sessionEpoch){
      lock();
      notice("\u81ea\u52a8\u8fde\u63a5\u672a\u6210\u529f\uff0c\u53ef\u4f7f\u7528\u7ec8\u7aef\u91cc\u7684\u5907\u7528\u5bc6\u94a5\u3002",true);
    }
  }finally{fields.forEach(f=>{f.disabled=false;});}
}

const loopStates={queued:"排队",claimed:"已领取",running:"执行中",done:"已完成",failed:"失败",cancelled:"已取消",canceled:"已取消",stopped:"已停止",retrying:"待重试",unknown:"未知"};
let loopLoading=false;
async function refreshLoop(){
  if(!token||loopLoading)return;
  loopLoading=true;
  const epoch=sessionEpoch;
  try{
    const data=await request("/api/loop/status");
    if(epoch!==sessionEpoch)return;
    const online=data.status==="online";
    const stamped=data.observed_at&&new Date(data.observed_at);
    $("loop-source").textContent=online
      ? data.source+"｜最近读取："+(stamped&&!Number.isNaN(stamped.valueOf())?stamped.toLocaleString():"时间未知")
      : data.status==="disabled"?"未启用 Loop 只读接入":"Loop 数据暂不可用，未沿用上次结果";
    $("loop-rules").textContent=online&&data.rules.length?"已接入规则："+data.rules.join("、"):"";
    $("loop-runs").replaceChildren();
    if(!online)return;
    if(!data.runs.length)$("loop-runs").append(el("p","detail","近期没有已匹配的潮核轮次。"));
    for(const run of data.runs){
      const item=el("article","card");
      const label=run.state==="claimed"?"已领取，执行未单独核验":loopStates[run.state]||"未知";
      item.append(el("strong","","#"+run.id+"　"+(run.rule||"未命名任务")),el("p","detail",label+"｜"+(run.agent||"执行者未知")));
      if(run.updated_at){const stamp=new Date(run.updated_at);if(!Number.isNaN(stamp.valueOf()))item.append(el("p","detail",stamp.toLocaleString()));}
      $("loop-runs").append(item);
    }
  }catch(e){
    if(epoch!==sessionEpoch)return;
    $("loop-source").textContent="Loop 数据读取失败";
    $("loop-rules").textContent="";
    $("loop-runs").replaceChildren();
  }finally{loopLoading=false;}
}

$("loop-refresh").addEventListener("click",refreshLoop);
setInterval(()=>{if(!document.hidden)void refreshLoop();},15000);

let probeBusy=false;
const probeLabels={ready:"只读测试就绪",queued:"已入队",claimed:"已领取",done:"收到完成回执",failed:"Loop 报告执行失败",unavailable:"隔离 Loop 不可用",delivery_unknown:"派发结果未知，不自动重发",awaiting_receipt:"等待同一单号的回执",already_attempted:"已尝试过，不重复派发"};
async function refreshProbe(){
  if(!token||probeBusy)return;
  const epoch=sessionEpoch;
  try{
    const p=await request("/api/loop/probe");
    if(epoch!==sessionEpoch||probeBusy)return;
    $("loop-probe").hidden=!p.enabled;
    $("probe-dispatch").disabled=!p.can_dispatch;
    $("probe-status").textContent=(p.run?"#"+p.run+"｜":"")+(probeLabels[p.state]||"状态未确认")+(p.state==="done"?(p.receipt_matches?"，回执匹配":"，回执未匹配"):"");
  }catch(e){if(epoch===sessionEpoch){$("probe-dispatch").disabled=true;$("probe-status").textContent="无法确认隔离测试状态";}}
}
$("probe-dispatch").addEventListener("click",async()=>{
  if(probeBusy||$("probe-dispatch").disabled)return;
  probeBusy=true;$("probe-dispatch").disabled=true;
  const epoch=sessionEpoch;
  try{await request("/api/loop/probe",{});}
  catch(e){if(epoch===sessionEpoch)$("probe-status").textContent="派发结果未知，不自动重发";}
  finally{probeBusy=false;if(epoch===sessionEpoch)void refreshProbe();}
});
setInterval(()=>{if(!document.hidden&&!$("loop-probe").hidden)void refreshProbe();},2500);

let controlBusy=false, controlRead=0;
const controlLabels={run:"立即运行",pause:"暂停后续派发",resume:"恢复派发"};
const controlPrompts={run:"在本机 Loop 派发一单？",pause:"暂停这条规则的后续派发？已经领取的任务不会被停止。",resume:"恢复这条规则的后续派发？这不会补跑暂停期间跳过的事件。"};
async function refreshControl(){
  if(!token||controlBusy)return;
  const epoch=sessionEpoch, rev=++controlRead;
  try{
    const chosen=$("loop-rule").value;
    const data=await request("/api/loop/control"+(chosen?"?rule="+encodeURIComponent(chosen):""));
    if(epoch!==sessionEpoch||rev!==controlRead||controlBusy)return;
    $("loop-control").hidden=data.status==="disabled";
    const selector=$("loop-rule"), roster=data.roster||[];
    if(roster.length){
      const current=roster.find(x=>x.name===(chosen||data.rule))||roster[0];
      selector.replaceChildren(...roster.map(x=>{const o=el("option","",x.name);o.value=x.name;return o;}));
      selector.value=current.name;
      selector.disabled=false;
      $("control-model").textContent=current.configured_model?"配置档位："+current.configured_model+" / "+current.configured_effort:"原有梳理规则";
      if(data.rule!==current.name){void refreshControl();return;}
    }else{selector.disabled=true;}
    $("control-status").textContent=data.status==="online"
      ? (data.paused?"派发已暂停":data.active?"有任务排队或已领取":"可派发")
      : data.status==="uncertain"?"上次操作未确认，已锁定控制":data.status==="rule_missing"?"未找到绑定的潮核规则":"Loop 未连接";
    for(const name of Object.keys(controlLabels))$("loop-"+name).disabled=!data.allowed.includes(name);
  }catch(e){
    if(epoch!==sessionEpoch||rev!==controlRead)return;
    $("control-status").textContent="无法读取控制状态";
    for(const name of Object.keys(controlLabels))$("loop-"+name).disabled=true;
  }
}
$("loop-rule").addEventListener("change",()=>{
  $("control-result").textContent="";
  for(const name of Object.keys(controlLabels))$("loop-"+name).disabled=true;
  void refreshControl();
});
for(const name of Object.keys(controlLabels))$("loop-"+name).addEventListener("click",async()=>{
    const rule=$("loop-rule").value;
  if(controlBusy||!rule||$("loop-"+name).disabled||!window.confirm(rule+"\n"+controlPrompts[name]))return;
  const epoch=sessionEpoch;
  controlBusy=true;++controlRead;$("loop-rule").disabled=true;
  for(const k of Object.keys(controlLabels))$("loop-"+k).disabled=true;
  const reply=$("control-result");
  reply.textContent="正在等待 Loop 确认…";
  try{
    const result=await request("/api/loop/control",{action:name,rule});
    if(epoch!==sessionEpoch)return;
    reply.textContent=result.run?"已入队，单号 #"+result.run:(result.paused?"Loop 已确认暂停":"Loop 已确认恢复");
  }catch(e){if(epoch===sessionEpoch)reply.textContent=e.message;}
  finally{controlBusy=false;void refreshControl();if(epoch===sessionEpoch)void refreshLoop();}
});
setInterval(()=>{if(!document.hidden)void refreshControl();},5000);

const orderStates = {
  ready:"待派发", sending:"发出结果待确认", queued:"已派发", claimed:"已接单", running:"执行中",
  unknown:"运行状态待核对", delegated:"管理员已分派", acknowledged:"已确认版号", reported:"执行者已交结果，待独立验收",
  blocked:"受阻", decision:"待决定", unverified:"回执待核对", unrouted:"未派发", superseded:"新版已替换，未投递"
};
let displayedVersion=null, editingVersion=null, orderBusy=false, orderRead=0;
let orderSignature="";
const orderImages=new Set();
function clearOrderImages(){
  orderImages.forEach(src=>URL.revokeObjectURL(src));
  orderImages.clear();
}

function clearOrder(){
  ++orderRead;
  displayedVersion=null; editingVersion=null; orderBusy=false;
  orderSignature=""; clearOrderImages();
  $("aams-order").hidden=true;
  $("order-form").reset(); $("order-history").replaceChildren(); $("order-notice").textContent="";
  $("order-status").textContent=""; $("order-submit").disabled=true;
}
function orderFormState(){
  const stale = editingVersion!==null && editingVersion!==displayedVersion;
  $("order-rebase").hidden=!stale;
  $("order-submit").disabled=orderBusy || stale || displayedVersion===null;
  $("order-instruction").disabled=orderBusy;
  $("order-acceptance").disabled=orderBusy;
  $("order-submit").textContent=displayedVersion?"发布改令":"下达首版指令";
  $("order-next").textContent=stale?"编辑期间出现新版，请核对下方记录。":
    (displayedVersion===null?"读取指令中":"将发布 v"+(displayedVersion+1));
}
function renderOrder(data){
  $("aams-order").hidden=!data.enabled;
  if(!data.enabled)return;
  displayedVersion=data.latest;
  const signature=JSON.stringify(data);
  if(signature===orderSignature){orderFormState();refreshEvidence();return;}
  orderSignature=signature; clearOrderImages();
  $("order-status").textContent=data.latest?
    "当前指令 v"+data.latest+" · "+(data.effective?"执行者已确认 v"+data.effective:"等待执行者确认版号"):"尚无主管指令";
  const cards=data.orders.map(order=>{
    const card=el("article","command");
    card.append(el("h4","","v"+order.version+" · "+date(order.created)),
      el("p","text",order.instruction),el("p","detail","验收条件　"+order.acceptance));
    for(const leg of order.deliveries){
      const ids=[leg.event_id && "事件 #"+leg.event_id,leg.run_id && "轮次 #"+leg.run_id].filter(Boolean).join(" · ");
      card.append(el("p","result",leg.agent+" · "+(orderStates[leg.state]||leg.state)+(ids?" · "+ids:"")));
      for(const detail of [leg.result,leg.evidence && "证据　"+leg.evidence,leg.note]){
        if(detail)card.append(el("p","detail",detail));
      }
      if(leg.decision_run_id)card.append(el("p","detail","决策续办轮次 #"+leg.decision_run_id));
      if(leg.role!=="manager" && ["reported","blocked"].includes(leg.state)){
        const evidence=el("div","route-evidence");
        evidence.dataset.version=order.version;
        evidence.dataset.run=leg.run_id;
        card.append(evidence);
      }
      if(order.version===data.latest && leg===order.deliveries.at(-1) && leg.role!=="manager"
          && ["acknowledged","blocked"].includes(leg.state)){
        const follow=el("button","secondary","请原执行者补交同版实录");
        follow.type="button"; card.append(follow);
        follow.addEventListener("click",async()=>{
          follow.disabled=true;
          try{
            await request("/api/aams/order/continue",{expected_version:order.version,after_run:leg.run_id});
            await refreshOrder();
          }catch(err){if(!obsolete(err))$("order-notice").textContent=err.message;}
          finally{follow.disabled=false;}
        });
      }
    }
    return card;
  });
  $("order-history").replaceChildren(...cards);
  refreshEvidence();
  orderFormState();
}
function refreshEvidence(){
  $("order-history").querySelectorAll(".route-evidence").forEach(container=>void displayEvidence(container));
}
function discardEvidenceFrames(container){
  for(const img of container.querySelectorAll("img.order-frame")){
    URL.revokeObjectURL(img.src);
    orderImages.delete(img.src);
  }
}
async function displayEvidence(container){
  if(container.dataset.reading)return;
  container.dataset.reading="true";
  const query="/api/aams/evidence?version="+container.dataset.version+"&run="+container.dataset.run;
  const epoch=sessionEpoch;
  if(!container.hasChildNodes())container.textContent="正在核对执行者引用的本机实录";
  try{
    const result=await request(query);
    if(epoch!==sessionEpoch || !container.isConnected)return;
    const signature=JSON.stringify(result);
    if(signature===container.dataset.signature)return;
    container.dataset.signature=signature;
    discardEvidenceFrames(container);
    const normalSummary=result.same_candidate?"两条路线的候选指纹一致，实录可查":"实录核对";
    const summary=el("p","detail",normalSummary), failedPreviews=new Set();
    const updateSummary=()=>{summary.textContent=failedPreviews.size?
      "实录核对 · "+[...failedPreviews].join("、")+"的截图预览失败":normalSummary;};
    container.replaceChildren(summary);
    for(const route of result.routes){
      if(route.outcome==="missing"){container.append(el("p","detail",route.label+" · 缺少可核对的实录"));continue;}
      const outcome=route.outcome==="completed"?"到达终点":"记录到首个卡点";
      container.append(el("p","detail",route.label+" · "+outcome+" · "+route.detail+
        " · "+route.keys+" 个按键边沿 · "+route.screenshots+" 张视口截图"),
        el("p","detail","候选 SHA-256　"+route.candidate));
      const button=el("button","secondary","查看"+route.label+"最后一帧");
      container.append(button);
      button.addEventListener("click",async()=>{
        button.disabled=true;
        let src;
        try{
          const response=await fetch(query+"&frame="+route.route,{headers:{Authorization:"Bearer "+token},cache:"no-store"});
          if(!response.ok)throw new Error("截图读取失败");
          const blob=await response.blob();
          if(epoch!==sessionEpoch||!button.isConnected)return;
          src=URL.createObjectURL(blob);
          const img=el("img","order-frame");
          img.src=src;img.alt=route.label+"的实机视口";
          try{await img.decode();}catch{throw new Error("浏览器无法解码截图");}
          if(epoch!==sessionEpoch||!button.isConnected)return;
          orderImages.add(src);src=null;button.replaceWith(img);
          failedPreviews.delete(route.label);updateSummary();
        }catch(e){
          if(epoch===sessionEpoch && button.isConnected){
            failedPreviews.add(route.label);updateSummary();
            button.textContent=e.message+"，点击重试";
          }
        }finally{
          if(src)URL.revokeObjectURL(src);
          button.disabled=false;
        }
      });
    }
    result.issues.forEach(issue=>container.append(el("p","detail",issue)));
  }catch(e){
    if(!obsolete(e)&&epoch===sessionEpoch && container.isConnected){
      discardEvidenceFrames(container);
      delete container.dataset.signature;
      container.textContent="实录尚未核对";
    }
  }finally{delete container.dataset.reading;}
}
async function refreshOrder(){
  if(!token||orderBusy)return;
  const seq=++orderRead;
  try{
    const data=await request("/api/aams/order");
    if(seq===orderRead && !orderBusy)renderOrder(data);
  }catch(e){
    if(seq===orderRead && !obsolete(e)){
      displayedVersion=null; orderFormState();
      $("order-status").textContent="看板连接中断，请刷新核对当前指令。";
    }
  }
}
$("order-form").addEventListener("input",()=>{if(editingVersion===null)editingVersion=displayedVersion;});
$("order-rebase").addEventListener("click",()=>{editingVersion=displayedVersion;orderFormState();});
$("order-form").addEventListener("submit",async event=>{
  event.preventDefault();
  if($("order-submit").disabled)return;
  const epoch=sessionEpoch;
  orderBusy=true; ++orderRead; orderFormState();
  try{
    const result=await request("/api/aams/order",{
      expected_version:editingVersion===null?displayedVersion:editingVersion,
      instruction:$("order-instruction").value, acceptance:$("order-acceptance").value
    });
    if(epoch!==sessionEpoch)return;
    $("order-notice").textContent="已记录 v"+result.version+"，接单状态见下方回执。";
    $("order-form").reset(); editingVersion=null;
  }catch(e){if(epoch===sessionEpoch)$("order-notice").textContent=e.message;}
  finally{if(epoch===sessionEpoch){orderBusy=false;void refreshOrder();}}
});
setInterval(()=>{if(!document.hidden&&!$("dashboard").hidden)void refreshOrder();},3000);
void connectOnStartup();
