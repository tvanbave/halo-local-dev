"""MASP association state machine port, LGPL-3.0-only; see source provenance.

No BLE I/O here. Strict target/chunk correlation applies to every response.
Short-code association is deliberately unsupported rather than guessed.
"""
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.asymmetric import ec

from .protocol import integer

class AssociationError(ValueError):
    """Safe public protocol failure code; never includes key or packet contents."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


MASK = bytes.fromhex("52202a394018c441aad17d2605d57fae2cb30ef5c52d062415")
MASP_KEY = hashlib.sha256(b"\0MASP").digest()[::-1][:16]
SEQ = b"\x01" + bytes(7)
# Peer response opcode accepted in each waiting state. Shared with the claim
# receive filter so its pre-queue step check cannot diverge from process().
MASP_RESPONSE_OPCODES = {"association": 3, "public_key": 5, "confirm": 7, "random": 9,
                         "device_id": 11, "network_key": 13}


def digest(key, data, length=8):
    return hmac.new(key, data, hashlib.sha256).digest()[::-1][:length]


def xor_mask(data):
    return bytes(b ^ MASK[min(i, 24)] for i, b in enumerate(data))


def envelope(decoded):
    if not 1 <= len(decoded) <= 31:
        raise ValueError("Invalid MASP payload")
    wire = xor_mask(decoded)
    return wire + digest(MASP_KEY, bytes(8) + wire) + b"\x37"


def open_envelope(frame):
    # The final byte is mutable relay TTL, not a MASP protocol tag.
    # Authenticate the payload/MAC exactly as ESP does; do not fix TTL to 55.
    if not 10 <= len(frame) <= 40:
        raise ValueError("Invalid MASP frame")
    wire = frame[:-9]
    if not hmac.compare_digest(digest(MASP_KEY, bytes(8) + wire), frame[-9:-1]):
        raise ValueError("Invalid MASP authentication")
    return xor_mask(wire)


def uuid_hash(uuid_bytes):
    if len(uuid_bytes) != 16:
        raise ValueError("Expected full device UUID")
    return int.from_bytes(hashlib.sha256(uuid_bytes).digest()[-4:], "big") & 0x7fffffff


def discovery_request():
    return envelope(b"\x00" + bytes(5) + SEQ + bytes(4))


class Association:
    def __init__(self, uuid_bytes, device_id, network_key, *, private_key=None, local_random=None):
        integer(device_id, 32896, 65407, "device ID")
        if len(network_key) != 16:
            raise ValueError("Invalid network key")
        self.hash_bytes = uuid_hash(uuid_bytes).to_bytes(4, "little")
        self.device_id = device_id
        self.network_key = network_key
        self.private_key = private_key or ec.generate_private_key(ec.SECP192R1())
        numbers = self.private_key.public_key().public_numbers()
        self.public = numbers.y.to_bytes(24, "little") + numbers.x.to_bytes(24, "little")
        self.random = local_random if local_random is not None else os.urandom(8)
        if len(self.random) != 8:
            raise ValueError("Invalid nonce")
        self.peer_public = bytearray()
        self.peer_confirm = None
        self.shared = None
        # True once a hash-matched DeviceID ack is accepted (None until then).
        self.device_id_ack_hash_matched = None
        self.state = "new"
        self.confirm_type = 0
        self.chunk = 0

    def packet(self, opcode, body):
        return envelope(bytes([opcode]) + self.hash_bytes + body)

    def start(self):
        if self.state != "new":
            raise RuntimeError("Association cannot be restarted")
        self.state = "association"
        return self.packet(2, b"\0" + SEQ + b"\x01")

    def confirm_data(self, public):
        return SEQ + bytes([self.confirm_type, 0]) + public + bytes(8)

    def secret_xor(self, value, label):
        stream = digest(self.shared, label, len(value))
        return bytes(a ^ b for a, b in zip(value, stream))

    def process(self, frame):
        p = open_envelope(frame)
        if self.state in ("new", "complete", "error") or len(p) < 5 or p[1:5] != self.hash_bytes:
            return None
        expected = MASP_RESPONSE_OPCODES[self.state]
        if p[0] != expected:
            return None
        if self.state == "association":
            if len(p) < 6:
                return None
            if p[5] not in (0, 1):
                self.state = "error"
                raise AssociationError("association_response_rejected")
            self.confirm_type = p[5]
            self.state = "public_key"
            return self.packet(4, b"\0" + self.public[:8] + b"\0")
        if self.state == "public_key":
            if len(p) < 14 or p[5] != self.chunk:
                return None
            self.peer_public.extend(p[6:14])
            self.chunk += 1
            if self.chunk < 6:
                return self.packet(4, bytes([self.chunk]) + self.public[self.chunk * 8:self.chunk * 8 + 8] + b"\0")
            try:
                public = ec.EllipticCurvePublicNumbers(int.from_bytes(self.peer_public[24:], "little"),
                    int.from_bytes(self.peer_public[:24], "little"), ec.SECP192R1()).public_key()
                self.shared = self.private_key.exchange(ec.ECDH(), public)[::-1]
            except ValueError:
                self.state = "error"
                raise AssociationError("association_peer_key_invalid") from None
            self.state = "confirm"
            return self.packet(6, digest(self.random, self.confirm_data(self.public)) + b"\0")
        if self.state == "confirm":
            if len(p) < 13:
                return None
            self.peer_confirm = p[5:13]
            self.state = "random"
            return self.packet(8, self.random + b"\0")
        if self.state == "random":
            if len(p) < 13:
                return None
            if not hmac.compare_digest(self.peer_confirm, digest(p[5:13], self.confirm_data(bytes(self.peer_public)))):
                self.state = "error"
                raise AssociationError("association_confirmation_mismatch")
            self.state = "device_id"
            return self.packet(10, self.secret_xor(self.device_id.to_bytes(2, "little"), b"DeviceID") + b"\0")
        if self.state == "device_id":
            # Strict: the ack must carry this target's hash (checked above). The 0.5.0.dev8
            # opcode-only ESP parity was reverted after six live RL4 claims all answered
            # with a matching hash; the flag stays as evidence on each claim.
            if len(p) < 6:
                return None
            self.device_id_ack_hash_matched = True
            self.state = "network_key"
            self.chunk = 0
            return self.packet(12, b"\0" + self.secret_xor(self.network_key, b"NetworkKey")[:8] + b"\0")
        if len(p) < 6 or p[5] != self.chunk:
            return None
        self.chunk += 1
        if self.chunk == 2:
            self.state = "complete"
            self.private_key = self.shared = None
            return None
        return self.packet(12, b"\x01" + self.secret_xor(self.network_key, b"NetworkKey")[8:] + b"\0")
