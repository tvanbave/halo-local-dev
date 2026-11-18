/* Synthetic browser checks: a scene job's end must reach the page even when the live stream is
   late, silent or refused. No live mesh or broker; every request is answered by this script. */
const fs=require('fs'),path=require('path'),assert=require('assert'),pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),engine=process.env.BROWSER_ENGINE||'chromium',out=process.env.EVIDENCE_DIR||'/tmp/halo-scene-live-updates';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await pw[engine].launch(engine==='chromium'?{headless:true,executablePath:process.env.CHROME_EXECUTABLE,args:['--disable-background-networking']}:{headless:true});const results=[];
 for(const [name,width,height] of [['desktop',1280,900],['phone',390,664]]){
  const context=await browser.newContext({viewport:{width,height},isMobile:width<500,hasTouch:width<500,serviceWorkers:'block'}),page=await context.newPage();page.setDefaultTimeout(5000);
  const calls=[],errors=[],bridge={refuseStreams:0,quietStreams:false,streams:0,sceneReads:0,holdRead:null,staleFirstRead:false,jobs:0};
  const scene={id:'s1',name:'Evening',status:'ready',activation_ready:true,exposed:false,members:[{target:40001,brightness:128,kelvin:4000}]};
  const workflow={scenes:[scene],operations:[],capabilities:{supported_models:[93,134]},job:null,enabled:true};
  const snapshot=()=>({status:{mesh_ready:true,pending_transactions:0,mqtt_enabled:true,mqtt_ready:true,instance:'s1',inventory_revision:'r1',jobs:{}},
   inventory:{devices:[{device_id:40001,product_id:134,name:'Reading bulb'}],groups:[]},device_states:{},bindings:{},controllers:[],device_rooms:{},
   scene_workflow:JSON.parse(JSON.stringify(workflow)),group_workflow:{rooms:[],groups:[]}});
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',async route=>{
   const u=new URL(route.request().url());if(u.origin!=='http://synthetic.invalid')return route.abort();
   const assets={'/':'ui.html','/ui.js':'ui.js','/ui.css':'ui.css','/ops.js':'ops.js','/settings.js':'settings.js','/backup.js':'backup.js'};
   if(assets[u.pathname])return route.fulfill({contentType:u.pathname.endsWith('.css')?'text/css':u.pathname==='/'?'text/html':'text/javascript',body:fs.readFileSync(path.join(root,'avionmesh_pi/web',assets[u.pathname]),'utf8')});
   const send=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
   if(u.pathname==='/api/capabilities')return send({features:{scenes:true,control:true,labels:true},types:{controllers:{},lights:{134:'A19'},color_temp:[134]}});
   if(u.pathname==='/api/snapshot')return send(snapshot());
   if(u.pathname==='/api/events'){bridge.streams++;if(bridge.refuseStreams>0){bridge.refuseStreams--;return send({error:'too_many_event_streams'},503);}
    if(bridge.quietStreams)return; // Left open and silent, so the stream itself causes no redraw.
    return route.fulfill({contentType:'text/event-stream',body:''});} // An empty stream: this page gets no pushed updates.
   if(u.pathname==='/api/scenes'){
    bridge.sceneReads++;const view=JSON.parse(JSON.stringify(workflow));
    if(bridge.staleFirstRead){bridge.staleFirstRead=false;view.job={...view.job,state:'running'};delete view.job.result;}
    if(bridge.holdRead){const hold=bridge.holdRead;bridge.holdRead=null;hold.seen();await hold.release;}
    return send(view);}
   if(u.pathname==='/api/scenes/activate'){
    calls.push(route.request().postDataJSON());const id='job'+(++bridge.jobs);
    // The bridge answers at once and finishes the single recall a moment later.
    workflow.job={id,operation:'activate',state:'complete',result:{scene_id:'s1',sent:true,physical_state_confirmed:false}};
    return send({started:true,job:{id,operation:'activate',state:'running'}},202);}
   return send({error:'unexpected request'},404);
  });
  const banner=()=>page.locator('#sceneLiveProgress .callout.info');
  const reset=async()=>{workflow.job=null;await page.evaluate(s=>{applySnapshot(s);render()},snapshot());await page.getByRole('button',{name:'Activate',exact:true}).waitFor();
   await page.waitForFunction(()=>!document.querySelector('button[onclick^="activateScene"]').disabled);};
  try{
   await page.goto('http://synthetic.invalid');await page.waitForFunction(()=>S.synced);await page.getByRole('button',{name:'Scenes',exact:true}).click();
   await page.getByRole('button',{name:'Activate',exact:true}).waitFor();

   // 1. No pushed update ever arrives, and the first read after the request still says "running".
   bridge.staleFirstRead=true;await page.getByRole('button',{name:'Activate',exact:true}).click();
   await page.getByText('Scene activation sent. Check the lights to confirm the result.',{exact:true}).waitFor({timeout:4000});
   assert.equal(await banner().count(),0,'The activating banner stayed after the bridge finished');
   assert(await page.getByRole('button',{name:'Activate',exact:true}).isEnabled(),'Activate stayed disabled after the bridge finished');
   assert.equal(calls.length,1,'Activation must be sent once');

   // 2. A slow read answered before the job ended arrives after a pushed update that already showed the end.
   await reset();let seen,release;const waitSeen=new Promise(r=>seen=r);bridge.holdRead={seen,release:new Promise(r=>release=r)};bridge.staleFirstRead=true;
   await page.getByRole('button',{name:'Activate',exact:true}).click();await waitSeen;
   await page.evaluate(s=>applySnapshot(s),snapshot());           // pushed update: finished
   assert.equal(await page.evaluate(()=>S.sceneWorkflow.job.state),'complete');
   release();await sleep(120);                                    // late reply: still "running"
   assert.equal(await page.evaluate(()=>S.sceneWorkflow.job.state),'complete','A late reply brought a finished job back to running');
   await page.evaluate(s=>applySnapshot(s),snapshot());await sleep(300); // identical pushed update
   assert.equal(await banner().count(),0,'The activating banner stayed after an identical update');
   assert.equal(calls.length,2,'Each press sends one activation');

   // 3. A read that changes what is shown must not stop the next pushed update from redrawing the page.
   //    Direct reads are held back and the stream is silent, so only the pushed update can clear the banner.
   bridge.quietStreams=true;await page.evaluate(()=>connect());await sleep(300);await reset();await page.evaluate(s=>applySnapshot(s),snapshot());let free;bridge.holdRead={seen:()=>{},release:new Promise(r=>free=r)};
   await page.evaluate(()=>{S.sceneWorkflow={...S.sceneWorkflow,job:{operation:'activate',state:'running'}};sceneShown();render();});
   await banner().waitFor();await sleep(400);assert.equal(await banner().count(),1); // let any redraw already queued finish first
   await page.evaluate(s=>applySnapshot(s),snapshot());
   await banner().waitFor({state:'detached',timeout:2000});free();bridge.quietStreams=false;

   if(name==='desktop'){
    // 4. A refused live stream is asked for again; the browser does not retry a refusal by itself.
    const before=bridge.streams;bridge.refuseStreams=1;await page.evaluate(()=>connect());
    await page.waitForFunction(()=>true);const deadline=Date.now()+9000;
    while(Date.now()<deadline&&bridge.streams<before+2)await sleep(250);
    assert(bridge.streams>=before+2,'A refused live stream was never requested again');
   }
   assert.equal(errors.length,0,errors.join('\n'));
   results.push({name,passed:true});
  }catch(e){results.push({name,passed:false,error:e.stack,errors});await page.screenshot({path:path.join(out,name+'-failure.png')});}
  await context.close();
 }
 await browser.close();fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({engine,results},null,2));console.log(JSON.stringify({engine,results},null,2));if(results.some(r=>!r.passed))process.exitCode=1;
})();
