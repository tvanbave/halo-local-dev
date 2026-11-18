"""Bounded owner/session scoped MCP diagnostics, never MASP/key material."""
import copy
import uuid
from collections import deque
from .protocol import integer, Packet

class Diagnostics:
    def __init__(self, clock):
        self.clock = clock
        self.capture = None
        self.session = 0
        self.tx_attempts = 0
        self.tx_failures = 0
        self.tx_samples = deque(maxlen=8)
        self.tx_overwritten = 0

    def boundary(self):
        self.session += 1
        if self.capture and not self.capture.get('stopped'):
            self.capture['stopped'] = 'session_changed'
            self.capture['ends_ms'] = min(self.clock(),self.capture['ends_ms'])

    def active(self):
        return bool(self.capture and not self.capture.get('stopped') and self.clock()<self.capture['ends_ms'])

    def start(self, target, seconds=30):
        integer(target,32896,65407,'capture source')
        return self._start(target, seconds)

    def start_group(self, group, members, seconds=30):
        """Observe one ordinary group's traffic; this never reads or controls it.

        The API owns inventory/protection checks. Keep an immutable-by-copy member
        snapshot here so later inventory changes cannot broaden a running capture.
        """
        integer(group,256,24575,'capture group')
        if group == 24566:
            raise ValueError('Reserved group cannot be captured as an ordinary group')
        if not isinstance(members, (list, tuple)) or not 1 <= len(members) <= 512:
            raise ValueError('Expected one to 512 capture members')
        for member in members:
            integer(member,32896,65407,'capture member')
        if len(set(members)) != len(members):
            raise ValueError('Duplicate capture member')
        return self._start(group, seconds, scope='group', group=group, members=sorted(members))

    def _start(self, target, seconds, **scope):
        integer(seconds,1,60,'duration')
        if self.capture and (self.active() or not self.capture.get('exported')):
            raise RuntimeError('Previous capture must be stopped and exported or explicitly released')
        self.capture = {'id':uuid.uuid4().hex,'session':self.session,'target':target,
                        'started_ms':self.clock(),'ends_ms':self.clock()+seconds*1000,
                        'records':[],'received':0,'dropped':0,'truncated':0,**scope}
        return copy.deepcopy({k:v for k,v in self.capture.items() if k!='records'})

    def _group_match(self, packet, local_tx):
        """Only documented group controls and exact member GET/report layouts."""
        p, c = packet.payload, self.capture
        if packet.opcode != 115 or len(p) < 2 or p[1] not in (10,29):
            return False
        if packet.mcp_address == 0 and len(p) == 10 and int.from_bytes(p[2:4],'big') == c['group']:
            if p[4] or any(p[8:]):
                return False
            if p[1] == 10 and not any(p[6:]):
                return p[0] == 0 or (not local_tx and p[0] == 17 and p[5] in (13,243))
            return (p[0] == 0 and p[1] == 29 and p[5] == 1
                    and 2700 <= int.from_bytes(p[6:8],'big') <= 5000)
        if local_tx:
            return (packet.mcp_address in c['members'] and len(p) == 10
                    and p[0] == 1 and not any(p[2:]))
        if packet.crypto_source not in c['members'] or packet.mcp_address not in (0,32768,packet.crypto_source):
            return False
        if p[0] not in (1,11):
            return False
        return ((p[1] == 10 and len(p) == 4)
                or (p[1] == 29 and len(p) == 6 and p[3] in (0,1)
                    and 2700 <= int.from_bytes(p[4:6],'big') <= 5000))

    @staticmethod
    def _wire_metadata(packet, wire_length, trailer, local_tx):
        # Diagnostic metadata must never turn a completed send into an error.
        # Omit invalid optional metadata rather than logging arbitrary objects.
        result = {}
        if type(wire_length) is int and wire_length == len(packet.payload) + 17 and 17 <= wire_length <= 40:
            result['wire_length'] = wire_length
        if type(trailer) is int and 0 <= trailer <= 255:
            result.update(transport_trailer=trailer, trailer_authenticated=False,
                          trailer_may_be_forwarded=not local_tx)
        return result

    def _append(self, packet, received, *, local_tx=False, accepted=None,
                wire_length=None, trailer=None, dispatch_started=None, ended=None):
        c = self.capture
        source = packet.mcp_address if local_tx else packet.crypto_source
        if not self.active() or c['session']!=self.session or not c['started_ms']<=received<c['ends_ms']:
            return
        if c.get('scope') == 'group':
            if not self._group_match(packet,local_tx):
                return
        elif source!=c['target']:
            return
        # Scene noun 0x22 is retained as opaque research data, not decoded into
        # commands/events. Source/session/size bounds and group filtering still apply.
        if packet.opcode != 115 or len(packet.payload)<2 or packet.payload[1] not in (3,10,27,28,29,34,48,255):
            return
        c['received'] += 1
        if len(c['records']) >= 256:
            c['dropped'] += 1
            return
        c['truncated'] += int(len(packet.payload)>64)
        record = {'session':c['session'],'capture_index':c['received'],
            'elapsed_ms':received-c['started_ms'],'received_ms':received,'sequence':packet.sequence,
            'sequence_known':True,'source':packet.crypto_source,'address':packet.mcp_address,
            'opcode':packet.opcode,'payload':packet.payload[:64].hex(),'original_length':len(packet.payload),
            'local_tx':local_tx,'authenticated_rx':not local_tx,'transport_accepted':accepted,
            'remote_acknowledged':False}
        record.update(self._wire_metadata(packet,wire_length,trailer,local_tx))
        if local_tx:
            end = ended if type(ended) is int and ended >= received else self.clock()
            record.update(begun_ms=received, ended_ms=end, duration_ms=max(0,end-received),
                          dispatch_started=dispatch_started if type(dispatch_started) is bool else None)
        c['records'].append(record)

    def observe(self, packet, received, *, wire_length=None, trailer=None):
        """Called only after Runtime authenticates MCP; trailer is not authenticated."""
        self._append(packet,received,wire_length=wire_length,trailer=trailer)

    def transmitted(self, plaintext, sequence, begun, accepted, *, session=None, owner=None,
                    wire_length=None, trailer=None, dispatch_started=None, ended=None):
        self.tx_attempts += 1
        self.tx_failures += int(not accepted)
        self.tx_overwritten += int(len(self.tx_samples)==8)
        now=ended if type(ended) is int and ended >= begun else self.clock()
        destination=int.from_bytes(plaintext[:2],'little')
        self.tx_samples.append({'target':destination,'begun_ms':begun,'ended_ms':now,
                                'duration_ms':max(0,now-begun),'transport_accepted':accepted})
        if session!=self.session or owner is None or not self.capture or owner!=self.capture['id']:
            return
        if len(plaintext)>=3:
            self._append(Packet(sequence,0x8000,destination,plaintext[2],plaintext[3:]),begun,
                         local_tx=True,accepted=accepted,wire_length=wire_length,trailer=trailer,
                         dispatch_started=dispatch_started,ended=now)

    def summary(self):
        c=self.capture
        return {'session':self.session,'capture':None if c is None else {
            'active':self.active(),'target':c['target'],'received':c['received'],
            'queued':len(c['records']),'dropped':c['dropped'],'truncated':c['truncated'],
            'stopped':c.get('stopped')},'tx_attempts':self.tx_attempts,'tx_failures':self.tx_failures,
            'tx_overwritten':self.tx_overwritten,'tx_samples':list(self.tx_samples),
            'tx_is_radio_delivery_proof':False}

    def export(self, identifier, stop=False):
        if not self.capture or identifier != self.capture['id']:
            raise ValueError('Capture owner does not match')
        if stop:
            self.capture['stopped']='owner_stopped'
            self.capture['ends_ms']=min(self.capture['ends_ms'],self.clock())
        if not self.active():
            self.capture['exported']=True
        return dict(copy.deepcopy(self.capture),format=3 if self.capture.get('scope') == 'group' else 2,complete=not self.active(),
                    authenticated_rx_only=not any(r['local_tx'] for r in self.capture['records']),mesh_key_included=False,masp_included=False,
                    local_tx_is_delivery_proof=False)

    def release(self, identifier):
        if not self.capture or identifier != self.capture['id']:
            raise ValueError('Capture owner does not match')
        self.capture=None
        return {'released':True}
