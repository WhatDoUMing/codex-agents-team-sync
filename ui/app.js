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
  if(!response.ok){
    if(response.status===401)lock();
    const error=new Error(data.error||"请求失败");
    error.status=response.status;
    throw error;
  }
  return data;
}
function lock(){
  clearOrder();
  resetWorkspace();
  $("loop-control").hidden=true;$("control-result").textContent="";$("loop-rule").replaceChildren();$("loop-rule").disabled=true;$("control-model").textContent="";$("loop-probe").hidden=true;$("probe-dispatch").disabled=true;$("loop-source").textContent="尚未读取 Loop 状态";$("loop-rules").textContent="";$("loop-runs").replaceChildren();sessionEpoch++;viewEpoch++;refreshSequence++;refreshBusy=false;token="";sessionStorage.removeItem("team-sync-session");latest=null;knownProjects.clear();$("login").hidden=false;$("dashboard").hidden=true;$("logout").hidden=true;$("access-key").value="";$("workers").replaceChildren();$("commands").replaceChildren();$("sources").replaceChildren();$("target").replaceChildren();$("instruction").value="";$("project").replaceChildren(el("option","","全部项目"));$("project").firstChild.value="";commandAttempt=null;demoAttempt=null;ownedAttempt=null;composerFlight=null;$("instruction").disabled=false;$("target").disabled=false;$("delivery-mode").value="queue";$("delivery-mode").disabled=true;}
function render(snapshot){
  const firstRender=$("dashboard").hidden;
  latest=snapshot;
  $("login").hidden=true;$("dashboard").hidden=false;
  if(firstRender){
    activateWorkspace();
    clearOrder();void refreshOrder();void refreshLoop();void refreshProbe();void refreshControl();
  }
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
  try{const data=await request("/api/snapshot");sessionStorage.setItem("team-sync-session",token);$("access-key").value="";render(data);notice("");}
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
  sessionStorage.removeItem("team-sync-active-project");
  sessionStorage.removeItem("team-sync-active-department");
  token=""; // Do not poll with a previous session while redeeming the launch ticket.
  $("connection-status").textContent="正在连接本地应用…";
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

const workspace = {
  projectId: null, departmentId: null, project: null, revision: 0, sequence: 0, loading: false,
  fingerprint: null, dialog: null,
};
const workspaceAgentStates = {
  online: "在线", offline: "离线", unknown: "待确认", observing: "仅观察",
  idle: "待命", running: "执行中", blocked: "受阻", pending_review: "待验收",
  completed: "已完成", paused: "暂停派发", ready: "待接单",
};
const workspaceDeliveryStates = {
  ready: "待派发", sending: "派发中", queued: "已排队", claimed: "已接单",
  running: "执行中", unknown: "结果待确认", decision: "待决策", scoped: "已确认范围",
  applied: "已生效", reported: "有结果", blocked: "有卡点", unverified: "待核验",
  unrouted: "未送达", reconciling: "旧版对账中", superseded: "已被新版替代",
};

function workspaceButton(text, onClick, className = "secondary") {
  const button = el("button", className, text);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function workspaceMessage(text, error = false) {
  const status = $("workspace-feedback");
  status.textContent = text;
  status.classList.toggle("workspace-feedback-error", error);
}

function resetWorkspace() {
  returnToWorkspaceHome(false);
  $("project-workspace").hidden = true;
  $("connection-status").textContent = "从 AAMS 启动器打开此页面。";
}

function activateWorkspace() {
  $("project-workspace").hidden = false;
  const savedProject = sessionStorage.getItem("team-sync-active-project");
  if (savedProject) void openWorkspaceProject(savedProject, false, sessionStorage.getItem("team-sync-active-department"));
}

function returnToWorkspaceHome(focus = true) {
  workspace.revision++;
  workspace.projectId = null;
  workspace.departmentId = null;
  workspace.project = null;
  workspace.fingerprint = null;
  workspace.loading = false;
  sessionStorage.removeItem("team-sync-active-project");
  sessionStorage.removeItem("team-sync-active-department");
  closeWorkspaceDialog();
  $("project-view").hidden = true;
  $("department-view").hidden = true;
  $("project-home").hidden = false;
  $("workspace-tools").open = false;
  $("workspace-tools").hidden = true;
  $("logout").hidden = true;
  $("department-cards").replaceChildren();
  $("department-detail-card").replaceChildren();
  workspaceMessage("");
  if (focus) $("project-home-heading").focus();
}

async function openWorkspaceProject(projectId, focus = true, departmentId = null) {
  const revision = ++workspace.revision;
  workspace.projectId = projectId;
  workspace.departmentId = departmentId;
  workspace.project = null;
  workspace.fingerprint = null;
  $("project-home").hidden = true;
  $("project-view").hidden = false;
  $("department-view").hidden = true;
  $("workspace-tools").open = false;
  $("workspace-tools").hidden = true;
  $("workspace-add-department").hidden = true;
  $("logout").hidden = false;
  $("workspace-project-name").textContent = "本地项目";
  $("department-cards").replaceChildren();
  $("department-detail-card").replaceChildren();
  workspaceMessage("正在打开项目…");
  await refreshWorkspaceProject(true);
  if (revision === workspace.revision && focus) $("workspace-project-name").focus();
}

async function refreshWorkspaceProject(force = false) {
  if (!token || !workspace.projectId || (workspace.loading && !force)) return;
  const revision = workspace.revision;
  const sequence = ++workspace.sequence;
  const projectId = workspace.projectId;
  workspace.loading = true;
  try {
    const data = await request("/api/aams/project?" + new URLSearchParams({project: projectId}));
    if (revision !== workspace.revision || sequence !== workspace.sequence) return;
    if (data.id !== projectId || !Array.isArray(data.departments)) throw new Error("项目数据无效");
    const fingerprint = JSON.stringify(data);
    if (fingerprint !== workspace.fingerprint) {
      renderWorkspaceProject(data);
      workspace.fingerprint = fingerprint;
    }
    sessionStorage.setItem("team-sync-active-project", projectId);
    if ($("workspace-feedback").textContent === "正在打开项目…" ||
        $("workspace-feedback").dataset.readError === "true") workspaceMessage("");
    delete $("workspace-feedback").dataset.readError;
  } catch (error) {
    if (obsolete(error) || revision !== workspace.revision || sequence !== workspace.sequence) return;
    const status = $("workspace-feedback");
    status.dataset.readError = "true";
    workspaceMessage("读取项目失败。请核对连接或返回项目列表。", true);
  } finally {
    if (revision === workspace.revision && sequence === workspace.sequence) workspace.loading = false;
  }
}

function renderWorkspaceProject(project) {
  workspace.project = project;
  $("workspace-project-name").textContent = project.name;
  $("workspace-add-department").hidden = false;
  const selected = project.departments.find(department => department.id === workspace.departmentId);
  $("project-view").hidden = Boolean(selected);
  $("department-view").hidden = !selected;
  $("workspace-tools").hidden = !selected;
  if (selected) {
    $("workspace-department-name").textContent = selected.name;
    sessionStorage.setItem("team-sync-active-department", selected.id);
    renderSelectedWorkspaceDepartment(project, selected);
    return;
  }
  workspace.departmentId = null;
  sessionStorage.removeItem("team-sync-active-department");
  $("workspace-tools").open = false;
  $("department-detail-card").replaceChildren();
  const content = project.departments.map(renderDepartmentSummary);
  if (!content.length) content.push(el("p", "workspace-empty", "还没有部门。新建部门后即可填写主线与负责人。"));
  $("department-cards").replaceChildren(...content);
}

function departmentPresence(department) {
  const presence = department.paused ? "已暂停派发" : (workspaceAgentStates[department.status] || "待确认");
  const review = Number.isInteger(department.pending_review) && department.pending_review > 0
    ? department.pending_review + " 项待验收" : department.pending_review === true ? "待验收" : "";
  return [presence, review].filter(Boolean).join(" · ");
}

function renderDepartmentSummary(department) {
  const control = workspaceButton("", () => openWorkspaceDepartment(department.id), "department-card department-summary secondary");
  control.dataset.department = department.id;
  control.dataset.departmentChoice = department.id;
  const lead = department.employees.find(employee => employee.lead);
  control.append(el("span", "department-summary-title", department.name),
    el("span", "workspace-muted", lead ? "负责人 · " + lead.name : "尚未登记负责人"),
    el("span", "workspace-muted", departmentPresence(department)));
  return control;
}

function openWorkspaceDepartment(departmentId) {
  workspace.departmentId = departmentId;
  workspaceMessage("");
  renderWorkspaceProject(workspace.project);
  $("workspace-department-name").focus();
}

function returnToWorkspaceDepartments() {
  const previous = workspace.departmentId;
  workspace.departmentId = null;
  workspaceMessage("");
  renderWorkspaceProject(workspace.project);
  const card = [...$("department-cards").children].find(button => button.dataset.departmentChoice === previous);
  (card || $("workspace-project-name")).focus();
}

function renderSelectedWorkspaceDepartment(project, department) {
  const cards = $("department-detail-card");
  const wasOpen = cards.querySelector(".department-details")?.open || false;
  const openEmployees = new Set([...cards.querySelectorAll(".employee-details[open]")]
    .map(details => details.closest(".employee-card").dataset.employee));
  const active = document.activeElement;
  const focusTarget = cards.contains(active) ? active.dataset.workspaceAction : null;
  cards.replaceChildren(renderDepartment(project, department, wasOpen, openEmployees));
  if (focusTarget) {
    const match = [...cards.querySelectorAll("[data-workspace-action]")].find(item => item.dataset.workspaceAction === focusTarget);
    if (match && !match.disabled) match.focus();
  }
}

function renderDepartment(project, department, wasOpen, openEmployees) {
  const card = el("article", "department-card");
  card.dataset.department = department.id;
  const mainline = el("section", "mainline-card");
  const mainlineHeader = el("div", "mainline-heading");
  const adjust = workspaceButton("调整主线", () => editWorkspaceChange(project, department));
  adjust.dataset.workspaceAction = department.id + ":mainline";
  adjust.disabled = department.can_adjust === false;
  if (adjust.disabled) adjust.title = "负责人尚未绑定可投递的岗位";
  const version = department.mainline?.version;
  const effectiveVersion = department.mainline?.effective_version ?? version;
  mainlineHeader.append(el("h4", "", "当前主线"), el("span", "work-version", effectiveVersion ? "v" + effectiveVersion : "初始主线"), adjust);
  const instruction = department.mainline?.effective_instruction ?? department.mainline?.instruction;
  mainline.append(mainlineHeader, el("p", "workspace-line-content", instruction || "尚未填写主线"));
  if (version > effectiveVersion) {
    const saved = department.changes.find(item => item.scope === "mainline" && item.version === version);
    const received = saved?.received_by?.length > 0;
    mainline.append(el("p", "workspace-muted", "已保存 v" + version + (received ? " · 已接收，待生效" : " · 待接收和生效")));
  }

  const employees = el("div", "employee-cards");
  const ordered = [...department.employees].sort((a, b) => Number(b.lead) - Number(a.lead));
  for (const employee of ordered) employees.append(renderEmployee(project, department, employee, openEmployees.has(employee.id)));
  if (!ordered.length) employees.append(el("p", "workspace-muted", "尚未登记员工"));

  const actions = el("div", "department-actions");
  const pause = workspaceButton(department.paused ? "恢复派发" : "暂停后续派发", () => editWorkspacePause(project, department));
  pause.dataset.workspaceAction = department.id + ":pause";
  actions.append(el("p", "workspace-muted", departmentPresence(department)), pause);
  card.append(mainline, employees, actions, renderDepartmentDetails(project, department, wasOpen));
  return card;
}

function currentChange(department, receipt) {
  if (!receipt) return null;
  return department.changes.find(change => change.deliveries.some(delivery =>
    delivery.agent === receipt.agent && delivery.bound_task === receipt.bound_task && delivery.stage === receipt.stage &&
    delivery.event_id === receipt.event_id && delivery.run_id === receipt.run_id));
}

function appendFact(facts, label, value) {
  const row = el("div");
  row.append(el("dt", "", label), el("dd", "", value));
  facts.append(row);
}

function receiptFact(label, text, confirmed, instant) {
  const column = el("div");
  column.append(el("span", "workspace-muted", label), el("strong", confirmed ? "has-receipt" : "", text));
  const recorded = typeof instant === "string" && Number.isFinite(Date.parse(instant));
  if (recorded) {
    const time = el("time", "", new Date(instant).toLocaleTimeString("zh-CN", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }));
    time.dateTime = instant;
    time.title = new Date(instant).toLocaleString("zh-CN");
    column.append(time);
  }
  return column;
}

function renderEmployee(project, department, employee, detailOpen) {
  const card = el("article", "employee-card");
  card.dataset.employee = employee.id;
  const current = employee.last_delivery;
  const history = employee.history;
  const heading = el("div", "employee-heading");
  const name = el("h4", "", employee.name);
  if (employee.lead) name.append(el("span", "employee-role", "负责人"));
  heading.append(name);
  const task = el("p", "work-title", employee.task?.title || "暂无当前工作");
  if (employee.task) task.append(el("span", "employee-role work-version", "v" + employee.task.version));
  const facts = el("dl", "employee-facts");
  const workState = current ? workspaceDeliveryStates[current.state]
    : history ? loopStates[history.state] : employee.manual ? states[employee.manual.status] : employee.task ? "已登记" : "待分配";
  appendFact(facts, "工作状态", workState || "待确认");
  appendFact(facts, "模型", employee.model_source && employee.model ? employee.model : "待核实");
  appendFact(facts, "思考级别", employee.model_source && employee.effort ? employee.effort : "待核实");
  const receipts = el("div", "work-receipts");
  const claimed = current?.claimed_at || history?.claimed_at;
  const applied = current?.applied_at;
  const hasResult = current ? current.state === "reported" && Boolean(current.result)
    : Boolean(history?.finished_at && history.result);
  const hasBlocker = current?.state === "blocked";
  receipts.append(
    receiptFact("保存", employee.task ? "已保存" : "待登记", Boolean(employee.task)),
    receiptFact("接收", claimed ? "已接收" : "待确认", Boolean(claimed), claimed),
    receiptFact("生效", applied ? "已生效" : "待确认", Boolean(applied), applied),
    receiptFact("结果", hasBlocker ? "有卡点" : hasResult ? "有结果" : "待结果", hasResult,
      hasResult || hasBlocker ? current?.finished_at || history?.finished_at : null),
  );
  card.append(heading, task, facts, receipts);

  if (employee.task) {
    const correct = workspaceButton("纠正此 Agent", () => editWorkspaceChange(project, department, employee));
    correct.dataset.workspaceAction = department.id + ":" + employee.id + ":correct";
    correct.disabled = !employee.can_correct || employee.channel !== "loop";
    if (correct.disabled) correct.title = employee.channel === "observe" ? "观察员工暂未接入派发" : "当前工作暂不能接收纠正";
    card.append(correct);
  }
  card.append(renderEmployeeDetails(project, department, employee, detailOpen));
  return card;
}

function renderEmployeeDetails(project, department, employee, open) {
  const details = el("details", "employee-details");
  details.open = open;
  details.append(el("summary", "", "工作详情"));
  const facts = el("dl", "employee-facts");
  const current = employee.last_delivery;
  const historical = employee.history;
  appendFact(facts, "归属", project.name + " / " + department.name);
  appendFact(facts, "在岗", employee.channel === "observe" ? "仅观察"
    : employee.status === "unknown" ? "在线情况未知" : workspaceAgentStates[employee.status] || "在线情况未知");
  if (employee.model_source) appendFact(facts, "模型来源", employee.model_source);
  if (current) {
    const change = currentChange(department, current);
    if (change) appendFact(facts, "指令版本", (change.scope === "person" ? "个人纠正" : "部门主线") + " v" + change.version);
    if (current.event_id) appendFact(facts, "事件", "#" + current.event_id);
    if (current.run_id) appendFact(facts, "轮次", "#" + current.run_id);
    if (current.note) appendFact(facts, "说明", current.note);
  }
  if (historical) {
    appendFact(facts, "历史来源", historical.source || "管理员登记的历史任务关联");
    appendFact(facts, "历史轮次", "#" + historical.run_id + " · " + (loopStates[historical.state] || "待核验"));
    if (historical.finished_at) appendFact(facts, "结束时间", new Date(historical.finished_at).toLocaleString("zh-CN"));
    if (historical.result) appendFact(facts, "历史结果", historical.result);
  }
  if (employee.manual) {
    appendFact(facts, "自报时间", date(employee.manual.updated));
    if (employee.manual.detail) appendFact(facts, "自报说明", employee.manual.detail);
  }
  details.append(facts);
  return details;
}

function renderDepartmentDetails(project, department, open) {
  const details = el("details", "department-details");
  details.dataset.department = department.id;
  details.open = open;
  details.append(el("summary", "", "部门详情"), workspaceButton("添加观察员工", () => editWorkspaceEmployee(project, department)));
  const history = el("ol");
  const names = new Map(department.employees.map(employee => [employee.id, employee.name]));
  for (const change of department.changes) {
    const line = el("li");
    const title = change.scope === "person" ? "个人纠正 · " + (names.get(change.agent) || change.agent) : "主线调整";
    line.append(el("strong", "", title + " v" + change.version), el("p", "workspace-line-content", change.instruction));
    if (change.acceptance) line.append(el("p", "workspace-line-content", "验收 · " + change.acceptance));
    const recipients = change.received_by?.length || 0;
    const applied = change.applied_by?.length || 0;
    line.append(el("p", "workspace-muted", ["已保存", recipients ? recipients + " 人已接收" : "", applied ? applied + " 人已生效" : ""].filter(Boolean).join(" · ")));
    const latestChange = department.changes.find(entry => entry.scope === change.scope &&
      entry.agent === change.agent && entry.task_id === change.task_id);
    const deliveries = el("ul");
    for (const delivery of change.deliveries) {
      const item = el("li", "", [names.get(delivery.agent) || delivery.agent,
        workspaceDeliveryStates[delivery.state] || "待确认", delivery.run_id ? "第 " + delivery.run_id + " 轮" : ""].filter(Boolean).join(" · "));
      for (const [label, value] of [["回执", delivery.result], ["证据", delivery.evidence], ["备注", delivery.note]]) {
        if (typeof value === "string" && value) item.append(el("p", "workspace-line-content", label + " · " + value));
      }
      const latestDelivery = change.deliveries.findLast(entry => entry.agent === delivery.agent && entry.stage !== "scope");
      if (latestChange === change && latestDelivery === delivery && ["applied", "blocked"].includes(delivery.state) && Number.isInteger(delivery.run_id)) {
        item.append(workspaceButton("续收同版结果", () => editWorkspaceContinuation(change, delivery)));
      }
      if (delivery.evidence_available && delivery.evidence) item.append(workspaceButton("查看现场证据", () => openWorkspaceAttachment(change.id, delivery.evidence)));
      deliveries.append(item);
    }
    if (change.deliveries.length) line.append(deliveries);
    for (const review of change.reviews || []) {
      const reviewNote = el("section", "workspace-review");
      reviewNote.dataset.review = review.id;
      reviewNote.append(el("strong", "", "独立审核 · " + review.reviewer), el("p", "workspace-line-content", review.conclusion));
      const facts = el("dl", "employee-facts");
      if (review.source) appendFact(facts, "来源", review.source);
      if (review.candidate) appendFact(facts, "候选", review.candidate);
      if (review.commit) appendFact(facts, "提交", review.commit);
      if (review.evidence) appendFact(facts, "证据引用", review.evidence);
      reviewNote.append(facts);
      if (typeof review.pr === "string" && /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/[1-9]\d*$/.test(review.pr)) {
        const link = el("a", "", "打开 GitHub PR");
        link.href = review.pr;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        reviewNote.append(link);
      }
      if (review.evidence) reviewNote.append(workspaceButton("查看审核证据", () => openWorkspaceAttachment(change.id, review.evidence)));
      line.append(reviewNote);
    }
    history.append(line);
  }
  details.append(department.changes.length ? history : el("p", "workspace-muted", "暂无指令变更"));
  return details;
}

function closeWorkspaceDialog() {
  workspace.dialog = null;
  if ($("workspace-dialog").open) $("workspace-dialog").close();
}

function showWorkspaceDialog(title, options = {}) {
  const current = {options, revision: workspace.revision, sending: false, signature: null, requestId: null};
  workspace.dialog = current;
  $("workspace-dialog-form").dataset.workspaceForm = options.kind || "";
  $("workspace-dialog-title").textContent = title;
  $("workspace-dialog-context").textContent = options.context || "";
  $("workspace-dialog-context").hidden = !options.context;
  $("workspace-dialog-error").textContent = "";
  $("workspace-dialog-content").replaceChildren();
  $("workspace-dialog-submit").textContent = options.submitText || "提交";
  $("workspace-dialog-submit").hidden = !options.send;
  $("workspace-dialog-submit").disabled = false;
  $("workspace-dialog-cancel").disabled = false;
  for (const field of options.fields || []) {
    const label = el("label", "", field.label);
    const input = el(field.multiline ? "textarea" : "input");
    input.name = field.name;
    input.required = true;
    if (field.file) {
      input.type = "file";
      input.accept = "application/json,.json";
    } else {
      input.maxLength = field.maxLength || 950;
      input.value = field.value || "";
      if (field.pattern) input.pattern = field.pattern;
    }
    label.append(input);
    $("workspace-dialog-content").append(label);
  }
  $("workspace-dialog").showModal();
  return current;
}

async function selectWorkspaceProject() {
  const current = showWorkspaceDialog("打开项目");
  const area = $("workspace-dialog-content");
  area.append(el("p", "", "正在读取项目…"));
  try {
    const data = await request("/api/aams/projects");
    if (workspace.dialog !== current) return;
    if (!data.projects.length) {
      area.replaceChildren(el("p", "", "还没有本地项目。请返回新建项目。"));
      return;
    }
    const list = el("ul", "workspace-project-list");
    for (const project of data.projects) {
      const row = el("li");
      const button = workspaceButton(project.name, () => {
        closeWorkspaceDialog();
        void openWorkspaceProject(project.id);
      });
      button.dataset.projectChoice = project.id;
      row.append(button);
      list.append(row);
    }
    area.replaceChildren(list);
    list.querySelector("button").focus();
  } catch (error) {
    if (!obsolete(error) && workspace.dialog === current) {
      area.replaceChildren(el("p", "", "读取本地项目失败。请返回后重试。"));
    }
  }
}

function workspaceRecordId(prefix) {
  return prefix + "-" + id().slice(0, 12);
}

function createWorkspaceProject() {
  const projectId = workspaceRecordId("project");
  showWorkspaceDialog("新建项目", {
    fields: [{name: "name", label: "项目名称", maxLength: 80}], submitText: "创建项目",
    send: values => request("/api/aams/projects", {id: projectId, name: values.name}),
    done: data => { void openWorkspaceProject(data.id); },
  });
}

function editWorkspaceDepartment() {
  const departmentId = workspaceRecordId("department");
  const lead = workspaceRecordId("lead");
  showWorkspaceDialog("新建部门", {
    fields: [
      {name: "name", label: "部门名称", maxLength: 80},
      {name: "lead", label: "负责人标识", value: lead, pattern: "[a-zA-Z0-9_\\-]+", maxLength: 48},
      {name: "mainline", label: "当前主线", multiline: true},
    ],
    submitText: "创建部门",
    send: values => request("/api/aams/departments", {project: workspace.projectId, id: departmentId, ...values}),
    done: () => { void refreshWorkspaceProject(true); },
  });
}

function editWorkspaceEmployee(project, department) {
  const employeeId = workspaceRecordId("employee");
  const taskId = workspaceRecordId("task");
  showWorkspaceDialog("添加观察员工", {
    context: project.name + " / " + department.name,
    fields: [{name: "name", label: "员工姓名", maxLength: 80}, {name: "task_title", label: "当前工作", maxLength: 120}],
    submitText: "添加员工",
    send: values => request("/api/aams/employees", {project: project.id, department: department.id, id: employeeId, task_id: taskId, ...values}),
    done: () => { void refreshWorkspaceProject(true); },
  });
}

function editWorkspaceChange(project, department, employee = null) {
  const expectedVersion = employee ? employee.task.version : department.mainline.version;
  if (!Number.isInteger(expectedVersion) || expectedVersion < 0) return;
  showWorkspaceDialog(employee ? "纠正此 Agent" : "调整主线", {
    kind: employee ? "person" : "mainline",
    context: [project.name, department.name, employee?.name, employee?.task.title, "当前 v" + expectedVersion].filter(Boolean).join(" / "),
    fields: [{name: "instruction", label: employee ? "纠正指令" : "新主线", multiline: true},
      {name: "acceptance", label: "验收条件", multiline: true}],
    submitText: "提交指令",
    send: (values, requestId) => request("/api/aams/changes", {
      request_id: requestId, project: project.id, department: department.id,
      scope: employee ? "person" : "mainline", agent: employee?.id || null,
      task_id: employee?.task.id || null, expected_version: expectedVersion, ...values,
    }),
    done: data => {
      workspaceMessage("已登记 v" + data.version + "，接收与生效以回执为准。");
      void refreshWorkspaceProject(true);
    },
  });
}

function editWorkspacePause(project, department) {
  const paused = !department.paused;
  showWorkspaceDialog(paused ? "暂停后续派发" : "恢复派发", {
    context: department.name,
    submitText: paused ? "确认暂停" : "确认恢复",
    send: () => request("/api/aams/departments/pause", {project: project.id, department: department.id, paused}),
    done: () => { void refreshWorkspaceProject(true); },
  });
}

function editWorkspaceContinuation(change, delivery) {
  showWorkspaceDialog("续收同版结果", {
    context: "核对第 " + delivery.run_id + " 轮已生成的结果。",
    submitText: "确认续收",
    send: () => request("/api/aams/changes/continue", {id: change.id, agent: delivery.agent, after_run: delivery.run_id}),
    done: () => { void refreshWorkspaceProject(true); },
  });
}

async function openWorkspaceAttachment(changeId, reference) {
  const current = showWorkspaceDialog("现场证据", {context: reference});
  const content = $("workspace-dialog-content");
  content.append(el("p", "", "正在读取证据…"));
  try {
    const data = await request("/api/aams/attachment?" + new URLSearchParams({change_id: changeId, ref: reference}));
    if (workspace.dialog === current) content.replaceChildren(el("pre", "workspace-attachment", JSON.stringify(data, null, 2)));
  } catch (error) {
    if (!obsolete(error) && workspace.dialog === current) content.replaceChildren(el("p", "", "证据暂不可读。请核对本机记录。"));
  }
}

function editWorkspaceRoster() {
  const projectId = workspace.projectId;
  showWorkspaceDialog("导入岗位登记", {
    context: "按登记文件中的项目与部门绑定岗位。",
    fields: [{name: "manifest", label: "岗位登记 JSON 文件", file: true}],
    submitText: "导入登记",
    send: async values => {
      if (values.manifest.size > 256 * 1024) throw new Error("岗位登记文件超过 256 KB。");
      let manifest;
      try {
        manifest = JSON.parse(await values.manifest.text());
      } catch {
        throw new Error("岗位登记文件须为有效的 JSON。");
      }
      if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) throw new Error("岗位登记须为 JSON 对象。");
      if (manifest.project?.id !== projectId) throw new Error("登记文件中的项目与当前打开的项目不一致。");
      return request("/api/aams/roster/import", {manifest});
    },
    done: () => {
      workspaceMessage("已导入岗位登记。");
      void refreshWorkspaceProject(true);
    },
  });
}

$("workspace-create").addEventListener("click", createWorkspaceProject);
$("workspace-open").addEventListener("click", () => { void selectWorkspaceProject(); });
$("workspace-back").addEventListener("click", () => returnToWorkspaceHome());
$("workspace-back-departments").addEventListener("click", returnToWorkspaceDepartments);
$("workspace-add-department").addEventListener("click", editWorkspaceDepartment);
$("workspace-import-roster").addEventListener("click", editWorkspaceRoster);
$("workspace-dialog-cancel").addEventListener("click", closeWorkspaceDialog);
$("workspace-dialog").addEventListener("cancel", event => {
  event.preventDefault();
  if (!workspace.dialog?.sending) closeWorkspaceDialog();
});
$("workspace-dialog-form").addEventListener("submit", async event => {
  event.preventDefault();
  const current = workspace.dialog;
  if (!current?.options.send || current.sending) return;
  const form = event.currentTarget;
  const values = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, typeof value === "string" ? value.trim() : value]));
  const message = $("workspace-dialog-error");
  if (Object.values(values).some(value => typeof value === "string" ? !value : !value.size)) {
    message.textContent = "请填写所有字段。";
    return;
  }
  const signature = JSON.stringify(values);
  if (signature !== current.signature) {
    current.signature = signature;
    current.requestId = id();
  }
  current.sending = true;
  const inputs = [...form.elements];
  inputs.forEach(input => { input.disabled = true; });
  message.textContent = "";
  try {
    const data = await current.options.send(values, current.requestId);
    if (workspace.dialog !== current || workspace.revision !== current.revision) return;
    closeWorkspaceDialog();
    current.options.done(data);
  } catch (error) {
    if (!obsolete(error) && workspace.dialog === current) {
      message.textContent = error.status === 409
        ? (error.message + " 请核对最新状态。") : (error.message || "提交未确认，请核对后重试。");
      if (error.status === 409) void refreshWorkspaceProject(true);
    }
  } finally {
    inputs.forEach(input => { input.disabled = false; });
    current.sending = false;
  }
});
setInterval(() => { if (!document.hidden) void refreshWorkspaceProject(); }, 4000);
void connectOnStartup();
