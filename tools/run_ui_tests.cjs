/* Synthetic VM checks only. */
const fs=require('fs'),path=require('path'),{spawnSync}=require('child_process');
const root=path.resolve(__dirname,'..');let passed=0;
for(const file of fs.readdirSync(path.join(root,'tests')).filter(f=>f.endsWith('_ui.js')).sort()){
 const r=spawnSync(process.execPath,[path.join(root,'tests',file)],{cwd:root,encoding:'utf8',timeout:30000});
 if(r.status!==0){console.error(file,r.error?.message||r.stderr||r.stdout);process.exit(1);}passed++;
}
console.log(JSON.stringify({javascript_suites:passed,passed:true}));
