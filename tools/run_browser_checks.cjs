/* All invoked harnesses intercept requests; this runner never selects a live URL. */
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('assert'),{spawnSync}=require('child_process');
const root=path.resolve(__dirname,'..'),engine=process.env.BROWSER_ENGINE||'chromium';
assert(['chromium','webkit'].includes(engine),'Choose chromium or webkit');
const evidence=path.resolve(process.env.EVIDENCE_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'halo-browser-')));
fs.mkdirSync(evidence,{recursive:true});const results=[];
const cases=[...Array.from({length:4},(_,n)=>({name:'workflows-'+n,file:'test_redesigned_workflows.cjs',shard:n+'/4'})),
 {name:'backup',file:'test_backup_browser.cjs'},
 {name:'cloud-setup',file:'test_cloud_setup_browser.cjs'},
 {name:'forms',file:'test_form_usability.cjs'},{name:'settings',file:'test_settings_browser.cjs'},
 {name:'scenes',file:'test_scenes_browser.cjs',report:'results.json'},
 {name:'removal',file:'test_removal_browser.cjs',report:'results.json'},
 {name:'pairing-names',file:'test_pairing_names_browser.cjs',report:'results.json'},
 {name:'scene-scrolling',file:'test_scene_scrolling.cjs',report:'results.json'},
 {name:'scene-live-updates',file:'test_scene_live_updates.cjs',report:'results.json'}];
for(const c of cases){
 const dir=path.join(evidence,c.name),env={...process.env,BROWSER_ENGINE:engine,EVIDENCE_DIR:dir};
 delete env.SCENARIO_PATTERN;delete env.SCENARIO_SHARD;if(c.shard)env.SCENARIO_SHARD=c.shard;
 const r=spawnSync(process.execPath,[path.join(root,'tools',c.file)],{cwd:root,env,encoding:'utf8',timeout:180000,maxBuffer:16*1024*1024});
 fs.writeFileSync(path.join(evidence,c.name+'.log'),(r.stdout||'')+(r.stderr||''));
 if(r.status!==0){console.error(c.name,r.error?.message||r.stderr||r.stdout);process.exit(1);}
 const report=JSON.parse(fs.readFileSync(path.join(dir,c.report||(c.name==='forms'?engine+'-results.json':'browser.json')),'utf8'));
 assert(!(report.errors||[]).length&&report.results.every(x=>x.passed),c.name);results.push(...report.results.map(r=>({...r,name:c.report?c.name+'-'+r.name:r.name})));
 console.log(c.name+': '+report.results.length+' passed');
}
assert.equal(new Set(results.map(r=>r.name)).size,results.length,'Duplicate scenarios');
fs.writeFileSync(path.join(evidence,'summary.json'),JSON.stringify({engine,passed:results.length,results},null,2)+'\n');
console.log(JSON.stringify({engine,passed:results.length,evidence}));
