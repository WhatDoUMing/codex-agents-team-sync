"use strict";
// Real Chromium UI tests against an isolated local test server. Never uses the user's browser profile.
const fs=require("node:fs"), path=require("node:path"), {spawn}=require("node:child_process");
const {once}=require("node:events");
const assert=require("node:assert/strict");
const url=process.env.TEAM_SYNC_QA_URL, token=process.env.TEAM_SYNC_QA_TOKEN, temp=process.env.TEAM_SYNC_QA_TEMP;
if(!url||!token||!temp)throw new Error("Run via python -B run_browser_qa.py");
const chrome=process.env.TEAM_SYNC_CHROME || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const output=path.join(__dirname,"verification");
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
  await evaluate("document.getElementById('logout').click()");
  await login("incorrect-session-key");
  await wait("document.getElementById('notice').textContent.includes('解锁失败')","invalid key feedback");
  assert.equal(await evaluate("document.getElementById('dashboard').hidden"),true);
  pass("wrong key is rejected in the real page");
  await login(token);
  await wait("!document.getElementById('dashboard').hidden&&latest!==null","valid key unlock");
  pass("authenticated worker and source cards render");
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
  assert.equal(await evaluate("sessionStorage.getItem('team-sync-session')"),null);
  pass("lock stays locked when an older in-flight snapshot resolves");
  await login(token);
  await wait("!document.getElementById('dashboard').hidden&&latest!==null","unlock after lock");
  pass("unlock works again after lock");
  assert.deepEqual(exceptions,[]);
  const report={passed:true,browser:"installed Chrome headless; isolated temporary profile",checks:passes,
    screenshots:["dashboard-desktop.png","dashboard-mobile.png","native-metadata-desktop.png"],runtimeExceptions:exceptions};
  fs.writeFileSync(path.join(output,"browser-results.json"),JSON.stringify(report,null,2));
  process.stdout.write("BROWSER QA PASS: "+passes.length+" checks\n");
 }finally{
  if(ws&&ws.readyState===WebSocket.OPEN){try{await cdp("Browser.close");}catch{}}
  if(child.exitCode===null){await Promise.race([once(child,"exit"),delay(5000)]);if(child.exitCode===null)child.kill();}
 }
})().catch(error=>{process.stderr.write(error.stack+"\n");process.exitCode=1;});
