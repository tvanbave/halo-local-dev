/* Imported removal is synthetic: intercept every request, including all confirmation calls. */
const fs=require('fs'),path=require('path'),assert=require('assert'),pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),engine=process.env.BROWSER_ENGINE||'chromium',out=process.env.EVIDENCE_DIR||'/tmp/halo-removal-browser';
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await pw[engine].launch(engine==='chromium'?{headless:true,executablePath:process.env.CHROME_EXECUTABLE,args:['--disable-background-networking']}:{headless:true});
 const results=[];
 for(const [name,width,height] of [['desktop',1280,900],['phone',390,664],['small-phone',320,568]]){
  const context=await browser.newContext({viewport:{width,height},isMobile:width<500,hasTouch:width<500,serviceWorkers:'block'}),page=await context.newPage();page.setDefaultTimeout(7000);
  const calls=[],errors=[],dialogs=[],candidate={uuid:'6655443322121400000a000000000001',product_id:134};let ambiguous=true;
  const receipt={id:'b'.repeat(32),kind:'remove',target:40002,status:'uncertain',detail:{identity_source:'cloud_import',product_id:134},recovery:{actions:['reconcile','confirm_imported_removal'],reason:'imported_removal_owner_confirmation'}};
  const snapshot={status:{mesh_ready:true,pending_transactions:1,mqtt_enabled:false,mqtt_ready:false,instance:'s1',inventory_revision:'r1',jobs:{}},inventory:{devices:[{device_id:40002,product_id:134,name:'Living Room A19',exposed:false}],groups:[]},device_states:{}};
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',async d=>{dialogs.push(d.message());await d.accept();});
  await context.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());if(u.origin!=='http://synthetic.invalid')return route.abort();
   const assets={'/':'ui.html','/ui.js':'ui.js','/ui.css':'ui.css','/ops.js':'ops.js','/settings.js':'settings.js','/backup.js':'backup.js'};
   if(assets[u.pathname])return route.fulfill({contentType:u.pathname.endsWith('.css')?'text/css':u.pathname==='/'?'text/html':'text/javascript',body:fs.readFileSync(path.join(root,'avionmesh_pi/web',assets[u.pathname]),'utf8')});
   const send=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
   if(u.pathname==='/api/capabilities')return send({features:{remove:true,labels:true,control:true},types:{controllers:{},lights:{134:'A19'},color_temp:[134]}});
   if(u.pathname==='/api/snapshot')return send(snapshot);
   if(u.pathname==='/api/events')return route.fulfill({contentType:'text/event-stream',body:''});
   if(u.pathname==='/api/transactions')return send({transactions:[receipt]});
   if(u.pathname==='/api/reconcile'){
    calls.push({path:u.pathname,body:req.postDataJSON()});
    return send({transaction:receipt.id,removed:false,requires_review:true,owner_confirmation_required:true,
     removal_candidates:ambiguous?[candidate,{uuid:'7766554433221400000a000000000001',product_id:134}]:[candidate]});
   }
   if(u.pathname==='/api/unclaim/confirm'){
    calls.push({path:u.pathname,body:req.postDataJSON()});receipt.status='verified';receipt.recovery={actions:[],reason:'terminal_receipt_retained'};
    snapshot.status.pending_transactions=0;snapshot.inventory.devices=[];
    return send({removed:true,verification_mode:'owner_confirmed_unpaired'});
   }
   return send({error:'unexpected synthetic request'},404);
  });
  try{
   await page.goto('http://synthetic.invalid');await page.waitForFunction(()=>S.synced);
   await page.getByRole('navigation',{name:'Bridge sections'}).getByRole('button',{name:'Settings',exact:true}).click();
   await page.getByRole('button',{name:'Review operations',exact:true}).click();
   await page.getByRole('button',{name:'Check what happened',exact:true}).waitFor();
   assert.equal(await page.getByRole('button',{name:'This is the light I saw reset…',exact:true}).count(),0);
   await page.getByRole('button',{name:'Check what happened',exact:true}).click();
   await page.getByText(/More than one newly unpaired light/).waitFor();
   const candidateButtons=page.getByRole('button',{name:'This is the light I saw reset…',exact:true});
   assert.equal(await candidateButtons.count(),2);
   assert(await candidateButtons.nth(0).isDisabled());assert(await candidateButtons.nth(1).isDisabled());
   assert.equal(calls.filter(c=>c.path==='/api/unclaim/confirm').length,0);
   ambiguous=false;await page.getByRole('button',{name:'Check what happened',exact:true}).click();
   await page.waitForFunction(()=>[...document.querySelectorAll('button')].filter(b=>b.textContent==='This is the light I saw reset…').length===1);
   const confirm=page.getByRole('button',{name:'This is the light I saw reset…',exact:true});await confirm.scrollIntoViewIfNeeded();
   assert(await confirm.isEnabled());
   assert(await confirm.evaluate(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}),'Owner confirmation is covered by mobile navigation');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Operations has horizontal overflow');
   await page.screenshot({path:path.join(out,name+'-confirm.png')});await confirm.click();
   await page.waitForFunction(()=>S.status.pending_transactions===0);
   assert.equal(calls.filter(c=>c.path==='/api/unclaim/confirm').length,1);
   assert.deepEqual(calls.find(c=>c.path==='/api/unclaim/confirm').body,{transaction:receipt.id,uuid:candidate.uuid,owner_confirmed:true});
   assert(dialogs.some(s=>s.includes('cannot prove the old and new identities match')));
   assert(!calls.some(c=>c.path==='/api/unclaim'||c.path==='/api/claim'),'Confirmation must not reset or pair');
   assert.equal(errors.length,0,errors.join('\n'));results.push({name,passed:true});
  }catch(e){results.push({name,passed:false,error:e.message});await page.screenshot({path:path.join(out,name+'-failure.png'),fullPage:true}).catch(()=>{});}
  await context.close();
 }
 await browser.close();fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({engine,results},null,2));console.log(JSON.stringify({engine,results}));if(results.some(r=>!r.passed))process.exit(1);
})().catch(e=>{console.error(e);process.exit(1);});
