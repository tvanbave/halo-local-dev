/* Synthetic backup/restore UI checks. Every HTTP request is intercepted. */
const fs=require('fs'),path=require('path'),assert=require('assert');
const pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),engine=process.env.BROWSER_ENGINE||'chromium';
const evidence=process.env.EVIDENCE_DIR||'/tmp/halo-backup-browser';
const clone=x=>JSON.parse(JSON.stringify(x));
const password='synthetic-backup-password';
const summary={node:'Synthetic bridge',version:'0.5.0.dev68',created_at:'2026-11-17T18:00:00Z',devices:3,controllers:1,groups:1,scenes:2};
const baseStatus={enabled:true,backup_supported:true,restore_supported:true,current:summary,last_backup:null};
const ready=(mode='same_bridge')=>({preview_id:'review-1',revision:'current-1',expires_in:600,summary,can_restore:true,blockers:[],warnings:[],mode});
const snapshot=()=>({status:{mesh_ready:true,mqtt_enabled:false,mqtt_ready:false,pending_transactions:0,jobs:{},instance:'one',inventory_revision:'i1',bootstrap:{needs_first_claim:false}},inventory:{devices:[{device_id:40002,product_id:134,name:'Example lamp'}],groups:[]},device_states:{},group_workflow:{programming_enabled:true,lights:[{target:40002,name:'Example lamp',eligible:true}]},controllers:[]});
(async()=>{
 fs.mkdirSync(evidence,{recursive:true});const browser=await pw[engine].launch(engine==='chromium'?{headless:true,executablePath:process.env.CHROME_EXECUTABLE,args:['--disable-background-networking']}:{headless:true});const results=[],errors=[];
 async function scenario(name,work,{mobile=false,setup=false}={}){
  const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1280,height:960},isMobile:mobile,hasTouch:mobile,serviceWorkers:'block'}),p=await context.newPage();p.setDefaultTimeout(6000);
  const s={calls:[],status:clone(baseStatus),preview:ready(setup?'replacement':'same_bridge'),handlers:{},downloads:[]};
  p.on('pageerror',e=>errors.push(name+': '+e.message));p.on('download',d=>s.downloads.push(d));
  await context.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());if(u.origin!=='http://synthetic.invalid')return route.abort();
   const assets={'/':setup?'setup.html':'ui.html','/setup.js':'setup.js','/setup.css':'setup.css','/ui.js':'ui.js','/ui.css':'ui.css','/ops.js':'ops.js','/settings.js':'settings.js','/backup.js':'backup.js'};
   if(assets[u.pathname]){const name=assets[u.pathname];return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'avionmesh_pi/web',name),'utf8')});}
   let body;try{body=req.postDataJSON();}catch{}s.calls.push({path:u.pathname,method:req.method(),body,owner:req.headers()['authorization']});
   const send=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
   if(s.handlers[req.method()+' '+u.pathname])return s.handlers[req.method()+' '+u.pathname]({body,send,route});
   if(u.pathname==='/api/capabilities')return send({version:'0.5.0.dev68',features:{backup:true,control:true,labels:true,group_metadata:true,mqtt_settings:true,cloud_import:true},types:{lights:{134:'A19'},controllers:{},color_temp:[134]}});
   if(u.pathname==='/api/snapshot')return send(snapshot());
   if(u.pathname==='/api/events')return route.fulfill({contentType:'text/event-stream',body:''});
   if(u.pathname==='/api/settings/mqtt')return send({revision:'mqtt-1',enabled:false,host:'',port:1883,username:'',tls:false,password_set:false});
   if(u.pathname==='/api/backup/status')return send(s.status);
   if(u.pathname==='/api/backup/create')return send({filename:'synthetic-full.halobackup',archive:Buffer.from('synthetic-encrypted-bytes').toString('base64'),summary});
   if(u.pathname==='/api/backup/preview')return send(s.preview);
   if(u.pathname==='/api/backup/restore')return send({accepted:true,restart_required:true});
   return send({error:'unexpected_request'},404);
  });
  try{
   await p.goto('http://synthetic.invalid');
   if(!setup){await p.waitForFunction(()=>S.synced);await p.getByRole('button',{name:'Settings',exact:true}).click();}
   if(setup) await p.locator('[data-setup-choice="restore"]').click();
   else await p.locator('[data-settings-panel="backup"] > summary').click();
   await p.waitForFunction(()=>document.querySelector('#backupRestoreFields')?.disabled===false);
   await work(p,s);results.push({name,passed:true});
  }catch(error){results.push({name,passed:false,error:error.message});await p.screenshot({path:path.join(evidence,name+'-failure.png'),fullPage:true});}
  finally{await context.close();}
 }
 const upload=async(p)=>{await p.locator('#restoreFile').setInputFiles({name:'example.halobackup',mimeType:'application/octet-stream',buffer:Buffer.from('synthetic encrypted backup')});await p.locator('#restorePassword').fill(password);};
 const preview=async(p)=>{await upload(p);await p.getByRole('button',{name:'Review backup',exact:true}).click();await p.getByRole('heading',{name:'Review this backup'}).waitFor();};
 const confirm=async(p)=>{for(const id of ['backupConfirmStopped','backupConfirmReplace','backupConfirmHistory'])if(await p.locator('#'+id).count())await p.locator('#'+id).check();};
 const posts=s=>s.calls.filter(c=>c.method==='POST');
 await scenario('trusted-lan-optional-backup-password',async(p,s)=>{
  s.status.trusted_lan_backups=true;
  await p.locator('#backupRefresh').click();
  await p.locator('#backupProtect').waitFor();
  assert(await p.locator('#backupPassword').isHidden());
  await p.locator('#backupCreate').click();
  await p.getByText(/Backup downloaded without a password/).waitFor();
  assert.deepEqual(posts(s).at(-1).body,{unencrypted:true});
  await p.locator('#backupProtect').check();
  await p.locator('#backupPassword').fill(password);
  await p.locator('#backupPasswordConfirm').fill(password);
  await p.locator('#backupCreate').click();
  await p.getByText(/Encrypted backup created/).waitFor();
  assert.deepEqual(posts(s).at(-1).body,{password});
  await p.locator('#restoreFile').setInputFiles({name:'copy.halobackup',mimeType:'application/octet-stream',buffer:Buffer.from('HALO-LOCAL-PLAIN\x00\x01synthetic')});
  await p.locator('#backupPreview').click();
  await p.getByText('Review this backup',{exact:true}).waitFor();
  assert.deepEqual(Object.keys(posts(s).at(-1).body),['archive']);
  await p.locator('#backupDismiss').click();
  await upload(p);
  assert(await p.locator('#restorePassword').isVisible());
 },{mobile:true});
 await scenario('backup-owner-required-on-passwordless-bridge',async(p,s)=>{
  s.status.owner_password_required=true;
  await p.getByRole('button',{name:'Refresh backup status'}).click();
  const owner=p.getByLabel('This Pi’s bridge owner password');
  await owner.waitFor();
  await upload(p);
  await p.getByRole('button',{name:'Review backup',exact:true}).click();
  assert.equal(posts(s).length,0);
  await owner.fill('synthetic-owner');
  await preview(p);await confirm(p);
  await p.locator('#backupApply').click();
  await p.getByRole('button',{name:'Reload bridge'}).waitFor();
  const expected='Basic '+Buffer.from('halo:synthetic-owner').toString('base64');
  assert.deepEqual(posts(s).map(c=>c.owner),[expected,expected]);
  assert.equal(await owner.inputValue(),'');
 });
 await scenario('backup-password-validation-and-encrypted-download',async(p,s)=>{
  await p.locator('#backupPassword').fill(password);await p.locator('#backupPasswordConfirm').fill('different-password');await p.getByRole('button',{name:'Download backup'}).click();assert.equal(posts(s).length,0);assert(!(await p.locator('#backupPasswordConfirm').evaluate(e=>e.validity.valid)));
  await p.locator('#backupPasswordConfirm').fill(password);const download=p.waitForEvent('download');await p.getByRole('button',{name:'Download backup'}).click();const file=await download;assert.equal(file.suggestedFilename(),'synthetic-full.halobackup');const data=fs.readFileSync(await file.path());assert.equal(data.toString(),'synthetic-encrypted-bytes');
  await p.getByText(/Encrypted backup created/).waitFor();assert.equal(await p.locator('#backupPassword').inputValue(),'');assert.equal(await p.locator('#backupPasswordConfirm').inputValue(),'');assert.deepEqual(posts(s).map(c=>c.body),[{password}]);
 });
 await scenario('backup-restore-drafts-survive-status-and-clear-on-navigation',async(p,s)=>{
  await upload(p);await p.locator('#restorePassword').focus();await p.evaluate(()=>{window.oldBackupInput=document.querySelector('#restorePassword');window.oldBackupFile=document.querySelector('#restoreFile');for(let i=0;i<3;i++){S.status.receive_count=i;render();}});
  assert(await p.evaluate(()=>window.oldBackupInput===document.querySelector('#restorePassword')&&window.oldBackupFile===document.querySelector('#restoreFile')));assert.equal(await p.locator('#restorePassword').inputValue(),password);assert.equal(await p.locator('#restoreFile').evaluate(e=>e.files.length),1);assert.equal(await p.evaluate(()=>document.activeElement.id),'restorePassword');
  await p.locator('#navItems button').filter({hasText:/^Home/}).click();await p.getByRole('button',{name:'Settings',exact:true}).click();assert.equal(await p.locator('#restorePassword').inputValue(),'');assert.equal(await p.locator('#restoreFile').evaluate(e=>e.files.length),0);assert.equal(posts(s).length,0);
 });
 await scenario('backup-same-bridge-review-before-single-apply',async(p,s)=>{
  await preview(p);assert(await p.getByText('Synthetic bridge',{exact:true}).isVisible());assert.equal(await p.locator('#restorePassword').inputValue(),'');assert.equal(await p.locator('#restoreFile').evaluate(e=>e.files.length),0);assert.equal(posts(s).length,1);assert.equal(await p.locator('#backupConfirmStopped').count(),0);assert(await p.locator('#backupApply').isDisabled());
  await p.locator('#backupConfirmReplace').check();assert(await p.locator('#backupApply').isDisabled());await p.locator('#backupConfirmHistory').check();await p.locator('#backupApply').click();await p.getByText(/Restore accepted/).waitFor();await p.evaluate(()=>render());assert.equal(posts(s).filter(c=>c.path==='/api/backup/restore').length,1);assert.deepEqual(posts(s).find(c=>c.path==='/api/backup/restore').body,{preview_id:'review-1',revision:'current-1',confirm_old_bridge_stopped:true,confirm_replace:true,confirm_backup_history:true});assert(await p.locator('#restorePassword').isDisabled());
 });
 await scenario('backup-replacement-first-run-mobile-confirmations',async(p,s)=>{
  assert.equal(await p.locator('#backupCreateForm').count(),0);await preview(p);await p.locator('#backupConfirmReplace').check();await p.locator('#backupConfirmHistory').check();assert(await p.locator('#backupApply').isDisabled());await p.locator('#backupConfirmStopped').check();assert(await p.locator('#backupApply').isEnabled());
  assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await p.locator('#backupApply').scrollIntoViewIfNeeded();const rect=await p.locator('#backupApply').boundingBox();assert(rect.y>=0&&rect.y+rect.height<=844);await p.screenshot({path:path.join(evidence,'replacement-phone-review.png'),fullPage:true});
  await p.locator('#backupApply').click();await p.getByText(/Restore accepted/).waitFor();assert(await p.locator('#create').isDisabled());assert(await p.locator('#login').isDisabled());assert(await p.locator('#confirm').isDisabled());assert.equal(posts(s).filter(c=>c.path==='/api/backup/restore').length,1);
 },{setup:true,mobile:true});
 await scenario('backup-preview-blockers-and-escaping',async(p,s)=>{
  s.preview={...ready(),can_restore:false,preview_id:null,blockers:['Newer device history exists. Nothing will be overwritten.'],warnings:['<img src=x onerror=alert(1)>'],summary:{...summary,node:'<script>window.injected=true</script>'}};
  await preview(p);assert.equal(await p.locator('#backupApply').count(),0);assert(await p.getByText('Newer device history exists. Nothing will be overwritten.',{exact:true}).isVisible());assert.equal(await p.locator('#backupReview img,#backupReview script').count(),0);assert.equal(await p.evaluate(()=>window.injected),undefined);assert.equal(posts(s).length,1);await p.getByRole('button',{name:'Discard review'}).click();assert.equal(await p.locator('#backupReview').textContent(),'');
 });
 await scenario('backup-expired-preview-blocks-restore',async(p,s)=>{
  s.preview.expires_in=0.15;await preview(p);await p.getByText(/This backup review expired/).waitFor();await confirm(p);assert(await p.locator('#backupApply').isDisabled());assert.equal(posts(s).filter(c=>c.path==='/api/backup/restore').length,0);
 });
 await scenario('backup-wrong-password-never-starts-restore',async(p,s)=>{
  s.handlers['POST /api/backup/preview']=({send})=>send({error:'invalid_password_or_backup',message:'The password is incorrect or the backup is damaged.'},400);await upload(p);await p.getByRole('button',{name:'Review backup'}).click();await p.getByText('The password is incorrect or the backup is damaged.',{exact:true}).waitFor();assert.equal(await p.locator('#restorePassword').inputValue(),'');assert.equal(await p.locator('#backupApply').count(),0);assert.equal(posts(s).length,1);
 });
 await scenario('backup-lost-restore-response-never-replays',async(p,s)=>{
  s.handlers['POST /api/backup/restore']=({route})=>route.abort('failed');await preview(p);await confirm(p);await p.locator('#backupApply').click();await p.getByText(/restore response was not confirmed/).waitFor();await p.evaluate(()=>render());assert.equal(await p.locator('#backupApply').count(),0);assert(await p.locator('#restorePassword').isDisabled());assert.equal(posts(s).filter(c=>c.path==='/api/backup/restore').length,1);assert(await p.getByRole('button',{name:'Reload bridge'}).isVisible());
 });
 await scenario('backup-preview-busy-locks-other-first-run-choices',async(p,s)=>{
  let release;const gate=new Promise(resolve=>release=resolve);s.handlers['POST /api/backup/preview']=async({send})=>{await gate;return send(s.preview);};await upload(p);await p.getByRole('button',{name:'Review backup'}).click();await p.getByText(/Opening and checking/).waitFor();assert(await p.locator('#login').isDisabled());assert(await p.locator('#confirm').isDisabled());release();await p.getByRole('heading',{name:'Review this backup'}).waitFor();assert(await p.locator('#login').isEnabled());
 },{setup:true});
 await scenario('backup-settings-phone-scroll-and-form-retention',async(p,s)=>{
  await p.locator('#backupPassword').fill(password);await p.locator('#backupPasswordConfirm').fill(password);
  await p.evaluate(()=>{window.retained=document.querySelector('#backupPasswordConfirm');render();});assert(await p.evaluate(()=>window.retained===document.querySelector('#backupPasswordConfirm')));assert.equal(await p.locator('#backupPasswordConfirm').inputValue(),password);
  await preview(p);await confirm(p);await p.locator('#backupApply').scrollIntoViewIfNeeded();assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  const box=await p.locator('#backupApply').boundingBox(),nav=await p.locator('#nav').boundingBox();assert(box.y>=0&&box.y+box.height<=nav.y+1);await p.screenshot({path:path.join(evidence,'settings-phone-review.png'),fullPage:true});assert.equal(posts(s).filter(c=>c.path==='/api/backup/restore').length,0);
 },{mobile:true});
 await scenario('backup-first-run-lost-response-locks-setup',async(p,s)=>{
  s.handlers['POST /api/backup/restore']=({route})=>route.abort('failed');await preview(p);await confirm(p);await p.locator('#backupApply').click();await p.getByText(/restore response was not confirmed/).waitFor();assert(await p.locator('#confirm').isDisabled());assert(await p.locator('#login').isDisabled());assert(await p.locator('#restorePassword').isDisabled());assert.equal(posts(s).filter(c=>c.path==='/api/backup/restore').length,1);
 },{setup:true});
 await scenario('backup-disabled-status-and-invalid-file',async(p,s)=>{
  await p.locator('#restoreFile').setInputFiles({name:'empty.halobackup',mimeType:'application/octet-stream',buffer:Buffer.alloc(0)});await p.locator('#restorePassword').fill(password);await p.getByRole('button',{name:'Review backup'}).click();await p.getByText(/empty or too large/).waitFor();assert.equal(posts(s).length,0);
  s.status={...s.status,enabled:false,reason:'Backups are unavailable in this preview.'};await p.getByRole('button',{name:'Refresh backup status'}).click();await p.getByText(s.status.reason,{exact:true}).waitFor();assert(await p.locator('#backupPassword').isDisabled());assert(await p.locator('#restorePassword').isDisabled());
 });
 await browser.close();fs.writeFileSync(path.join(evidence,'browser.json'),JSON.stringify({engine,results,errors},null,2)+'\n');console.log(JSON.stringify({passed:results.filter(r=>r.passed).length,total:results.length,errors}));if(results.some(r=>!r.passed)||errors.length){console.log(JSON.stringify(results.filter(r=>!r.passed),null,2));process.exitCode=1;}
})().catch(e=>{console.error(e);process.exitCode=1;});
