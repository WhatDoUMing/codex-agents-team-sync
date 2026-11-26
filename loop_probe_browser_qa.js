"use strict";
// Real Chromium UI tests against an isolated local test server. Never uses the user's browser profile.
const fs=require("node:fs"), path=require("node:path"), {spawn}=require("node:child_process");
const {once}=require("node:events");
const assert=require("node:assert/strict");
const url=process.env.TEAM_SYNC_QA_URL, token=process.env.TEAM_SYNC_QA_TOKEN, temp=process.env.TEAM_SYNC_QA_TEMP;
if(!url||!token||!temp)throw new Error("Run via python -B run_browser_qa.py");
const chrome=process.env.TEAM_SYNC_CHROME || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const output=temp;
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
  await login(token);
  await wait("!document.getElementById('loop-probe').hidden&&!document.getElementById('probe-dispatch').disabled","isolated probe ready");
  await evaluate("document.getElementById('probe-dispatch').click();document.getElementById('probe-dispatch').click();true");
  await until(async()=> {
    const words=await evaluate("document.getElementById('probe-status').textContent");
    if(words.includes("执行失败")||words.includes("结果未知")||words.includes("无法确认"))throw new Error(words);
    return words.includes("#1")&&words.includes("收到完成回执，回执匹配");
  },"same run completion shown by CAS",110000);
  assert.equal(await evaluate("document.getElementById('probe-dispatch').disabled"),true);
  assert.equal(exceptions.length,0);
  pass("one real browser dispatch; same completed run ID rendered; second click blocked");
 }catch(e){console.error(e.message);process.exitCode=1;}
 finally{
   if(ws){try{await cdp("Browser.close");}catch{} ws.close();}
   child.kill();
 }
})().catch(e=>{console.error(e.message);process.exitCode=1});
