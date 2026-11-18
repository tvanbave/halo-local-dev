/* Auto-naming UI checks. Synthetic requests only; no scanning, pairing or radio. */
const fs=require('fs'),path=require('path'),assert=require('assert'),pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),engine=process.env.BROWSER_ENGINE||'chromium',out=process.env.EVIDENCE_DIR||'/tmp/halo-pairing-names';
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await pw[engine].launch({headless:true,...(engine==='chromium'?{executablePath:process.env.CHROME_EXECUTABLE}: {})}),results=[];
 for(const [name,width,height] of [['desktop',1280,900],['phone',390,664],['small-phone',320,568],['landscape',780,360]]){
  const context=await browser.newContext({viewport:{width,height},isMobile:width<821,hasTouch:width<821,serviceWorkers:'block'}),page=await context.newPage();page.setDefaultTimeout(5000);
  const calls=[],errors=[],snapshot={status:{mesh_ready:true,mqtt_enabled:true,mqtt_ready:true,pending_transactions:0,jobs:{},instance:'one',inventory_revision:'r1'},inventory:{devices:[{device_id:30001,product_id:93,name:'Theatre 1'},{device_id:30002,product_id:93,name:'Theatre 2'}],groups:[]},device_states:{},device_rooms:{30001:'Theatre',30002:'Theatre'}};
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());if(u.origin!=='http://synthetic.invalid')return route.abort();
   const file={'/':'ui.html','/ui.js':'ui.js','/ui.css':'ui.css','/ops.js':'ops.js','/settings.js':'settings.js','/backup.js':'backup.js'}[u.pathname];
   if(file)return route.fulfill({contentType:file.endsWith('.html')?'text/html':file.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(path.join(root,'avionmesh_pi/web',file),'utf8')});
   const send=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
   if(u.pathname==='/api/capabilities')return send({features:{pair_lights:true,labels:true,control:true,pairable_types:[93,127]},types:{controllers:{127:'Wired keypad'},lights:{93:'RL4'},color_temp:[93]}});
   if(u.pathname==='/api/snapshot')return send(snapshot);
   if(u.pathname==='/api/events')return route.fulfill({contentType:'text/event-stream',body:''});
   if(u.pathname==='/api/claim'){
    const body=req.postDataJSON();calls.push({path:u.pathname,body});const id=50000+calls.filter(c=>c.path==='/api/claim').length;
    snapshot.inventory.devices.push({device_id:id,product_id:body.product_id,name:body.name,exposed:false});
    snapshot.status.inventory_revision='r'+(calls.length+1);return send({device_id:id,associated:true});
   }
   if(u.pathname==='/api/device-rooms'){
    const body=req.postDataJSON();calls.push({path:u.pathname,body});Object.assign(snapshot.device_rooms,body.rooms);return send({saved:true,device_rooms:snapshot.device_rooms});
   }
   if(u.pathname==='/api/metadata'){
    const body=req.postDataJSON();calls.push({path:u.pathname,body});assert.equal(body.operation,'update');
    const d=snapshot.inventory.devices.find(d=>d.device_id===body.target);d.name=body.name;
    snapshot.status.inventory_revision='r'+(calls.length+1);return send({saved:true,revision:snapshot.status.inventory_revision});
   }
   calls.push({path:u.pathname,body:req.postData()});return route.fulfill({status:404,body:'{}'});
  });
  try{
   await page.goto('http://synthetic.invalid');await page.waitForFunction(()=>S.synced);await page.getByRole('navigation',{name:'Bridge sections'}).getByRole('button',{name:'Add devices',exact:true}).click();
   assert(!(await page.locator('#wizardAutoName').isChecked()));
   await page.locator('#wRoom').selectOption('__new');await page.locator('#wrnnull').fill('Theatre');await page.getByRole('button',{name:'Add room',exact:true}).click();
   await page.locator('#wizardAutoName').check();assert.equal(await page.locator('#wizardNamePrefix').inputValue(),'Theatre');
   assert.match(await page.locator('#wizardNamingHint').textContent(),/Theatre 3, Theatre 4/);
   await page.locator('#wizardNameStart').fill('5');
   const added=Array.from({length:10},(_,i)=>({device_id:40001+i,product_id:93,name:'New light '+(i+1),exposed:i%2===0}));
   snapshot.inventory.devices.push(...added,{device_id:40100,product_id:127,name:'New keypad'});
   for(const d of [...added,{device_id:40100}])snapshot.device_rooms[d.device_id]='Theatre';
   await page.evaluate(s=>{applySnapshot(s);S.wiz.claims=s.inventory.devices.filter(d=>d.device_id>=40001).map(d=>({id:d.device_id,type:d.product_id,name:d.name,st:'done'}));S.wiz.claims.push({id:40200,type:93,st:'fail',msg:'Read-only review needed'});S.wiz.step=2;render();},snapshot);
   await page.getByRole('button',{name:'Name 11 devices',exact:true}).click();
   assert.equal(await page.locator('#wn40001').inputValue(),'Theatre 5');assert.equal(await page.locator('#wn40010').inputValue(),'Theatre 14');assert.equal(await page.locator('#wn40100').inputValue(),'');assert.equal(await page.locator('#wn40200').count(),0);
   await page.locator('#wn40001').fill('Screen left');
   const prefix=page.locator('#wizardNamePrefix');await prefix.fill('Cinema');const handle=await prefix.elementHandle();
   await prefix.evaluate(e=>e.setSelectionRange(1,3));
   await page.evaluate(s=>{applySnapshot(s);render(true);},snapshot);
   assert(await handle.evaluate(e=>e.isConnected&&e===document.activeElement),'Prefix edit was interrupted by live updates');
   assert.deepEqual(await prefix.evaluate(e=>[e.selectionStart,e.selectionEnd]),[1,3]);
   assert.equal(await page.locator('#wn40001').inputValue(),'Screen left');assert.equal(await page.locator('#wn40002').inputValue(),'Cinema 5');
   await page.locator('#wizardAutoName').uncheck();assert.equal(await page.locator('#wn40001').inputValue(),'Screen left');assert.equal(await page.locator('#wn40002').inputValue(),'');
   await page.locator('#wizardAutoName').check();assert.equal(await page.locator('#wn40002').inputValue(),'Cinema 5');
   await prefix.fill('');await page.getByRole('button',{name:'Finish',exact:true}).click();assert.equal(calls.length,0);assert.match(await page.locator('#wizardNamingHint').textContent(),/prefix/);
   await prefix.fill('Cinema');await page.locator('#wizardNameStart').fill('1.5');await page.getByRole('button',{name:'Finish',exact:true}).click();assert.equal(calls.length,0);
   await page.locator('#wizardNameStart').fill('5');
   snapshot.inventory.devices.push({device_id:30003,product_id:93,name:'Cinema 5'});
   await page.evaluate(s=>{applySnapshot(s);render(true);},snapshot);await page.getByRole('button',{name:'Finish',exact:true}).click();assert.equal(calls.length,0);await page.getByText(/suggested name is now in use/).waitFor();
   await page.locator('#wizardNameStart').fill('8');assert.equal(await page.locator('#wn40002').inputValue(),'Cinema 8');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Naming page overflows horizontally');
   if(width<821)for(const id of ['wizardNamePrefix','wizardNameStart','wn40001'])assert(await page.locator('#'+id).evaluate(e=>parseFloat(getComputedStyle(e).fontSize)>=16),'Mobile input can trigger focus zoom');
   const finish=page.getByRole('button',{name:'Finish',exact:true});await finish.scrollIntoViewIfNeeded();
   assert(await finish.evaluate(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}),'Finish is hidden by navigation');
   await page.screenshot({path:path.join(out,name+'-review.png')});await finish.click();await page.waitForFunction(()=>!S.wiz);
   assert.equal(calls.length,10);assert(calls.every(c=>c.path==='/api/metadata'&&c.body.kind==='device'));
   assert.equal(calls[0].body.name,'Screen left');assert.equal(calls[1].body.name,'Cinema 8');assert.equal(calls[9].body.name,'Cinema 16');
   calls.forEach(c=>assert.equal(c.body.exposed,(c.body.target-40001)%2===0));
   assert.equal(snapshot.inventory.devices.find(d=>d.device_id===30001).name,'Theatre 1');assert.equal(snapshot.inventory.devices.find(d=>d.device_id===40100).name,'New keypad');assert.deepEqual(errors,[]);
   calls.length=0;
   await page.evaluate(()=>{
    go('add');S.wiz=newWizard();S.wiz.step=1;S.wiz.room='Theatre';S.wiz.searchComplete=true;wizardNaming().enabled=true;
    S.wiz.found={1:{uuid:'0011223344561400000a000000000001',uuid_hash:1,hash_hex:'00000001',detected_product_type:93,lastSeen:Date.now(),epoch:wizardDiscoveryEpoch(),sel:true},2:{uuid:'1021324354661400000a000000000002',uuid_hash:2,hash_hex:'00000002',detected_product_type:93,lastSeen:Date.now(),epoch:wizardDiscoveryEpoch(),sel:true}};render();
   });
   await page.getByRole('button',{name:'Pair 2 devices',exact:true}).click();
   await page.getByRole('button',{name:'Pair',exact:true}).click();
   await page.getByRole('button',{name:'Name 2 devices',exact:true}).waitFor();
   assert.deepEqual(calls.filter(c=>c.path==='/api/claim').map(c=>c.body.name),['Theatre 3','Theatre 4']);
   assert.equal(snapshot.device_rooms[50001],'Theatre');assert.equal(snapshot.inventory.devices.find(d=>d.device_id===50001).name,'Theatre 3');
   await page.reload();await page.waitForFunction(()=>S.synced);
   assert.equal(await page.evaluate(()=>S.devices.find(d=>d.avion_id===50001).name),'Theatre 3');
   assert(!calls.some(c=>c.path==='/api/metadata'),'Name must survive without Finish');
   assert.deepEqual(errors,[]);
   results.push({name,passed:true});
  }catch(e){results.push({name,passed:false,error:e.stack,errors});await page.screenshot({path:path.join(out,name+'-failure.png')});}
  await context.close();
 }
 await browser.close();fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({engine,results},null,2));console.log(JSON.stringify({engine,results},null,2));if(results.some(r=>!r.passed))process.exitCode=1;
})();
