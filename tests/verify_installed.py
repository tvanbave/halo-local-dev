"""Explicit production-image check; does not start the bridge or open sockets."""
import base64
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import pkgutil
import subprocess
import avionmesh_pi

root=Path(avionmesh_pi.__file__).resolve()
assert '/site-packages/' in str(root),root
assert not Path('/app/avionmesh_pi').exists(),'Source checkout shadows installed wheel'
assert not Path('/wheels').exists(),'Build wheel cache was retained in runtime'
assert os.getuid()==65532
assert importlib.metadata.version('avionmesh-pi')==avionmesh_pi.__version__=='0.5.0.dev84'
modules=[]
for item in pkgutil.iter_modules(avionmesh_pi.__path__):
    importlib.import_module('avionmesh_pi.'+item.name)
    modules.append(item.name)
verified=[]
dist=importlib.metadata.distribution('avionmesh-pi')
for entry in dist.files:
    if str(entry).startswith('avionmesh_pi/') and str(entry).endswith('.py'):
        assert entry.hash and entry.hash.mode=='sha256'
        encoded=base64.urlsafe_b64encode(hashlib.sha256(entry.locate().read_bytes()).digest()).decode().rstrip('=')
        assert encoded==entry.hash.value,str(entry)
        verified.append(str(entry))
refusal=subprocess.run(['avionmesh-pi','serve','/nonexistent-unconfigured-file'],capture_output=True,text=True)
assert refusal.returncode==2 and '--allow-live' in refusal.stderr
print(json.dumps({'version':avionmesh_pi.__version__,'installed_path':str(root),'uid':os.getuid(),
                  'all_modules_imported':modules,'wheel_record_hashes_verified':len(verified),
                  'source_shadow_absent':True,'live_start_without_opt_in_refused':True},indent=2))
