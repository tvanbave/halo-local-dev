/* Synthetic browser checks for scene forms, progress and mobile navigation. */
const fs=require('fs'),path=require('path'),assert=require('assert'),pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),engine=process.env.BROWSER_ENGINE||'chromium',out=process.env.EVIDENCE_DIR||'/tmp/halo-scene-browser';
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await pw[engine].launch(engine==='chromium'?{headless:true,executablePath:process.env.CHROME_EXECUTABLE,args:['--disable-background-networking']}:{headless:true});const results=[];
 for(const [name,width,height] of [['desktop',1280,900],['phone',390,664],['small-phone',320,568]]){
  const context=await browser.newContext({viewport:{width,height},isMobile:width<500,hasTouch:width<500,serviceWorkers:'block'}),page=await context.newPage();page.setDefaultTimeout(5000);
  const calls=[],errors=[],workflow={scenes:[],operations:[],capabilities:{supported_models:[93,134]},job:null};
  const snapshot={status:{mesh_ready:true,pending_transactions:0,mqtt_enabled:true,mqtt_ready:true,instance:'s1',inventory_revision:'r1',jobs:{}},inventory:{devices:[{device_id:40001,product_id:134,name:'Reading bulb'},{device_id:40002,product_id:93,name:'Ceiling light'},{device_id:40003,product_id:127,name:'Keypad'}],groups:[{group_id:300,name:'Room',members:[40001,40002]}]},device_states:{},scene_workflow:workflow};
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',async route=>{
   const u=new URL(route.request().url());if(u.origin!=='http://synthetic.invalid')return route.abort();
   const assets={'/':'ui.html','/ui.js':'ui.js','/ui.css':'ui.css','/ops.js':'ops.js','/settings.js':'settings.js','/backup.js':'backup.js'};
   if(assets[u.pathname])return route.fulfill({contentType:u.pathname.endsWith('.css')?'text/css':u.pathname==='/'?'text/html':'text/javascript',body:fs.readFileSync(path.join(root,'avionmesh_pi/web',assets[u.pathname]),'utf8')});
   const send=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
   if(u.pathname==='/api/capabilities')return send({features:{scenes:true,pair_lights:true,control:true,labels:true},types:{controllers:{127:'Wired keypad'},lights:{93:'RL4',134:'A19'},color_temp:[93,134]}});
   if(u.pathname==='/api/snapshot')return send(snapshot);
   if(u.pathname==='/api/events')return route.fulfill({contentType:'text/event-stream',body:''});
   if(u.pathname==='/api/scenes')return send(workflow);
   if(u.pathname.startsWith('/api/scenes/')){calls.push({path:u.pathname,body:route.request().postDataJSON()});workflow.job={state:'running',operation:u.pathname.split('/').pop()};return send({started:true},202);}
   return send({error:'unexpected request'},404);
  });
  try{
   await page.goto('http://synthetic.invalid');await page.waitForFunction(()=>S.synced);await page.getByRole('button',{name:'Scenes',exact:true}).click();
   assert.equal(await page.locator('#navItems button').count(),8);
   if(width<500)assert(await page.locator('#navItems').evaluate(n=>[...n.children].every(e=>{const r=e.getBoundingClientRect();return r.width>=44&&r.height>=44&&r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight})), 'All eight nav destinations need visible touch targets');
   await page.getByRole('button',{name:'Create scene',exact:true}).click();await page.locator('#sceneName').fill('Evening');
   await page.getByRole('button',{name:'Add Room lights',exact:true}).click();assert.equal(await page.locator('.scene-member').count(),2);
   assert(await page.getByText('2 of 2 lights selected').isVisible());
   await page.locator('#sceneLevel40002').fill('40');await page.locator('#sceneWhite40002').selectOption('3500');
   await page.locator('#sceneBulkLevel').fill('65');await page.locator('#sceneBulkWhite').selectOption('2700');
   await page.getByRole('button',{name:'Apply to selected lights'}).click();
   assert.equal(calls.length,0);assert.equal(await page.locator('#sceneLevel40002').inputValue(),'65');
   assert.equal(await page.locator('#sceneWhite40002').inputValue(),'2700');
   for(const [selector,value] of [['#sceneName','Evening reading'],['#sceneLevel40001','25']]){
    const el=page.locator(selector);await el.fill(value);const handle=await el.elementHandle();
    await page.evaluate(s=>{applySnapshot(s);render(true)},snapshot);
    assert(await handle.evaluate(e=>e.isConnected&&e===document.getElementById(e.id)),'Input was replaced while editing');assert.equal(await el.inputValue(),value);
   }
   await page.locator('#sceneWhite40001').focus();const select=await page.locator('#sceneWhite40001').elementHandle();
   await page.locator('#sceneWhite40001').evaluate(e=>e.blur());await page.evaluate(()=>render(true));
   assert(await select.evaluate(e=>e.isConnected),'Open dropdown detached on refresh');
   await page.locator('#sceneWhite40001').selectOption('3500');await page.locator('.drawer-head h3').click();
   await page.evaluate(s=>{applySnapshot(s);render(true)},snapshot);assert.equal(await page.locator('#sceneLevel40001').inputValue(),'25');assert.equal(await page.locator('#sceneWhite40001').inputValue(),'3500');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Scene page overflows');
   const save=page.getByRole('button',{name:'Save scene',exact:true});await save.scrollIntoViewIfNeeded();assert(await save.evaluate(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}),'Save is hidden behind mobile navigation');
   await page.screenshot({path:path.join(out,name+'-editor.png')});await save.click();await page.waitForFunction(()=>!S.drawer);assert.equal(calls.length,1);assert.equal(calls[0].body.members.length,2);assert.equal(calls[0].body.members.find(m=>m.target===40001).brightness,64);
   assert.equal(calls[0].body.members.find(m=>m.target===40002).brightness,166);assert.equal(calls[0].body.members.find(m=>m.target===40002).kelvin,2700);
   workflow.job={state:'failed',operation:'create',error:'Readback needs review'};workflow.scenes=[{id:'s1',name:'Evening',status:'needs_review',members:calls[0].body.members}];workflow.operations=[{transaction:'t1',scene_id:'s1',state:'uncertain',progress:{recovery_ready:true,steps:[{target:40001,property:'colour',status:'verified'},{target:40002,property:'brightness',status:'not_started'}]}}];
   await page.evaluate(s=>{applySnapshot(s);render()},snapshot);await page.getByRole('button',{name:'Read and recover',exact:true}).waitFor();assert(await page.getByRole('button',{name:'Activate',exact:true}).isDisabled());assert(await page.getByRole('button',{name:'Continue save',exact:true}).isEnabled());
   await page.getByRole('button',{name:'Read and recover',exact:true}).click();assert.equal(calls.length,2);assert.equal(calls[1].path,'/api/scenes/recover');assert.equal(calls[1].body.transaction,'t1');assert.equal(errors.length,0,errors.join('\n'));
   workflow.job=null;workflow.operations=[];workflow.scenes=[{id:'s1',name:'Evening',status:'ready',activation_ready:true,members:calls[0].body.members}];
   await page.evaluate(s=>{applySnapshot(s);openScene('s1');render()},snapshot);
   await page.getByText('Activation troubleshooting',{exact:true}).click();
   await page.waitForFunction(()=>sceneComparisonOpen.has('s1'));
   await page.evaluate(s=>{applySnapshot(s);render(true)},snapshot);
   assert(await page.locator('[data-scene-key="comparison"] details').evaluate(e=>e.open),'Comparison disclosure collapsed on status refresh');
   const compare=page.getByRole('button',{name:'Test alternative activation',exact:true});
   await compare.scrollIntoViewIfNeeded();assert(await compare.evaluate(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}),'Comparison action covered by mobile navigation');
   await compare.click();assert.equal(calls.length,3);assert.equal(calls[2].path,'/api/scenes/activate-keypad-test');assert.deepEqual(calls[2].body,{id:'s1'});
   workflow.job=null;
   await page.evaluate(s=>{applySnapshot(s);render(true)},snapshot);
   const quiet=page.getByRole('button',{name:'Test without status reads',exact:true});
   await quiet.scrollIntoViewIfNeeded();assert(await quiet.evaluate(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}),'Quiet comparison covered by mobile navigation');
   await quiet.click();assert.equal(calls.length,4);assert.equal(calls[3].path,'/api/scenes/activate-quiet-test');assert.deepEqual(calls[3].body,{id:'s1'});
   assert.equal(errors.length,0,errors.join('\n'));
   results.push({name,passed:true});
  }catch(e){results.push({name,passed:false,error:e.stack,errors});await page.screenshot({path:path.join(out,name+'-failure.png')});}
  await context.close();
 }
 await browser.close();fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({engine,results},null,2));console.log(JSON.stringify({engine,results},null,2));if(results.some(r=>!r.passed))process.exitCode=1;
})();
