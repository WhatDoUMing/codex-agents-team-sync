"use strict";
// Real Chromium UI tests against an isolated local test server. Never uses the user's browser profile.
const fs=require("node:fs"), path=require("node:path"), {spawn}=require("node:child_process");
const {once}=require("node:events");
const assert=require("node:assert/strict");
const url=process.env.TEAM_SYNC_QA_URL, token=process.env.TEAM_SYNC_QA_TOKEN, temp=process.env.TEAM_SYNC_QA_TEMP;
if(!url||!token||!temp)throw new Error("Run via python -B run_browser_qa.py");
const chrome=process.env.TEAM_SYNC_CHROME || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const output=process.env.TEAM_SYNC_QA_OUTPUT || path.join(__dirname,"verification");
fs.mkdirSync(output,{recursive:true});
const profile=fs.mkdtempSync(path.join(temp,"chrome-profile-"));
const child=spawn(chrome,["--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check",
 "--disable-background-networking","--disable-component-update","--disable-sync","--metrics-recording-only",
 "--remote-debugging-address=127.0.0.1","--remote-debugging-port=0","--user-data-dir="+profile,"about:blank"],
 {windowsHide:true,stdio:"ignore"});
let ws, seq=0;
const pending=new Map(), exceptions=[], passes=[];
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,label,timeout=15000){
 const end=Date.now()+timeout;
 while(Date.now()<end){if(await fn())return;await delay(100);}
 throw new Error("Timeout: "+label);
}
function cdp(method,params={}){
 return new Promise((resolve,reject)=>{
  const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(new Error("CDP timeout: "+method));},15000);
  pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));
 });
}
async function evaluate(expression){
 const result=await cdp("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});
 if(result.exceptionDetails)throw new Error(result.exceptionDetails.text+" "+JSON.stringify(result.exceptionDetails.exception));
 return result.result.value;
}
const wait=async(expression,label)=>until(()=>evaluate(expression),label);
function pass(label){passes.push(label);process.stdout.write("PASS "+label+"\n");}
async function login(key){
 await evaluate("document.getElementById('access-key').value="+JSON.stringify(key)+";document.getElementById('login-form').requestSubmit()");
}
async function screenshot(name,width,height){
 await cdp("Emulation.setDeviceMetricsOverride",{width,height,deviceScaleFactor:1,mobile:width<600});
 await evaluate("window.scrollTo(0,0)");
 await delay(150);
 const size=(await cdp("Page.getLayoutMetrics")).cssContentSize;
 const shot=await cdp("Page.captureScreenshot",{format:"png",captureBeyondViewport:true,clip:{x:0,y:0,width,height:Math.ceil(size.height),scale:1}});
 fs.writeFileSync(path.join(output,name),Buffer.from(shot.data,"base64"));
}
async function workspaceScreenshot(name,width,height){
 await cdp("Emulation.setDeviceMetricsOverride",{width,height,deviceScaleFactor:1,mobile:width<600});
 await evaluate("window.scrollTo(0,0)");
 await delay(150);
 const shot=await cdp("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
 fs.writeFileSync(path.join(output,name),Buffer.from(shot.data,"base64"));
}
async function click(selector){
 await evaluate("document.querySelector("+JSON.stringify(selector)+").click()");
}
async function workspaceForm(values){
 await evaluate("Object.entries("+JSON.stringify(values)+").forEach(([name,value])=>{$('workspace-dialog-form').elements.namedItem(name).value=value;});$('workspace-dialog-form').requestSubmit()");
}
async function openDepartment(){
 await click("#workspace-open");
 await wait('!!document.querySelector(\'[data-project-choice="tp"]\')',"local project is listed");
 await click('[data-project-choice="tp"]');
 await wait('!!document.querySelector(\'[data-department-choice="combat"]\')',"departments are listed");
 await click('[data-department-choice="combat"]');
 await wait('!$("department-view").hidden && !!document.querySelector(\'[data-employee="player"]\')',"combat department opens");
}
async function checkWorkspace(){
 const homeButtons=await evaluate("[...$('project-home').querySelectorAll('button')].map(button=>button.textContent.trim())");
 assert.deepEqual(homeButtons,["新建项目","打开项目"]);
 assert.equal(await evaluate("$('project-view').hidden && $('department-view').hidden && $('logout').hidden"),true);
 assert.equal(await evaluate("document.documentElement.scrollHeight<=innerHeight+1"),true);
 await workspaceScreenshot("project-home-desktop.png",1280,900);
 await workspaceScreenshot("project-home-mobile.png",390,900);
 assert.equal(await evaluate("document.documentElement.scrollHeight<=innerHeight+1 && document.documentElement.scrollWidth<=innerWidth+1"),true);
 pass("project home has two Chinese actions and fits both single-screen viewports");

 await click("#workspace-create");
 await workspaceForm({name:"浏览器新建项目"});
 await wait("$('workspace-project-name').textContent==='浏览器新建项目' && !$('workspace-add-department').hidden","create a project");
 await click("#workspace-add-department");
 await workspaceForm({name:"临时部门",mainline:"临时主线"});
 await wait("$('department-cards').textContent.includes('临时部门')","create a department");
 await click("#department-cards button");
 await wait("$('department-view').textContent.includes('临时主线')","open a new department");
 assert.equal(await evaluate("document.querySelector('.mainline-card button').disabled"),true);
 assert.equal(await evaluate("document.querySelector('.employee-card button').disabled"),true);
  await click(".department-details summary");
  await evaluate("[...document.querySelectorAll('.department-details button')].find(button=>button.textContent==='添加观察员工').click()");
  await workspaceForm({name:"隔离观察员工",task_title:"核对临时工作"});
  await wait("$('department-view').textContent.includes('核对临时工作')","register an observation-only employee");
  await click("#workspace-tools summary");
  await click("#workspace-import-roster");
  await evaluate("window.__setRoster=manifest=>{const source=new DataTransfer();source.items.add(new File([JSON.stringify(manifest)],'local-roster.json',{type:'application/json'}));$('workspace-dialog-form').elements.manifest.files=source.files;$('workspace-dialog-form').requestSubmit();}; window.__setRoster({project:{id:'wrong-project'},departments:[]});");
  await wait("$('workspace-dialog-error').textContent.includes('与当前打开的项目不一致')","roster import refuses a different project");
  await evaluate("const p=workspace.project,d=p.departments[0],lead=d.employees.find(emp=>emp.lead);window.__setRoster({project:{id:p.id,name:p.name},departments:[{id:d.id,name:d.name,lead:lead.id,mainline:d.mainline.instruction,employees:[{id:'qa-imported',name:'导入观察岗',task_id:'qa-imported-task',task_title:'核对导入结果'}]}]});");
  await wait("$('department-view').textContent.includes('导入观察岗')","roster import updates the opened project");
 await click("#workspace-back-departments");
 await click("#workspace-back");
  pass("create project, department, and observation employee; import binds only the opened project");

 await click("#workspace-open");
 await wait('!!document.querySelector(\'[data-project-choice="tp"]\')',"list and reopen the existing test project");
 await click('[data-project-choice="tp"]');
 await wait("document.querySelectorAll('#department-cards button').length===2","project shows registered departments");
 assert.equal(await evaluate("$('department-view').hidden && $('department-detail-card').children.length===0"),true);
 await workspaceScreenshot("project-departments-desktop.png",1280,900);
 assert.equal(await evaluate("[...$('department-cards').children].every(card=>card.getBoundingClientRect().top===$('department-cards').firstChild.getBoundingClientRect().top)"),true);
 await click('[data-department-choice="combat"]');
 await wait('!!document.querySelector(\'[data-employee="player"]\')',"department detail contains work cards");
 assert.equal(await evaluate("document.querySelector('.employee-card').dataset.employee"),"boss");
 assert.equal(await evaluate("$('project-view').hidden && !$('workspace-tools').open && !document.querySelector('.department-details').open"),true);
 assert.equal(await evaluate("document.querySelector('[data-employee=reviewer]').textContent.includes('已完成')"),true);
 await wait("document.querySelector('[data-employee=reviewer] .employee-details').textContent.includes('离线')","offline & historical work states remain independent");
 assert.equal(await evaluate("document.querySelector('[data-employee=reviewer] .employee-details').textContent.includes('历史轮次#10')"),true);
 assert.equal(await evaluate("document.querySelector('[data-employee=observer] button').disabled"),true);
 pass("two-level navigation orders the lead first and distinguishes inactive presence from completed historical work");

 await evaluate("window.__workspacePosts=[]; window.__workspaceFetch=window.fetch; window.fetch=(...args)=>{if(String(args[0])==='/api/aams/changes'&&args[1]?.method==='POST')window.__workspacePosts.push(JSON.parse(args[1].body));return window.__workspaceFetch(...args);};");
 await click('[data-workspace-action="combat:mainline"]');
 assert.equal(await evaluate("$('workspace-dialog-form').dataset.workspaceForm"),"mainline");
 await workspaceForm({instruction:"核对浏览器主线流程",acceptance:"保留同版结果"});
 await wait("$('workspace-feedback').textContent.includes('已登记 v1')","mainline submission reaches the real server");
 const mainline=await evaluate("window.__workspacePosts[0]");
 assert.deepEqual([mainline.project,mainline.department,mainline.scope,mainline.agent,mainline.task_id,mainline.expected_version],["tp","combat","mainline",null,null,0]);
 await wait("workspace.project.departments[0].mainline.effective_version===1","manager receipt and worker effect reconcile");
 assert.equal(await evaluate("document.querySelector('.mainline-card').textContent.includes('核对浏览器主线流程')"),true);
 pass("mainline starts at the department scope and displays the effective revision after receipts");

 const player='[data-employee="player"]';
 const correct=player+" button";
 assert.equal(await evaluate("document.querySelector("+JSON.stringify(correct)+").textContent"),"纠正此 Agent");
 await click(correct);
 assert.equal(await evaluate("$('workspace-dialog-form').dataset.workspaceForm"),"person");
 assert.equal(await evaluate("$('workspace-dialog-form').elements.instruction.value"),"");
 await workspaceForm({instruction:"浏览器 QA 当前任务纠正",acceptance:"只验收当前任务"});
 await wait("document.querySelector('[data-employee=player]').textContent.includes('qa-first-model')","personal change has its own first bound receipt");
 const personal=await evaluate("window.__workspacePosts[1]");
 assert.deepEqual([personal.project,personal.department,personal.scope,personal.agent,personal.task_id,personal.expected_version],["tp","combat","person","player","fish",0]);
 assert.equal(await evaluate("workspace.project.departments[0].employees.find(e=>e.id==='reviewer').task.version"),0);
 assert.equal(await evaluate("workspace.project.departments[0].mainline.version"),1);
 assert.equal(await evaluate("document.querySelector('[data-employee=player] .work-receipts').textContent.includes('有结果')"),false);
 await click(".department-details summary");
 await evaluate("[...document.querySelectorAll('.department-details button')].find(b=>b.textContent==='续收同版结果').click()");
 await click("#workspace-dialog-submit");
 await wait("document.querySelector('[data-employee=player]').textContent.includes('qa-result-model')&&document.querySelector('[data-employee=player] .work-receipts').textContent.includes('有结果')","card follows the later result, not the first applied receipt");
 assert.equal(await evaluate("document.querySelector('[data-employee=player]').textContent.includes('qa-first-model')"),false);
 assert.equal(await evaluate("document.querySelector('[data-employee=player]').textContent.includes('xhigh')"),true);
 await evaluate("[...document.querySelectorAll('.department-details button')].find(b=>b.textContent==='查看现场证据').click()");
 await wait("$('workspace-dialog-content').textContent.includes('isolated browser fixture')","bound attachment opens with the authenticated browser");
 await click("#workspace-dialog-cancel");
  const outcome=await evaluate("(async()=>{const c=window.__workspacePosts[1];const response=await fetch('/api/aams/reviews',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+sessionStorage.getItem('team-sync-session')},body:JSON.stringify({change_id:c.request_id,reviewer:'independent',review_run:null,conclusion:'浏览器隔离审核结论',candidate:'abc1234',commit:'def5678',pr:'https://github.com/example/aams/pull/10',evidence:'field/browser-'+c.request_id+'.json'})});return {status:response.status,data:await response.json()};})()");
  assert.equal(outcome.status,200,JSON.stringify(outcome.data));
  await wait("document.querySelector('.workspace-review')?.textContent.includes('浏览器隔离审核结论')","review result becomes visible under details");
  const review=await evaluate("(()=>{const r=document.querySelector('.workspace-review');const a=r.querySelector('a');return {text:r.textContent,href:a?.href,rel:a?.rel};})()");
  assert(review.text.includes("independent") && review.text.includes("本地独立审核员自报") &&
    review.text.includes("abc1234") && review.text.includes("def5678"));
  assert.equal(review.href,"https://github.com/example/aams/pull/10");
  assert.equal(review.rel,"noopener noreferrer");
  await click(".workspace-review button");
  await wait("$('workspace-dialog-content').textContent.includes('isolated browser fixture')","authenticated review attachment opens");
  await click("#workspace-dialog-cancel");
  await screenshot("department-review-desktop.png",1280,900);
 await click(".department-details summary");
 await evaluate("window.fetch=window.__workspaceFetch;");
 pass("personal correction is scoped to the current agent and task and follows bound result evidence");
  pass("independent review displays source, conclusion, candidate, commit, PR link, and authenticated attachment");

 await click('[data-workspace-action="combat:pause"]');
 await click("#workspace-dialog-submit");
 await wait("document.querySelector('[data-workspace-action=\"combat:pause\"]').textContent==='恢复派发'","pause readback");
 await click('[data-workspace-action="combat:pause"]');
 await click("#workspace-dialog-submit");
 await wait("document.querySelector('[data-workspace-action=\"combat:pause\"]').textContent==='暂停后续派发'","resume readback");
 await workspaceScreenshot("department-work-desktop.png",1280,900);
 await workspaceScreenshot("department-work-mobile.png",390,900);
 assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth+1"),true);
 pass("card pause and resume confirm readback and the department fits mobile width");

  await click(correct);
  await workspaceForm({instruction:"浏览器 QA 记录卡点",acceptance:"保存真实卡点回执"});
  await wait("document.querySelector('[data-employee=player] .work-receipts').textContent.includes('有卡点')","terminal blocker appears on the current card");
  assert.equal(await evaluate("document.querySelector('[data-employee=player] .work-receipts').textContent.includes('有结果')"),false);
  assert.equal(await evaluate("workspaceDeliveryStates.reconciling"),"旧版对账中");
  await click(".department-details summary");
  await wait("document.querySelector('.department-details').textContent.includes('跳转入口不可用（隔离样例）')","the blocker receipt stays readable in the department details");
  await screenshot("department-blocker-desktop.png",1280,900);
  pass("a terminal blocker retains evidence without claiming a result; older revisions are labeled as reconciliation");
}
(async()=>{
 try{
  const active=path.join(profile,"DevToolsActivePort");
  await until(()=>fs.existsSync(active),"headless Chromium startup");
  const port=fs.readFileSync(active,"utf8").split(/\r?\n/)[0];
  const tabs=await (await fetch("http://127.0.0.1:"+port+"/json/list")).json();
  const tab=tabs.find(t=>t.type==="page");
  assert(tab,"headless page target exists");
  ws=new WebSocket(tab.webSocketDebuggerUrl);
  await once(ws,"open");
  ws.addEventListener("message",event=>{
   const m=JSON.parse(event.data);
   if(m.id&&pending.has(m.id)){
    const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);
   }else if(m.method==="Runtime.exceptionThrown"){exceptions.push(m.params.exceptionDetails);}
  });
  await cdp("Runtime.enable");await cdp("Page.enable");
  await cdp("Emulation.setDeviceMetricsOverride",{width:1280,height:1100,deviceScaleFactor:1,mobile:false});
  await cdp("Page.navigate",{url});
  await wait("document.readyState==='complete'&&!!document.getElementById('login-form')","login page");
  assert.equal(await evaluate("document.getElementById('dashboard').hidden"),true);
  pass("unauthenticated page keeps dashboard hidden");
  const ticket=process.env.TEAM_SYNC_QA_LAUNCH;
  assert(ticket,"test launch ticket exists");
  await cdp("Page.navigate",{url:"about:blank"});
  await wait("location.href==='about:blank'","fresh tab origin");
  await cdp("Page.navigate",{url:url+"#launch="+ticket});
  await wait("!document.getElementById('dashboard').hidden&&latest!==null","one-time local launch connects without typing");
  assert.equal(await evaluate("location.hash"),"");
  await cdp("Page.reload");
  await wait("!document.getElementById('dashboard').hidden&&latest!==null","same tab stays connected across reload");
  pass("automatic launch unlocks dashboard, clears fragment and survives reload");
  await checkWorkspace();
  await evaluate("document.getElementById('logout').click()");
  await login("incorrect-session-key");
  await wait("document.getElementById('notice').textContent.includes('解锁失败')","invalid key feedback");
  assert.equal(await evaluate("document.getElementById('dashboard').hidden"),true);
  pass("wrong key is rejected in the real page");
  await login(token);
  await wait("!document.getElementById('dashboard').hidden&&latest!==null","valid key unlock");
  await openDepartment();
  await click("#workspace-tools summary");
  pass("authenticated worker and source cards render");
  await wait("!$('aams-order').hidden&&!$('order-submit').disabled","local AAMS order form");
  await evaluate("$('order-instruction').value='QA normal route';$('order-acceptance').value='QA actual evidence';$('order-form').requestSubmit()");
  await wait("displayedVersion===1&&$('order-history').textContent.includes('QA normal route')","version 1 entered from browser");
  await evaluate("$('order-instruction').value='QA both routes';$('order-acceptance').value='QA two records';$('order-form').requestSubmit()");
  await wait("displayedVersion===2&&$('order-history').children.length===2","version 2 retains historical instructions");
  assert.equal(await evaluate("$('order-status').textContent.includes('等待执行者确认')"),true);
  pass("versioned AAMS orders preserve history and await a genuine receiver");
  assert.equal(await evaluate("latest.workers.find(w=>w.id==='expired-worker').status"),"stale");
  assert.equal(await evaluate("[...document.getElementById('target').options].some(o=>o.value.includes('manual-observer')||o.value.includes('expired-worker'))"),false);
  pass("manual and expired workers cannot be instruction targets");
  await evaluate("document.getElementById('project').value='native-fixture';document.getElementById('project').dispatchEvent(new Event('change'));true");
  await wait("latest&&latest.sources.length===1&&latest.sources[0].adapter==='codex-app-server'","explicit native fixture project");
  assert.equal(await evaluate("document.getElementById('sources').textContent.includes('不表示桌面会话已停止或空闲')"),true);
  assert.equal(await evaluate("document.getElementById('send').disabled"),true);
  assert.equal(await evaluate("latest.workers.length"),0);
  pass("native metadata labels notLoaded without claiming a stopped or live agent");
  assert.equal(await evaluate("[...document.getElementById('target').options].some(o=>o.value.includes('fixture-native-session'))"),false);
  await screenshot("native-metadata-desktop.png",1280,1100);
  pass("native stored sessions are read-only and never command targets");

  await evaluate("window.__filterOriginalFetch=window.fetch;window.__filterHeld=false;window.fetch=async(...args)=>{const response=await window.__filterOriginalFetch(...args);if(String(args[0]).includes('project=history')){window.__filterHeld=true;await new Promise(resolve=>window.__releaseFilter=resolve);}return response;};document.getElementById('project').value='history';document.getElementById('project').dispatchEvent(new Event('change'));true");
  assert.equal(await evaluate("document.getElementById('send').disabled&&document.getElementById('target').options.length===0"),true);
  await wait("typeof window.__releaseFilter==='function'","history filter response held");
  await evaluate("window.__releaseFilter();window.fetch=window.__filterOriginalFetch;true");
  pass("project switch immediately clears old instruction targets");
  await wait("latest&&latest.commands.length===1&&latest.commands[0].id==='history-older'","server-side history project filter");
  pass("project filter retrieves older command beyond global latest 100");
  await evaluate("document.getElementById('project').value='';document.getElementById('project').dispatchEvent(new Event('change'))");
  await wait("latest&&latest.workers.length>=4","return to all projects");
  await evaluate("document.getElementById('start-demo').click()");
  await wait("latest&&latest.workers.some(w=>w.is_demo)","echo demo starts");
  await evaluate("document.getElementById('project').value='demo';document.getElementById('project').dispatchEvent(new Event('change'))");
  await wait("latest&&latest.workers.length===1&&latest.workers[0].is_demo&&document.getElementById('target').value!==''","demo project selected");
  const directive='<img src=x onerror="window.__xss=1"> browser QA: receive text only, do not execute';
  await evaluate("document.getElementById('instruction').value="+JSON.stringify(directive)+";document.getElementById('command-form').requestSubmit();document.getElementById('command-form').requestSubmit()");
  await wait("latest&&latest.commands.length===1&&latest.commands[0].state==='completed'","echo protocol complete");
  assert.equal(await evaluate("latest.commands[0].text"),directive);
  assert.equal(await evaluate("document.querySelectorAll('#commands img').length"),0);
  assert.equal(await evaluate("typeof window.__xss"),"undefined");
  assert.equal(await evaluate("latest.commands[0].result.includes('not executed')"),true);
  pass("repeated submit queues once; demo claims and reports echo only");
  pass("instruction markup stays literal text, with no executable HTML");
  await screenshot("dashboard-desktop.png",1280,1100);
  await screenshot("dashboard-mobile.png",390,900);
  assert.equal(await evaluate("document.documentElement.scrollWidth<=window.innerWidth+1"),true);
  pass("desktop and 390px mobile render without horizontal overflow");
  // Deferred POST responses exercise single-flight and retry identity without any real model calls.
  await evaluate("window.__composerBaseFetch=window.fetch;window.__composerPosts=[];window.__holdComposerA=true;window.__loseComposerB=false;window.fetch=async(...args)=>{const options=args[1]||{};if(String(args[0])==='/api/commands'&&options.method==='POST'){window.__composerPosts.push(JSON.parse(options.body));const response=await window.__composerBaseFetch(...args);if(window.__holdComposerA){window.__holdComposerA=false;await new Promise(resolve=>window.__releaseComposerA=resolve);}else if(window.__loseComposerB){window.__loseComposerB=false;await new Promise(resolve=>window.__releaseComposerB=resolve);throw new Error('simulated lost POST response after server admission');}return response;}return window.__composerBaseFetch(...args);};document.getElementById('instruction').value='COMPOSER_DEFERRED_A';document.getElementById('command-form').requestSubmit();true");
  await wait("typeof window.__releaseComposerA==='function'","A POST response held");
  await evaluate("refresh(false,true);true");
  await wait("latest&&latest.commands.some(c=>c.text==='COMPOSER_DEFERRED_A')","refresh during A POST");
  assert.equal(await evaluate("document.getElementById('send').disabled&&document.getElementById('instruction').disabled&&document.getElementById('target').disabled&&document.getElementById('delivery-mode').disabled"),true);
  await evaluate("document.getElementById('instruction').value='COMPOSER_DEFERRED_B';document.getElementById('command-form').requestSubmit();true");
  await delay(150);
  assert.equal(await evaluate("window.__composerPosts.length"),1);
  await evaluate("window.__releaseComposerA();true");
  await wait("composerFlight===null","A settled");
  assert.equal(await evaluate("document.getElementById('instruction').value"),'COMPOSER_DEFERRED_B');
  pass("refresh cannot reopen an in-flight composer; late A preserves a newer draft");
  await evaluate("window.__loseComposerB=true;document.getElementById('command-form').requestSubmit();true");
  await wait("typeof window.__releaseComposerB==='function'","B admitted with response held");
  await evaluate("refresh(false,true);true");
  assert.equal(await evaluate("document.getElementById('send').disabled"),true);
  await evaluate("window.__releaseComposerB();true");
  await wait("composerFlight===null&&document.getElementById('notice').textContent.includes('投递未确认')","B response lost");
  const retryId=await evaluate("window.__composerPosts[1].command_id");
  assert.equal(await evaluate("commandAttempt.command_id"),retryId);
  await evaluate("document.getElementById('command-form').requestSubmit();true");
  await wait("composerFlight===null&&window.__composerPosts.length===3","B retry settled");
  assert.equal(await evaluate("window.__composerPosts[2].command_id"),retryId);
  await evaluate("refresh(false,true);true");
  await wait("latest&&latest.commands.filter(c=>c.text==='COMPOSER_DEFERRED_B').length===1","one B command only");
  await evaluate("window.fetch=window.__composerBaseFetch;true");
  pass("unknown POST outcome retries the same command ID without duplicate queue entries");
  await cdp("Page.reload");
  await wait("document.readyState==='complete'&&!document.getElementById('dashboard').hidden&&latest!==null","session reload");
  pass("same-tab reload restores session access");
  // Hold a real completed response to deterministically exercise the lock/fetch race.
  await evaluate("window.__originalFetch=window.fetch;window.__held=false;window.fetch=async(...args)=>{const response=await window.__originalFetch(...args);if(String(args[0]).startsWith('/api/snapshot')&&!window.__held){window.__held=true;await new Promise(resolve=>window.__releaseFetch=resolve);}return response;};refresh(false,true);true");
  await wait("typeof window.__releaseFetch==='function'","snapshot response held");
  await evaluate("document.getElementById('logout').click();window.__releaseFetch();window.fetch=window.__originalFetch");
  await delay(300);
  assert.equal(await evaluate("document.getElementById('dashboard').hidden"),true);
  assert.equal(await evaluate("document.getElementById('workers').childElementCount"),0);
  assert.equal(await evaluate("document.getElementById('order-history').childElementCount"),0);
  assert.equal(await evaluate("sessionStorage.getItem('team-sync-session')"),null);
  pass("lock stays locked when an older in-flight snapshot resolves");
  await login(token);
  await wait("!document.getElementById('dashboard').hidden&&latest!==null","unlock after lock");
  pass("unlock works again after lock");
  assert.deepEqual(exceptions,[]);
  const report={passed:true,browser:"installed Chrome headless; isolated temporary profile",checks:passes,
    screenshots:["project-home-desktop.png","project-home-mobile.png","project-departments-desktop.png","department-work-desktop.png",
      "department-work-mobile.png","department-review-desktop.png","department-blocker-desktop.png",
      "dashboard-desktop.png","dashboard-mobile.png","native-metadata-desktop.png"],runtimeExceptions:exceptions};
  fs.writeFileSync(path.join(output,"browser-results.json"),JSON.stringify(report,null,2));
  process.stdout.write("BROWSER QA PASS: "+passes.length+" checks\n");
 }finally{
  if(ws&&ws.readyState===WebSocket.OPEN){try{await cdp("Browser.close");}catch{}}
  if(child.exitCode===null){await Promise.race([once(child,"exit"),delay(5000)]);if(child.exitCode===null)child.kill();}
 }
})().catch(error=>{process.stderr.write(error.stack+"\n");process.exitCode=1;});
