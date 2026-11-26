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
const wait=async(expression,label)=>until(()=>evaluate(expression),label,110000);
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
  await until(()=>fs.existsSync(active),"isolated Chrome startup");
  const port=fs.readFileSync(active,"utf8").split(/\r?\n/)[0];
  const tabs=await(await fetch("http://127.0.0.1:"+port+"/json/list")).json();
  ws=new WebSocket(tabs.find(t=>t.type==="page").webSocketDebuggerUrl);await once(ws,"open");
  ws.addEventListener("message",event=>{const m=JSON.parse(event.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}else if(m.method==="Runtime.exceptionThrown")exceptions.push(m.params.exceptionDetails);});
  await cdp("Runtime.enable");await cdp("Page.enable");
  await cdp("Emulation.setDeviceMetricsOverride",{width:1280,height:1100,deviceScaleFactor:1,mobile:false});
  await cdp("Page.navigate",{url});
  await wait("document.readyState==='complete'&&!!document.getElementById('login-form')","login page");
  await login(token);
  await wait("latest&&latest.owned_codex&&latest.owned_codex.enabled&&!document.getElementById('start-owned').hidden","explicit bridge enabled");
  pass("authenticated page exposes explicitly enabled real-worker action");
  await evaluate("document.getElementById('start-owned').click();true");
  await wait("latest&&latest.workers.length===1&&latest.workers[0].native&&latest.workers[0].native.state==='idle'&&document.getElementById('target').value!==''","new real worker ready");
  assert.equal(await evaluate("latest.workers[0].native.environment_access"),false);
  assert.equal(await evaluate("latest.workers[0].native.owned_here"),true);
  pass("page starts a new owned environment-disabled Codex worker");
  await evaluate("document.getElementById('instruction').value='请先调用bridge_wait等待8秒，期间操作者会追加随机要求；最后仅逐字回复追加要求指定的随机词，不读文件、不执行命令或外部服务。';document.getElementById('command-form').requestSubmit();true");
  await wait("latest&&latest.workers[0].native.waiting_for_operator","real model bounded wait");
  const nonce="BROWSER_STEER_"+require("node:crypto").randomBytes(16).toString("hex");
  await evaluate("document.getElementById('delivery-mode').value='steer';document.getElementById('delivery-mode').dispatchEvent(new Event('change'));document.getElementById('instruction').value="+JSON.stringify("追加要求：最终仅逐字回复 "+nonce)+";document.getElementById('command-form').requestSubmit();true");
  await wait("latest&&latest.commands.length===2&&latest.commands.every(c=>c.state==='completed')","real model reflects steer");
  const answers=await evaluate("latest.commands.map(c=>({result:c.result,turn:c.native_delivery.turn_id,mode:c.native_delivery.mode}))");
  assert(answers.every(x=>x.result.trim()===nonce));assert.equal(new Set(answers.map(x=>x.turn)).size,1);
  assert(answers.some(x=>x.mode==="steer"));
  pass("HTML form steers the active real turn; random nonce is reflected exactly");
  await screenshot("owned-real-worker-desktop.png",1280,1100);
  await screenshot("owned-real-worker-mobile.png",390,900);
  assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth+1"),true);
  pass("real worker and model replies render on desktop and mobile");
  const second="BROWSER_SECOND_"+require("node:crypto").randomBytes(16).toString("hex");
  await evaluate("document.getElementById('delivery-mode').value='queue';document.getElementById('delivery-mode').dispatchEvent(new Event('change'));document.getElementById('instruction').value="+JSON.stringify("独立下一轮任务，仅回复 "+second+"，不要调用工具。")+";document.getElementById('command-form').requestSubmit();true");
  await wait("latest&&latest.commands.length===3&&latest.commands.every(c=>c.state==='completed')","second real turn");
  assert.equal(await evaluate("latest.commands[0].result.trim()"),second);
  pass("same live worker accepts and completes a subsequent independent turn");
  await evaluate("document.getElementById('instruction').value='调用bridge_wait等待8秒，然后回复WAIT_DONE。';document.getElementById('command-form').requestSubmit();true");
  await wait("latest&&latest.workers[0].native.waiting_for_operator","active interrupt target");
  await evaluate("[...document.querySelectorAll('#workers button')].find(b=>b.textContent.includes('请求停止')).click();true");
  await wait("latest&&latest.workers[0].native.state==='stopped'&&latest.workers[0].native.cleanup_confirmed===1&&latest.workers[0].status==='stopped'","confirmed owned shutdown");
  assert.equal(await evaluate("latest.commands[0].native_delivery.phase"),"turn_interrupted");
  assert.equal(await evaluate("latest.commands[0].state"),"rejected");
  pass("HTML stop interrupts the active turn and waits for owned-process exit");
  assert.deepEqual(exceptions,[]);
  const report={passed:true,real_model:true,existing_sessions_touched:false,checks:passes,
    screenshots:["owned-real-worker-desktop.png","owned-real-worker-mobile.png"],runtimeExceptions:exceptions};
  fs.writeFileSync(path.join(output,"owned-browser-results.json"),JSON.stringify(report,null,2));
  process.stdout.write("REAL BROWSER QA PASS: "+passes.length+" checks\n");
 }finally{
  if(ws&&ws.readyState===WebSocket.OPEN){try{await cdp("Browser.close");}catch{}}
  if(child.exitCode===null){await Promise.race([once(child,"exit"),delay(5000)]);if(child.exitCode===null)child.kill();}
 }
})().catch(error=>{process.stderr.write(error.stack+"\n");process.exitCode=1;});
