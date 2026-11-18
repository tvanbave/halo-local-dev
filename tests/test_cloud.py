import copy
import json
import tempfile
import unittest
from avionmesh_pi.cloud import CloudSync,BASE,parse_inventory,merge_inventory,pid
from avionmesh_pi.protocol import derive_mcp_key
from support import synthetic_store as Store
from avionmesh_pi.runtime import Runtime
from test_service import FakeTransport

DEVICES={'abstract_devices':[{'type':'device','pid':'device_a','avid':40002,'product_id':134,'name':'Synthetic A19'}]}
GROUP={'group':{'avid':291,'name':'Synthetic group','devices':['device_a']}}

class CloudParserTests(unittest.TestCase):
    def test_numeric_pid_and_membership_resolve_without_exposure(self):
        self.assertEqual(pid(2**63),'9223372036854775808')
        result=parse_inventory(DEVICES,[GROUP])
        self.assertEqual(result['groups'][0]['members'],[40002])
        self.assertFalse(result['devices'][0]['exposed'])
    def test_resource_paths_duplicates_and_unresolved_members_refuse(self):
        for value in ('../sessions','a/b',0,True):
            with self.assertRaises(ValueError):pid(value)
        bad=copy.deepcopy(DEVICES);bad['abstract_devices']*=2
        with self.assertRaises(ValueError):parse_inventory(bad,[])
        bad=copy.deepcopy(GROUP);bad['group']['devices']=['missing']
        with self.assertRaises(ValueError):parse_inventory(DEVICES,[bad])
    def test_merge_cannot_change_identity_or_protected_group(self):
        remote=parse_inventory(DEVICES,[GROUP]);local=copy.deepcopy(remote)
        local['groups'][0].update(protected=True,members=[])
        with self.assertRaises(ValueError):merge_inventory(local,remote)
        local=copy.deepcopy(remote);local['devices'][0]['product_id']=91
        with self.assertRaises(ValueError):merge_inventory(local,remote)

class Response:
    def __init__(self,data,status=200):self.data,self.status,self.content=data,status,self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def iter_chunked(self,n):yield json.dumps(self.data).encode()
class FakeCloud:
    def __init__(self,documents):self.documents,self.calls=documents,[]
    def __call__(self,**kwargs):
        assert kwargs['trust_env'] is False
        return self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def request(self,method,url,**kwargs):
        assert kwargs['allow_redirects'] is False
        assert url.startswith(BASE)
        self.calls.append((method,url,kwargs))
        data=self.documents[url[len(BASE):]]
        return data if isinstance(data,Response) else Response(data)

try:
    import aiohttp
except ImportError:
    aiohttp=None

@unittest.skipIf(aiohttp is None,'Cloud HTTP dependency exercised only with fake requests in container')
class CloudSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.key=derive_mcp_key('synthetic-cloud-mesh')
        self.store=Store(self.temp.name,self.key)
        self.transport=FakeTransport()
        self.runtime=Runtime(self.store,self.key,self.transport)
        self.runtime.capabilities['cloud_sync']=True
        self.http=FakeCloud({'sessions':{'credentials':{'auth_token':'synthetic-token'}},
            'user/locations':{'locations':[{'pid':2**63}]},
            'locations/9223372036854775808':{'location':{'passphrase':'synthetic-cloud-mesh'}},
            'locations/9223372036854775808/abstract_devices':DEVICES,
            'locations/9223372036854775808/groups':{'groups':[{'pid':'group_a'}]},'groups/group_a':GROUP})
        self.cloud=CloudSync(self.runtime,self.http)
    async def asyncTearDown(self):
        self.store.close();self.temp.cleanup()
    async def test_exact_mesh_match_merges_metadata_without_radio_or_secret_storage(self):
        result=await self.cloud.sync('test@example.invalid','synthetic-password')
        self.assertTrue(result['merged'])
        self.assertFalse(result['physical_memberships_changed'])
        self.assertEqual(len(self.http.calls),6)
        self.assertEqual(self.transport.frames,[])
        state=' '.join(v for (v,) in self.store.db.execute('SELECT value FROM metadata'))
        for secret in ('synthetic-token','synthetic-password','synthetic-cloud-mesh'):
            self.assertNotIn(secret,state)
    async def test_redirect_and_mesh_mismatch_do_not_merge(self):
        self.http.documents['sessions']=Response({},302)
        with self.assertRaises(ValueError):await self.cloud.sync('test@example.invalid','synthetic')
        self.assertEqual(len(self.http.calls),1)
        self.assertEqual(self.store.inventory()['devices'],[])
    async def test_pending_operation_prevents_authentication_request(self):
        self.store.begin('keypad_edit',40001,{})
        with self.assertRaises(RuntimeError):await self.cloud.sync('test@example.invalid','synthetic')
        self.assertEqual(self.http.calls,[])
    async def test_cumulative_response_budget_stops_before_merge(self):
        for document in self.http.documents.values():
            document['padding']='x'*(1024*1024)
        with self.assertRaises(ValueError):await self.cloud.sync('test@example.invalid','synthetic')
        self.assertLess(len(self.http.calls),6)
        self.assertEqual(self.store.inventory()['devices'],[])
        self.assertEqual(self.transport.frames,[])
