/* README screenshots from the real page code with an invented home. Every request is answered
   by this script; nothing reaches a bridge, broker or the internet. PNGs are written only to the
   README image folder: publication/images/ in the development tree, docs/images/ in the public one.
   Re-run whenever the page changes (Chromium; CHROME_EXECUTABLE is optional):
     PLAYWRIGHT_MODULE=... CHROME_EXECUTABLE=... node tools/capture_readme_screenshots.cjs
   SHOT=<regular expression> captures only matching file names. */
const fs=require('fs'),path=require('path'),pw=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const out=fs.existsSync(path.join(root,'publication'))?path.join(root,'publication','images'):path.join(root,'docs','images');
const ORIGIN='http://synthetic.invalid',clone=x=>JSON.parse(JSON.stringify(x));

/* ---------- the invented home ---------- */
// [id, product, name, room, percent (0 = off), kelvin]
const LIGHTS=[
 [40001,93,'Kitchen 1','Kitchen',85,3500],[40002,93,'Kitchen 2','Kitchen',85,3500],
 [40003,93,'Kitchen 3','Kitchen',85,3500],[40004,93,'Kitchen 4','Kitchen',85,3500],
 [40005,137,'Pantry','Kitchen',0,4000],
 [40011,93,'Living room 1','Living room',40,2700],[40012,93,'Living room 2','Living room',40,2700],
 [40013,93,'Living room 3','Living room',40,2700],[40014,134,'Reading lamp','Living room',70,3000],
 [40021,93,'Hallway 1','Hallway',0,3000],[40022,93,'Hallway 2','Hallway',0,3000],
 [40031,134,'Bedside left','Bedroom',25,2700],[40032,134,'Bedside right','Bedroom',0,2700],
 [40033,137,'Bedroom ceiling','Bedroom',0,3000]];
// [id, product, name, room]
const CONTROLLERS=[[40041,127,'Kitchen keypad','Kitchen'],[40042,91,'Hallway dimmer','Hallway']];
// The keypad's four buttons as the bridge reports them after reading the keypad: button 1 switches
// the Kitchen group directly, buttons 2 and 3 are sent to Home Assistant as events, button 4 is unused.
const button=(slot,assignment_mode,target,preset)=>({slot,target,preset,status:assignment_mode==='unassigned'?'unused':'verified_mapping',
 assignment_mode,ha_owned:assignment_mode==='ha',editable:true,draft_editable:true,ha_available:true,ha_draft_available:true});
const KEYPAD_BUTTONS=[button(1,'native',301,255),button(2,'ha',24001,255),button(3,'ha',24002,255),button(4,'unassigned',65535,0)];
const GROUPS=[{group_id:301,name:'Kitchen',members:[40001,40002,40003,40004,40005],exposed:true},
 {group_id:302,name:'Living room',members:[40011,40012,40013,40014],exposed:true},
 {group_id:303,name:'Downstairs',members:[40001,40002,40003,40004,40011,40012,40013,40021,40022],exposed:false},
 {group_id:304,name:'Bedside lamps',members:[40031,40032],exposed:false}];
const raw=p=>Math.round(p*255/100);
const SCENES=[
 {id:'s1',name:'Evening',status:'ready',activation_ready:true,exposed:true,
  members:[40011,40012,40013].map(target=>({target,brightness:raw(45),kelvin:2700})).concat([{target:40014,brightness:raw(60),kelvin:2700}])},
 {id:'s2',name:'Movie night',status:'ready',activation_ready:true,exposed:true,
  members:[40011,40012,40013].map(target=>({target,brightness:raw(10),kelvin:2700})).concat([{target:40014,brightness:0,kelvin:2700}])},
 {id:'s3',name:'Morning kitchen',status:'ready',activation_ready:true,exposed:false,
  members:[40001,40002,40003,40004].map(target=>({target,brightness:raw(100),kelvin:4000}))}];

const reading=(value,at)=>({value,fresh:true,observed_ms:at,age_ms:0});
function snapshot(){
 const at=1000,states={},rooms={};
 for(const [id,,,room,p,k] of LIGHTS){rooms[id]=room;states[id]={state:reading(p?'ON':'OFF',at),brightness:reading(raw(p),at),color_temp:reading(k,at)};}
 for(const [id,,,room] of CONTROLLERS)rooms[id]=room;
 return {status:{mesh_ready:true,mqtt_enabled:true,mqtt_ready:true,preview_only:false,pending_transactions:0,publication_pending:0,jobs:{},
   instance:'synthetic',inventory_revision:'r1',receive_count:18204,bootstrap:{needs_first_claim:false}},
  inventory:{devices:[...LIGHTS.map(([device_id,product_id,name])=>({device_id,product_id,name,exposed:true})),
    ...CONTROLLERS.map(([device_id,product_id,name])=>({device_id,product_id,name,exposed:true}))],groups:clone(GROUPS)},
  device_states:states,device_rooms:rooms,bindings:{},
  group_workflow:{programming_enabled:true,lights:LIGHTS.map(([target,,name])=>({target,name,eligible:true}))},
  controllers:CONTROLLERS.map(([device_id,product_id])=>product_id===127?
    {device_id,product_id,ha_status:'publishing_enabled',assignment_read_back:true,revision:'k1',editor_available:true,slots:clone(KEYPAD_BUTTONS),
     actions:{read_assignments:{enabled:true},edit_assignment:{enabled:true}},last_event:null}:
    {device_id,product_id,ha_status:'publishing_enabled',assignment_read_back:false,slots:[],actions:{read_assignments:{enabled:true}},last_event:null}),
  scene_workflow:scenes()};
}
function scenes(){return {scenes:clone(SCENES),operations:[],capabilities:{supported_models:[93,134,137]},job:null,enabled:true};}
const version=fs.readFileSync(path.join(root,'pyproject.toml'),'utf8').match(/^version = "([^"]+)"/m)[1];
const CAPABILITIES={version,
 features:{control:true,read_states:true,flash:true,labels:true,group_metadata:true,group_edit:true,pair_lights:true,
  pairable_types:[93,134,137,127,126,91,81],keypad_edit:true,scenes:true,mqtt_settings:true,cloud_import:true},
 types:{lights:{93:'RL4 downlight',134:'A19 bulb',137:'Surface downlight'},
  controllers:{127:'Wired scene keypad',126:'Battery scene keypad',91:'Accessory dimmer',81:'Anyplace dimmer'},color_temp:[93,134,137]}};
const MQTT={revision:'m1',enabled:true,host:'mqtt.home.example',port:8883,username:'halo-local',tls:true,password_set:true,connected:true,source:'settings'};

/* The bridge pushes its snapshot over a server-sent event stream. This stand-in opens at once,
   delivers one snapshot and stays open, so the page shows a healthy, connected bridge. */
function fakeEventStream(){
 window.EventSource=class{
  constructor(url){this.url=url;this.readyState=0;this.listeners={};
   setTimeout(async()=>{this.readyState=1;this.onopen&&this.onopen({});
    const data=await (await fetch('/api/snapshot')).text();(this.listeners.snapshot||[]).forEach(f=>f({data}));},0);}
  addEventListener(type,f){(this.listeners[type]=this.listeners[type]||[]).push(f);}
  removeEventListener(){}
  close(){this.readyState=2;}
 };
}

const ASSETS={'/':['ui.html','text/html'],'/ui.js':['ui.js','text/javascript'],'/ui.css':['ui.css','text/css'],
 '/ops.js':['ops.js','text/javascript'],'/settings.js':['settings.js','text/javascript'],'/backup.js':['backup.js','text/javascript']};
async function open(browser,{width,height,scheme}){
 const context=await browser.newContext({viewport:{width,height},deviceScaleFactor:2,isMobile:width<500,hasTouch:width<500,
  colorScheme:scheme,reducedMotion:'reduce',serviceWorkers:'block',locale:'en-GB',timezoneId:'UTC'});
 const page=await context.newPage(),problems=[];page.setDefaultTimeout(8000);
 page.on('pageerror',e=>problems.push('page error: '+e.message));
 page.on('console',m=>{if(['error','warning'].includes(m.type()))problems.push('console '+m.type()+': '+m.text());});
 page.on('dialog',d=>{problems.push('dialog: '+d.message());d.dismiss();});
 await context.addInitScript(fakeEventStream);
 await context.route('**/*',async route=>{
  const req=route.request(),u=new URL(req.url());
  if(u.origin!==ORIGIN){problems.push('blocked request: '+u.origin);return route.abort();}
  if(ASSETS[u.pathname]){const [file,type]=ASSETS[u.pathname];return route.fulfill({contentType:type,body:fs.readFileSync(path.join(root,'avionmesh_pi/web',file),'utf8')});}
  if(u.pathname==='/favicon.ico')return route.fulfill({status:204,body:''});
  const send=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  if(req.method()!=='GET'){problems.push('unexpected write: '+u.pathname);return send({error:'read_only_capture'},409);}
  if(u.pathname==='/api/capabilities')return send(CAPABILITIES);
  if(u.pathname==='/api/snapshot')return send(snapshot());
  if(u.pathname==='/api/scenes')return send(scenes());
  if(u.pathname==='/api/settings/mqtt')return send(MQTT);
  if(u.pathname==='/api/transactions')return send({transactions:[],operations:[]});
  problems.push('unexpected request: '+u.pathname);return send({error:'unexpected_request'},404);
 });
 await page.goto(ORIGIN);
 await page.waitForFunction(()=>S.synced&&S.sse);
 return {context,page,problems};
}
const settle=page=>page.evaluate(()=>new Promise(r=>setTimeout(()=>requestAnimationFrame(()=>requestAnimationFrame(r)),400)));
async function view(page,name){await page.evaluate(v=>go(v),name);await settle(page);}

const SHOTS=[
 {file:'home-desktop-dark.png',width:1280,height:740,scheme:'dark',
  what:'Home: rooms with their lights, room power and brightness',run:async p=>view(p,'home')},
 {file:'home-desktop-light.png',width:1280,height:740,scheme:'light',
  what:'Home in light appearance',run:async p=>view(p,'home')},
 {file:'light-desktop.png',width:1280,height:800,scheme:'dark',what:'One light: power, brightness and colour temperature',
  run:async p=>{await view(p,'home');await p.getByRole('button',{name:'Reading lamp'}).first().click();await p.locator('#drawerRoot .drawer').waitFor();await settle(p);}},
 {file:'scenes-desktop.png',width:1280,height:800,scheme:'dark',what:'Scenes, with the editor open: a preset for each light',
  run:async p=>{await view(p,'scenes');await p.getByRole('button',{name:'Edit / details',exact:true}).first().click();
   await p.locator('#drawerRoot .drawer #sceneName').waitFor();
   // Show the scene's own lights and their presets rather than the unselected ones above them.
   await p.evaluate(()=>{const body=document.querySelector('#drawerRoot .drawer-body'),row=body.querySelector('[data-scene-key="light-40011"]');
    body.scrollTop+=row.getBoundingClientRect().top-body.getBoundingClientRect().top-12;});await settle(p);}},
 {file:'settings-desktop.png',width:1280,height:800,scheme:'dark',what:'Settings: Home Assistant over MQTT',
  run:async p=>{await view(p,'settings');await p.locator('[data-settings-panel=mqtt] summary').click();await p.waitForFunction(()=>document.querySelector('#mqttFields')?.disabled===false);
   await p.getByText('MQTT broker connected',{exact:false}).first().waitFor();await settle(p);}},
 {file:'keypad-desktop.png',width:1280,height:800,scheme:'dark',what:'A wired keypad: each button set to a group, a Home Assistant event, or unused',
  run:async p=>{await view(p,'home');await p.getByRole('button',{name:'Kitchen keypad'}).first().click();
   await p.locator('#drawerRoot .drawer #kpM1').waitFor();await settle(p);}},
 {file:'home-phone.png',width:390,height:844,scheme:'dark',what:'Home on a phone',run:async p=>view(p,'home')},
 {file:'scenes-phone.png',width:390,height:844,scheme:'dark',what:'Scenes on a phone',
  run:async p=>{await view(p,'scenes');await p.getByRole('button',{name:'Activate',exact:true}).first().waitFor();await settle(p);}},
];

(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const only=process.env.SHOT?new RegExp(process.env.SHOT):null;
 const browser=await pw.chromium.launch({headless:true,executablePath:process.env.CHROME_EXECUTABLE,args:['--disable-background-networking']});
 const results=[];
 try{
  for(const shot of SHOTS){
   if(only&&!only.test(shot.file))continue;
   const {context,page,problems}=await open(browser,shot);
   try{
    await shot.run(page);
    const text=await page.locator('body').innerText();
    for(const bad of ['Reconnecting','disconnected','Needs attention','unexpected','Connecting to the bridge'])
     if(text.includes(bad))problems.push('page shows "'+bad+'"');
    if(problems.length)throw new Error(problems.join('\n'));
    const file=path.join(out,shot.file);await page.screenshot({path:file,fullPage:false,animations:'disabled',caret:'hide'});
    results.push({file:path.relative(root,file),bytes:fs.statSync(file).size,shows:shot.what});
   }finally{await context.close();}
  }
 }finally{await browser.close();}
 console.log(JSON.stringify(results,null,1));
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
