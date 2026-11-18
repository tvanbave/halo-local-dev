/* Long scene forms with live updates. Every request is synthetic and intercepted. */
const fs=require('fs'),path=require('path'),assert=require('assert'),pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),engine=process.env.BROWSER_ENGINE||'chromium',out=process.env.EVIDENCE_DIR||'/tmp/halo-scene-scrolling';
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await pw[engine].launch({headless:true,...(engine==='chromium'?{executablePath:process.env.CHROME_EXECUTABLE||pw.chromium.executablePath()}: {})}),results=[];
 for(const [name,width,height] of [['desktop',1280,900],['phone',390,664],['small-phone',320,568],['landscape',780,320],['keyboard-sized',390,300]]){
  if(process.env.SCROLL_CASE&&process.env.SCROLL_CASE!==name)continue;
  const context=await browser.newContext({viewport:{width,height},isMobile:width<821,hasTouch:width<821,serviceWorkers:'block'}),page=await context.newPage();
  page.setDefaultTimeout(5000);const calls=[],errors=[];
  const devices=Array.from({length:24},(_,i)=>({device_id:40001+i,product_id:93,name:'Light '+String(i+1).padStart(2,'0')}));
  const scene={id:'s1',name:'Stored scene',status:'ready',exposed:false,members:devices.map(d=>({target:d.device_id,brightness:128,kelvin:4000}))};
  const workflow={scenes:[scene],operations:[],capabilities:{supported_models:[93,134]},job:null};
  const snapshot={status:{mesh_ready:true,pending_transactions:0,mqtt_enabled:true,mqtt_ready:true,instance:'s1',inventory_revision:'r1',jobs:{}},inventory:{devices,groups:[{group_id:300,name:'Room',members:devices.map(d=>d.device_id)}]},device_states:{},scene_workflow:workflow};
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());if(u.origin!=='http://synthetic.invalid')return route.abort();
   const file={'/':'ui.html','/ui.js':'ui.js','/ui.css':'ui.css','/ops.js':'ops.js','/settings.js':'settings.js','/backup.js':'backup.js'}[u.pathname];
   if(file)return route.fulfill({contentType:file.endsWith('.html')?'text/html':file.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(path.join(root,'avionmesh_pi/web',file),'utf8')});
   const send=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
   if(u.pathname==='/api/capabilities')return send({features:{scenes:true,control:true,labels:true},types:{controllers:{},lights:{93:'RL4'},color_temp:[93,134]}});
   if(u.pathname==='/api/snapshot')return send(snapshot);
   if(u.pathname==='/api/scenes')return send(workflow);
   if(u.pathname==='/api/events')return route.fulfill({contentType:'text/event-stream',body:''});
   calls.push({path:u.pathname,method:req.method()});return route.fulfill({status:404,body:'{}'});
  });
  try{
   await page.goto('http://synthetic.invalid');await page.waitForFunction(()=>S.synced);
   await page.getByRole('button',{name:'Scenes',exact:true}).click();
   for(const mode of ['create','edit']){
    if(mode==='create'){
     await page.getByRole('button',{name:'Create scene',exact:true}).click();
     await page.locator('#sceneName').fill('Evening');
     await page.getByRole('button',{name:'Add Room lights',exact:true}).click();
    }else await page.getByRole('button',{name:'Edit / details',exact:true}).click();
    await page.locator('#sceneLevel40001').fill('37');
    await page.locator('#sceneLevel40001').evaluate(e=>e.blur());
    await page.evaluate(()=>{render(true);window.auditBody=document.querySelector('.scene-editor');window.auditFirst=document.querySelector('#sceneLevel40001');window.auditDetached=false;new MutationObserver(records=>{for(const r of records)for(const n of r.removedNodes)if(n===auditBody||n.contains?.(auditBody))auditDetached=true;}).observe(document.querySelector('#drawerRoot'),{subtree:true,childList:true});});
    // A focus retained above the visible portion must never pull scrolling back.
    await page.locator('#sceneWhite40001').selectOption('3500');
    await page.locator('#sceneWhite40001').evaluate(e=>e.blur());
    await page.evaluate(()=>releaseNativeSelect());
    await page.waitForTimeout(200); // Finish the native picker dismissal before starting a new gesture.
    await page.locator('.scene-editor').evaluate(e=>e.scrollTop=0);
    const box=await page.locator('.scene-editor').boundingBox();
    const x=Math.round(box.x+(width>=821?box.width/2:8)),y=Math.round(box.y+(width>=821?180:box.height-25));
    await page.evaluate(()=>{window.lastWheel=null;document.addEventListener('wheel',e=>{lastWheel={target:e.target.className,y:e.clientY,x:e.clientX,dy:e.deltaY,prevented:e.defaultPrevented};},{once:true,passive:true});});
    if(engine==='chromium'&&width<821){
     const cdp=await context.newCDPSession(page);
     await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
     for(let i=1;i<=8;i++){
      await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y-i*Math.min(40,(box.height-40)/8)}]});
      await page.evaluate(s=>{applySnapshot(s);render(true);},snapshot);
     }
     await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await cdp.detach();
    }else if(width>=821){
     await page.mouse.move(x,y);await page.mouse.wheel(0,450);
    }else await page.locator('.scene-editor').evaluate(e=>e.scrollBy(0,450)); // WebKit does not expose touch drags or mobile wheel input.
    try{await page.waitForFunction(()=>document.querySelector('.scene-editor').scrollTop>0);}catch(e){throw Error('Scroll gesture failed: '+JSON.stringify(await page.evaluate(([x,y])=>({wheel:window.lastWheel,hit:document.elementFromPoint(x,y)?.outerHTML.slice(0,180),active:document.activeElement?.outerHTML.slice(0,180),style:getComputedStyle(auditBody).overflowY,inert:!!auditBody.closest('[inert]'),height:auditBody.clientHeight,content:auditBody.scrollHeight}),[x,y])));}
    assert(await page.locator('.scene-editor').evaluate(e=>e.scrollTop>0),'Scroll gesture did not move the form');
    let reached=false;
    for(let i=0;i<40;i++){
     await page.locator('.scene-editor').evaluate(e=>e.scrollBy(0,450));
     const before=await page.locator('.scene-editor').evaluate(e=>e.scrollTop);
     snapshot.device_states['40001']={brightness:{value:100+i,fresh:true,observed_ms:i}};
     await page.evaluate(s=>{applySnapshot(s);render(true);},snapshot);
     const state=await page.evaluate(()=>{const e=document.querySelector('.scene-editor');return {same:e===auditBody,first:document.querySelector('#sceneLevel40001')===auditFirst,detached:auditDetached,top:e.scrollTop,max:e.scrollHeight-e.clientHeight};});
     assert(state.same&&!state.detached,'Live update detached the scene scroll container');
     assert(state.first,'Live update detached an unchanged input');
     assert(state.top>=before-2,'Live update moved the scroll position backwards');
     if(state.max-state.top<2){reached=true;break;}
    }
    assert(reached,'Cannot reach the bottom of a long scene');
    const last=page.locator('.scene-editor .sec').last();
    assert(await last.evaluate(e=>{const r=e.getBoundingClientRect(),y=r.bottom-8;return y<innerHeight&&e.contains(document.elementFromPoint(r.left+20,y));}),'Last section is covered');
    assert.equal(await page.locator('#sceneLevel40001').inputValue(),'37');
    assert.equal(await page.locator('#sceneWhite40001').inputValue(),'3500');
    const save=page.getByRole('button',{name:mode==='create'?'Save scene':'Save changes',exact:true});
    assert(await save.evaluate(e=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight&&e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}),'Save is covered');
    await page.screenshot({path:path.join(out,name+'-'+mode+'-bottom.png')});
    // Structural updates preserve the scrolling element and unrelated fields.
    await page.evaluate(()=>{sceneToggleMember(40024,false);sceneToggleMember(40024,true);});
    assert(await page.evaluate(()=>document.querySelector('.scene-editor')===auditBody&&document.querySelector('#sceneLevel40001')===auditFirst&&!auditDetached),'Selection changed the scrolling element or unrelated fields');
    // Inline validation and reordering lights must not reuse the wrong row.
    await page.evaluate(()=>{sceneDraft.error='Choose valid settings';dev(40024).name='A renamed light';render();});
    assert.equal(await page.locator('.scene-member').first().getAttribute('data-scene-key'),'light-40024');
    assert.equal(await page.locator('#sceneLevel40001').inputValue(),'37');
    assert(await page.evaluate(()=>document.querySelector('#sceneLevel40001')===auditFirst),'Reordering detached an unrelated input');
    await page.evaluate(()=>{sceneDraft.error='';render();});
    // Status and safety changes still arrive without reconstructing the form.
    workflow.job={state:'running',operation:'verify'};
    await page.evaluate(s=>{applySnapshot(s);render(true);},snapshot);
    assert(await save.isDisabled(),'Save remained enabled during a scene operation');
    assert(await page.locator('#sceneLiveProgress').filter({hasText:'Verifying saved settings'}).count());
    workflow.job=null;await page.evaluate(s=>{applySnapshot(s);render(true);},snapshot);
    assert(await save.isEnabled(),'Save did not recover after status update');
    assert(await page.evaluate(()=>document.querySelector('.scene-editor')===auditBody&&!auditDetached),'Status change detached scroll container');
    if(name==='phone'){
     await page.setViewportSize({width,height:360});await page.locator('.scene-editor').evaluate(e=>e.scrollTop=e.scrollHeight);
     assert(await save.evaluate(e=>e.getBoundingClientRect().bottom<=innerHeight),'Footer obscured after viewport shrinks');
     await page.setViewportSize({width,height});
    }
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Horizontal overflow');
    await page.locator('.drawer-head button[aria-label="Close"]').click();
    // Edited values are never dropped silently: closing asks first.
    const discard=page.getByRole('button',{name:'Discard changes',exact:true});
    await discard.waitFor();assert(await page.locator('.scene-editor').count(),'Drawer closed before the owner chose');await discard.click();
    assert.equal(await page.locator('.scene-editor').count(),0,'Scene drawer stayed open');
   }
   assert.equal(calls.length,0,'Scrolling sent an unexpected request');assert.deepEqual(errors,[]);
   results.push({name,passed:true});
  }catch(e){results.push({name,passed:false,error:e.stack,errors});await page.screenshot({path:path.join(out,name+'-failure.png')});}
  await context.close();
 }
 await browser.close();fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({engine,results},null,2));console.log(JSON.stringify({engine,results},null,2));if(results.some(r=>!r.passed))process.exitCode=1;
})();
