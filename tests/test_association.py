import asyncio
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric import ec

from avionmesh_pi.association import Association, SEQ, digest, envelope, open_envelope, uuid_hash
from avionmesh_pi.commissioning import Commissioning
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store

KEY = bytes(range(16))
IDENTITY = bytes(range(16))


class Peer:
    """Independent synthetic peer: real ECDH, confirm and encrypted key checks."""
    def __init__(self, confirm_type=0):
        self.confirm_type = confirm_type
        self.key = ec.derive_private_key(2, ec.SECP192R1())
        numbers = self.key.public_key().public_numbers()
        self.public = numbers.y.to_bytes(24, "little") + numbers.x.to_bytes(24, "little")
        self.host_public = bytearray()
        self.random = b"peerRand"
        self.shared = None
        self.host_confirm = None
        self.netkey = bytearray()
        self.target = None

    def respond(self, frame):
        p = open_envelope(frame)
        header = p[1:5]
        def response(opcode, body):
            return envelope(bytes([opcode]) + header + body)
        if p[0] == 2:
            return response(3, bytes([self.confirm_type]))
        if p[0] == 4:
            index = p[5]
            self.host_public.extend(p[6:14])
            if index == 5:
                host = ec.EllipticCurvePublicNumbers(int.from_bytes(self.host_public[24:], "little"),
                    int.from_bytes(self.host_public[:24], "little"), ec.SECP192R1()).public_key()
                self.shared = self.key.exchange(ec.ECDH(), host)[::-1]
            return response(5, bytes([index]) + self.public[index * 8:index * 8 + 8])
        if p[0] == 6:
            self.host_confirm = p[5:13]
            return response(7, digest(self.random, SEQ + bytes([self.confirm_type, 0]) + self.public + bytes(8)))
        if p[0] == 8:
            assert self.host_confirm == digest(p[5:13], SEQ + bytes([self.confirm_type, 0]) + self.host_public + bytes(8))
            return response(9, self.random)
        if p[0] == 10:
            mask = digest(self.shared, b"DeviceID", 2)
            self.target = int.from_bytes(bytes(a ^ b for a, b in zip(p[5:7], mask)), "little")
            return response(11, b"\0")
        if p[0] == 12:
            index = p[5]
            self.netkey.extend(p[6:14])
            if index == 1:
                mask = digest(self.shared, b"NetworkKey", 16)
                assert bytes(a ^ b for a, b in zip(self.netkey, mask)) == KEY
            return response(13, bytes([index]))
        raise AssertionError("Unexpected request")


class AssociationTests(unittest.TestCase):
    def test_full_type_one_no_short_code_exchange(self):
        protocol = Association(IDENTITY, 40001, KEY)
        peer = Peer(confirm_type=1)
        request = protocol.start()
        count = 0
        while request:
            request = protocol.process(peer.respond(request))
            count += 1
            self.assertLess(count, 20)
        self.assertEqual(protocol.state, 'complete')
        self.assertEqual(protocol.confirm_type, 1)
        self.assertEqual(peer.target, 40001)

    def test_type_one_wrong_confirmation_stops_before_key_distribution(self):
        protocol = Association(IDENTITY, 40001, KEY)
        peer = Peer(confirm_type=1)
        request = protocol.start()
        while open_envelope(request)[0] != 8:
            request = protocol.process(peer.respond(request))
        protocol.peer_confirm = digest(peer.random, SEQ + bytes(2) + peer.public + bytes(8))
        with self.assertRaisesRegex(ValueError, 'association_confirmation_mismatch'):
            protocol.process(peer.respond(request))
        self.assertEqual(protocol.state, 'error')
        self.assertIsNone(peer.target)
        self.assertFalse(peer.netkey)

    def test_rejected_and_foreign_confirmation_types(self):
        for kind in (2, 3, 255):
            protocol = Association(IDENTITY, 40001, KEY)
            protocol.start()
            foreign = bytes([protocol.hash_bytes[0] ^ 1]) + protocol.hash_bytes[1:]
            self.assertIsNone(protocol.process(envelope(b'\x03' + foreign + b'\x01')))
            self.assertEqual(protocol.confirm_type, 0)
            with self.assertRaisesRegex(ValueError, 'association_response_rejected'):
                protocol.process(envelope(b'\x03' + protocol.hash_bytes + bytes([kind])))
            self.assertEqual(protocol.state, 'error')

    def test_known_uuid_hash(self):
        self.assertEqual(uuid_hash(IDENTITY), 0x6e3a8991)

    def test_full_real_crypto_exchange(self):
        protocol = Association(IDENTITY, 40001, KEY, private_key=ec.derive_private_key(3, ec.SECP192R1()), local_random=b"hostRand")
        peer = Peer()
        request = protocol.start()
        count = 0
        while request:
            request = protocol.process(peer.respond(request))
            count += 1
            self.assertLess(count, 20)
        self.assertEqual(protocol.state, "complete")
        self.assertEqual(peer.target, 40001)
        self.assertEqual(count, 12)

    def test_foreign_hash_short_and_out_of_order_replies_do_not_advance(self):
        protocol = Association(IDENTITY, 40001, KEY)
        protocol.start()
        self.assertIsNone(protocol.process(envelope(b"\x03" + bytes(5))))
        self.assertEqual(protocol.state, "association")
        protocol.process(envelope(b"\x03" + protocol.hash_bytes + b"\0"))
        self.assertIsNone(protocol.process(envelope(b"\x05" + protocol.hash_bytes + b"\x01" + bytes(8))))
        self.assertEqual(protocol.chunk, 0)

    def test_ack_chunk_duplicate_cannot_complete(self):
        protocol = Association(IDENTITY, 40001, KEY)
        peer = Peer()
        request = protocol.start()
        while open_envelope(request)[0] != 12:
            request = protocol.process(peer.respond(request))
        first_ack = peer.respond(request)
        second_request = protocol.process(first_ack)
        self.assertIsNone(protocol.process(first_ack))
        self.assertEqual(protocol.state, "network_key")
        protocol.process(peer.respond(second_request))
        self.assertEqual(protocol.state, "complete")

    def test_short_code_and_invalid_mac_rejected(self):
        protocol = Association(IDENTITY, 40001, KEY)
        protocol.start()
        with self.assertRaises(ValueError):
            protocol.process(envelope(b"\x03" + protocol.hash_bytes + b"\x03"))
        frame = bytearray(envelope(b"\x01" + IDENTITY))
        frame[5] ^= 1
        with self.assertRaises(ValueError):
            open_envelope(bytes(frame))


class ClaimTransport:
    ready = True
    dropped = 0
    def __init__(self):
        self.runtime = None
        self.peer = Peer()
        self.sent = 0
        self.fail = False
    async def send(self, frame):
        self.sent += 1
        if self.fail:
            raise RuntimeError("Synthetic transport failure")
        try:
            p = open_envelope(frame)
        except ValueError:
            command = decrypt_mcp(frame, KEY)
            reply = encrypt_mcp(b"\x00\x80\x73\x06\xff"+bytes([command.payload[4]])+b"\x01\0\0\0", KEY, self.sent, command.mcp_address)
            reply = reply[:-1] + bytes([254])
        else:
            reply = envelope(b"\x01" + IDENTITY) if p[0] == 0 else self.peer.respond(frame)
            # Simulate forwarding: TTL changes, authenticated content does not.
            reply = reply[:-1] + bytes([54])
        self.runtime.observe(reply, 10)
    async def disconnect(self):
        self.ready = False


class ClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.transport = ClaimTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = True
        self.claims = Commissioning(self.runtime)
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_claim_publishes_inventory_only_after_crypto_and_mcp_proof(self):
        found = await self.claims.discover(seconds=0)
        self.assertEqual(found[0]["uuid"], IDENTITY.hex())
        result = await self.claims.claim(IDENTITY.hex(), "Synthetic A19", 134, inventory_complete=True)
        self.assertTrue(result["associated"])
        self.assertEqual(self.store.journals()[0]["status"], "verified")
        self.assertEqual(len(self.store.inventory()["devices"]), 1)
        self.assertFalse(self.store.inventory()["devices"][0]["exposed"])
        self.assertEqual(self.store.inventory()["devices"][0]["device_id"], self.transport.peer.target)
        self.assertIn(str(self.transport.peer.target), self.runtime.initial_light_status())

    async def test_type_one_claim_requires_crypto_and_mcp_proof(self):
        self.transport.peer = Peer(confirm_type=1)
        await self.claims.discover(seconds=0)
        result = await self.claims.claim(IDENTITY.hex(), 'Type one', 134, inventory_complete=True)
        self.assertTrue(result['associated'])
        self.assertEqual(self.store.journals()[0]['status'], 'verified')

    async def test_rejected_response_retains_safe_failure_code_and_blocks_retry(self):
        self.transport.peer = Peer(confirm_type=3)
        await self.claims.discover(seconds=0)
        with self.assertRaisesRegex(ValueError, 'association_response_rejected'):
            await self.claims.claim(IDENTITY.hex(), 'Rejected', 134, inventory_complete=True)
        receipt = self.store.journals()[0]
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertEqual(receipt['detail']['failure_code'], 'association_response_rejected')
        self.assertEqual(receipt['detail']['submissions_attempted'], 1)
        self.assertFalse(receipt['detail']['association_complete'])
        self.assertFalse(self.store.inventory()['devices'])
        with self.assertRaises((RuntimeError, ValueError)):
            await self.claims.claim(IDENTITY.hex(), 'Rejected', 134, inventory_complete=True)

    async def test_failure_retains_reservation_and_forbids_retry(self):
        await self.claims.discover(seconds=0)
        self.transport.fail = True
        with self.assertRaises(RuntimeError):
            await self.claims.claim(IDENTITY.hex(), "Synthetic A19", 134, inventory_complete=True)
        self.assertEqual(self.store.inventory()["devices"], [])
        self.assertEqual(self.store.journals()[0]["status"], "uncertain")
        self.assertFalse(self.transport.ready)
        self.runtime.mesh_ready = self.transport.ready = True
        with self.assertRaises(ValueError):
            await self.claims.claim(IDENTITY.hex(), "Retry", 134, inventory_complete=True)

    async def test_incomplete_inventory_refuses_before_radio(self):
        with self.assertRaises(ValueError):
            await self.claims.claim(IDENTITY.hex(), "Synthetic", 134)
        self.assertEqual(self.transport.sent, 0)

    async def test_retired_exact_identity_reclaims_new_address_and_keeps_old_receipts(self):
        from avionmesh_pi.lifecycle import Lifecycle
        import test_lifecycle
        await self.claims.discover(seconds=0)
        first = await self.claims.claim(IDENTITY.hex(), 'First', 134, inventory_complete=True)
        scan = test_lifecycle.Scan()
        scan.results = [{'uuid':IDENTITY.hex(),'uuid_hash':uuid_hash(IDENTITY)}]
        self.runtime.capabilities['removal'] = True
        lifecycle = Lifecycle(self.runtime,scan)
        removal = await lifecycle.remove(first['device_id'])
        self.assertTrue((await lifecycle.reconcile(removal['transaction']))['removed'])
        self.transport.peer = Peer()
        await self.claims.discover(seconds=0)
        second = await self.claims.claim(IDENTITY.hex(), 'Reclaimed', 134, inventory_complete=True)
        self.assertGreater(second['device_id'],first['device_id'])
        self.assertIn(first['device_id'],self.store.get('retired_addresses'))
        claims = [j for j in self.store.journals() if j['kind']=='claim']
        self.assertEqual(len(claims),2)
        self.assertIn(claims[0]['id'],self.store.get('retired_claims'))
        self.assertEqual(claims[1]['status'],'verified')

    async def test_completed_association_checkpoint_recovers_only_with_new_mcp_proof(self):
        from unittest.mock import patch
        await self.claims.discover(seconds=0)
        with patch.object(self.runtime,'exchange',side_effect=asyncio.TimeoutError):
            with self.assertRaises(asyncio.TimeoutError):
                await self.claims.claim(IDENTITY.hex(),'Interrupted',134,inventory_complete=True)
        journal=self.store.journals()[0]
        self.assertTrue(journal['detail']['association_complete'])
        self.assertEqual(journal['status'],'uncertain')
        sent=self.transport.sent
        self.transport.ready=self.runtime.mesh_ready=True
        result=await self.claims.reconcile(journal['id'])
        self.assertTrue(result['verified'])
        self.assertFalse(result['model_verified'])
        self.assertEqual(self.transport.sent,sent+1)  # One encrypted Ping, no association retry.
        self.assertEqual(self.store.journals()[0]['status'],'verified')

    async def test_precheckpoint_uncertain_claim_cannot_be_recovered_by_ping(self):
        await self.claims.discover(seconds=0);self.transport.fail=True
        with self.assertRaises(RuntimeError):
            await self.claims.claim(IDENTITY.hex(),'Interrupted',134,inventory_complete=True)
        journal=self.store.journals()[0];sent=self.transport.sent
        self.transport.fail=False;self.transport.ready=self.runtime.mesh_ready=True
        with self.assertRaises(ValueError):await self.claims.reconcile(journal['id'])
        self.assertEqual(sent,self.transport.sent)

    async def test_cancel_before_dispatch_allows_new_claim_without_reusing_old_address(self):
        from avionmesh_pi.management import cancel_unattempted
        identifier=self.store.begin('claim',40001,{'uuid':IDENTITY.hex(),'uuid_hash':uuid_hash(IDENTITY),
            'association_complete':False,'inventory_complete':True,'product_id':134,'name':'Never sent'})
        cancel_unattempted(self.store,identifier)
        await self.claims.discover(seconds=0)
        result=await self.claims.claim(IDENTITY.hex(),'New attempt',134,inventory_complete=True)
        self.assertEqual(result['device_id'],40002)
        old=self.store.journals()[0]
        self.assertEqual(old['status'],'cancelled')
        self.assertTrue(old['detail']['cancelled_before_dispatch'])

    async def test_full_inventory_refuses_claim_before_radio_or_receipt(self):
        from avionmesh_pi.bridge import Bridge,load_devices
        data={'devices':[{'device_id':40000+i,'product_id':134,'name':'Synthetic','exposed':False} for i in range(512)],'groups':[]}
        self.store.import_inventory(data);self.runtime.core=Bridge(load_devices(data['devices']),KEY)
        self.claims.discovered[IDENTITY.hex()]=10
        with self.assertRaises(ValueError):await self.claims.claim(IDENTITY.hex(),'Overflow',134,inventory_complete=True)
        self.assertEqual(self.transport.sent,0);self.assertEqual(self.store.journals(),[])
