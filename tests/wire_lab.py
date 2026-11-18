"""Isolated MQTT3.1.1 test peer and simulated GATT relay; not a production broker."""
import asyncio
from collections import deque
from avionmesh_pi.bluez import LOW,HIGH
from avionmesh_pi.protocol import decrypt_mcp,encrypt_mcp
from avionmesh_pi.transport import FragmentAssembler


def mqtt_string(value):
    value=value.encode() if isinstance(value,str) else value
    return len(value).to_bytes(2,'big')+value

def packet(header,body=b''):
    n=len(body);encoded=bytearray()
    while True:
        byte=n%128;n//=128;encoded.append(byte|(128 if n else 0))
        if not n:break
    return bytes([header])+encoded+body

def matches(pattern,topic):
    a,b=pattern.split('/'),topic.split('/')
    for i,p in enumerate(a):
        if p=='#':return True
        if i>=len(b) or p not in ('+',b[i]):return False
    return len(a)==len(b)

class WireBroker:
    def __init__(self):
        self.clients=[];self.tasks=set();self.retained={};self.messages=deque(maxlen=4096);self.connects=0
    async def start(self):
        self.server=await asyncio.start_server(self.handle,'127.0.0.1',0)
        self.port=self.server.sockets[0].getsockname()[1]
    async def publish(self,topic,payload,retain=False):
        payload=payload.encode() if isinstance(payload,str) else payload
        self.messages.append((topic,payload,retain))
        if retain:
            if payload:self.retained[topic]=payload
            else:self.retained.pop(topic,None)
        for client in list(self.clients):
            if any(matches(f,topic) for f in client['subscriptions']):
                # MQTT live forwarding clears RETAIN; stored replay below sets it.
                client['writer'].write(packet(0x30,mqtt_string(topic)+payload))
                await client['writer'].drain()
    async def drop(self):
        for client in list(self.clients):client['writer'].transport.abort()
        await asyncio.sleep(.01)
    async def close(self):
        self.server.close();await self.server.wait_closed()
        for client in list(self.clients):client['writer'].transport.abort()
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*list(self.tasks),return_exceptions=True)
    async def handle(self,reader,writer):
        task=asyncio.current_task();self.tasks.add(task)
        client={'writer':writer,'subscriptions':[],'will':None};graceful=False
        try:
            while True:
                head=(await reader.readexactly(1))[0];length=0
                for shift in range(4):
                    value=(await reader.readexactly(1))[0];length+=(value&127)<<(7*shift)
                    if value<128:break
                else:raise ValueError('Oversize MQTT length')
                if length>65536:raise ValueError('Test broker packet bound')
                body=await reader.readexactly(length);kind=head>>4
                def read_string(offset):
                    size=int.from_bytes(body[offset:offset+2],'big')
                    return body[offset+2:offset+2+size],offset+2+size
                if kind==1:
                    protocol,offset=read_string(0)
                    assert protocol==b'MQTT' and body[offset]==4
                    flags=body[offset+1];offset+=4
                    _,offset=read_string(offset)
                    if flags&4:
                        topic,offset=read_string(offset);payload,offset=read_string(offset)
                        client['will']=(topic.decode(),payload,bool(flags&32))
                    self.clients.append(client);self.connects+=1
                    writer.write(b'\x20\x02\x00\x00')
                elif kind==8:
                    offset=2;granted=[];new=[]
                    while offset<len(body):
                        topic,offset=read_string(offset);offset+=1;granted.append(0);new.append(topic.decode())
                    client['subscriptions'].extend(new)
                    writer.write(packet(0x90,body[:2]+bytes(granted)))
                    for topic,payload in self.retained.items():
                        if any(matches(f,topic) for f in new):writer.write(packet(0x31,mqtt_string(topic)+payload))
                elif kind==3:
                    topic,offset=read_string(0);qos=(head>>1)&3
                    identifier=body[offset:offset+2] if qos else b''
                    if qos:offset+=2
                    await self.publish(topic.decode(),body[offset:],bool(head&1))
                    if qos==1:writer.write(packet(0x40,identifier))
                elif kind==12:writer.write(b'\xd0\x00')
                elif kind==14:graceful=True;break
                else:raise ValueError('Unsupported test MQTT packet')
                await writer.drain()
        except (asyncio.IncompleteReadError,ConnectionError):pass
        finally:
            if client in self.clients:self.clients.remove(client)
            if not graceful and client['will']:
                try:await self.publish(*client['will'])
                except ConnectionError:pass
            writer.close()
            try:await writer.wait_closed()
            except ConnectionError:pass
            self.tasks.discard(task)

class Relay:
    def __init__(self,key):
        self.key=key;self.clients=[];self.frames=[];self.brightness=64;self.kelvin=3000;self.sequence=10
        self.fail_high=False;self.duplicates=False
    def client(self,address,disconnected_callback,**kwargs):
        outer=self
        class Client:
            is_connected=False
            def __init__(self):self.services=self;self.notifications={};self.assembly=FragmentAssembler()
            def get_characteristic(self,name):return name
            async def connect(self):self.is_connected=True
            async def start_notify(self,name,callback):self.notifications[name]=callback
            async def disconnect(self):
                self.is_connected=False;disconnected_callback(self)
            async def write_gatt_char(self,name,data,response=False):
                if name==HIGH and outer.fail_high:
                    outer.fail_high=False;raise RuntimeError('Injected HIGH failure')
                frame=self.assembly.feed('low' if name==LOW else 'high',data,outer.sequence)
                if frame is None:return
                outer.frames.append(frame);command=decrypt_mcp(frame,outer.key);p=command.payload
                if command.opcode!=115:raise ValueError('Unexpected simulated operation')
                if p[0]==0 and p[1]==10:outer.brightness=p[5]
                elif p[0]==0 and p[1]==29:outer.kelvin=int.from_bytes(p[6:8],'big')
                elif p[:2]==b'\x01\x0a':outer.emit(command.mcp_address,b'\x01\x0a\0'+bytes([outer.brightness]))
                elif p[:2]==b'\x01\x1d':outer.emit(command.mcp_address,b'\x01\x1d\0\x01'+outer.kelvin.to_bytes(2,'big'))
        client=Client();self.clients.append(client);return client
    async def find_device_by_address(self,address,**kwargs):return object()
    def emit(self,source,payload,repeat=1):
        self.sequence+=1;frame=encrypt_mcp(b'\x00\x80\x73'+payload,self.key,self.sequence,source)
        for _ in range(repeat+(1 if self.duplicates else 0)):
            self.deliver(frame)
        return frame
    def deliver(self,frame,client=None):
        client=client or self.clients[-1]
        if len(frame)>20:client.notifications[LOW](None,frame[:20])
        client.notifications[HIGH](None,frame[20:] if len(frame)>20 else frame)
