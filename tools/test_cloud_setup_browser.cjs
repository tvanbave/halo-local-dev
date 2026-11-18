/* Synthetic first-install browser checks; every HTTP request is intercepted. */
const fs=require('fs'),path=require('path'),assert=require('assert');
const pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const engine=process.env.BROWSER_ENGINE||'chromium',root=path.resolve(__dirname,'..');
const evidence=process.env.EVIDENCE_DIR||'/tmp/halo-cloud-browser';
(async()=>{
 fs.mkdirSync(evidence,{recursive:true});
 const browser=await pw[engine].launch(engine==='chromium'?{headless:true,executablePath:process.env.CHROME_EXECUTABLE,args:['--disable-background-networking']}:{headless:true});
 const results=[],errors=[];
 try {
 for(const mobile of [false,true]){
  const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1280,height:960},isMobile:mobile,hasTouch:mobile,serviceWorkers:'block'});
  const page=await context.newPage();page.setDefaultTimeout(5000);
  page.on('pageerror',e=>errors.push(e.message));
  const calls=[];let rejectPreview=false;
  await context.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url());
   if(url.origin!=='http://synthetic.invalid')return route.abort();
   const assets={'/':['setup.html','text/html'],'/setup.js':['setup.js','application/javascript'],'/backup.js':['backup.js','application/javascript'],'/setup.css':['setup.css','text/css']};
   if(assets[url.pathname]){const [file,type]=assets[url.pathname];return route.fulfill({contentType:type,body:fs.readFileSync(path.join(root,'avionmesh_pi/web',file),'utf8')});}
   let body;try{body=req.postDataJSON();}catch{}
   calls.push({path:url.pathname,body});
   const send=(obj,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(obj)});
   if(url.pathname==='/api/setup/cloud/login')return send({session:'s1',locations:[{id:'a',name:'Synthetic home'},{id:'b',name:'Second home'}]});
   if(url.pathname==='/api/setup/cloud/preview'){
    if(rejectPreview)return send({error:'invalid_inventory',message:'This location contains unsupported records. Nothing was imported.'},409);
    return send({preview:'p1',location:{name:'Second home'},inventory:{devices:[{device_id:40002,name:'Synthetic A19'},{device_id:40003,name:'Synthetic keypad'}],groups:[{group_id:291,name:'Desk',members:[40002]}]}});
   }
   if(url.pathname==='/api/setup/cloud/cancel')return send({cancelled:true});
   if(url.pathname==='/api/setup/cloud/apply')return send({imported:true},201);
   if(url.pathname==='/api/capabilities')return send({},503);
   if(url.pathname==='/api/setup')return send({created:true},201);
   return send({},404);
  });
  await page.goto('http://synthetic.invalid');
  assert(await page.locator('#create').isDisabled());assert(await page.locator('#import').isDisabled());
  for (const id of ['freshPanel','cloudPanel','setupBackupMount']) assert(await page.locator('#'+id).isHidden());
  await page.locator('[data-setup-choice=fresh]').click();
  assert(await page.locator('#confirm').isVisible());
  assert.equal(await page.evaluate(()=>document.activeElement.id),'freshTitle');
  await page.locator('[data-setup-choice=restore]').click();
  assert(await page.locator('#freshPanel').isHidden());
  assert(await page.locator('#setupBackupMount').isVisible());
  await page.locator('[data-setup-choice=cloud]').click();
  assert(await page.locator('#setupBackupMount').isHidden());
  assert.equal(await page.evaluate(()=>document.activeElement.id),'cloudTitle');
  const email=page.getByLabel('Account email',{exact:true}),password=page.getByLabel('Account password',{exact:true});
  await email.fill('owner@example.invalid');await password.fill('synthetic-password');
  await page.getByRole('button',{name:'Sign in and find locations'}).click();
  await page.locator('#locationStep').waitFor({state:'visible'});assert.equal(await password.inputValue(),'');
  await page.locator('#location').selectOption('b');
  assert.equal(await page.locator('#location').inputValue(),'b');
  await page.waitForTimeout(300);assert.equal(await page.locator('#location').inputValue(),'b');
  await page.getByRole('button',{name:'Preview this location'}).click();
  await page.locator('#review').waitFor({state:'visible'});
  assert((await page.locator('#summary').textContent()).includes('2 devices and 1 group'));
  assert(await page.locator('#import').isDisabled());
  assert.equal(calls.find(x=>x.path.endsWith('/preview')).body.location,'b');
  assert(!await page.locator('body').textContent().then(x=>x.includes('synthetic-password')));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  for(const el of await page.locator('input,select,button').all())if(await el.isVisible()){
   const box=await el.boundingBox();assert(box.x>=0&&box.x+box.width<=(mobile?390:1280)+1);
  }
  await page.screenshot({path:path.join(evidence,engine+'-'+(mobile?'mobile':'desktop')+'.png'),fullPage:true});
  await page.getByRole('button',{name:'Cancel import'}).click();
  await page.locator('#loginForm').waitFor({state:'visible'});assert(!calls.some(x=>x.path.endsWith('/apply')));
  await password.fill('synthetic-password');await page.locator('#login').click();await page.locator('#locationStep').waitFor({state:'visible'});
  rejectPreview=true;await page.locator('#preview').click();await page.getByRole('status').filter({hasText:'unsupported records'}).waitFor();
  assert(await page.locator('#review').isHidden());rejectPreview=false;
  await page.locator('#preview').click();await page.locator('#review').waitFor({state:'visible'});
  await page.locator('#importConfirm').check();await page.locator('#import').click();
  await page.getByRole('status').filter({hasText:'Starting your local bridge'}).waitFor();
  const apply=calls.filter(x=>x.path.endsWith('/apply'));assert.equal(apply.length,1);assert.deepEqual(apply[0].body,{session:'s1',preview:'p1',enable_local_control:true});
  assert(await page.locator('#create').isDisabled());assert(await page.locator('#import').isDisabled());
  results.push({name:'cloud-setup-'+(mobile?'mobile':'desktop'),passed:true});await context.close();
 }
 assert.deepEqual(errors,[]);
 fs.writeFileSync(path.join(evidence,'browser.json'),JSON.stringify({engine,results,errors},null,2)+'\n');
 console.log(JSON.stringify({engine,results,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
