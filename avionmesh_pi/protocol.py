"""MCP crypto and typed command encoding.

Ported from recsrmesh-cpp and avionmesh-cpp; LGPL-3.0-only, see NOTICE.
All I/O is owned by the explicit service layer, never these encoding functions.
"""
from dataclasses import dataclass
import hashlib
import hmac

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
try:
    from cryptography.hazmat.decrepit.ciphers.modes import OFB
except ImportError:  # cryptography 44 on the development Mac
    from cryptography.hazmat.primitives.ciphers.modes import OFB


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid " + label)
    return value


def derive_mcp_key(passphrase: str) -> bytes:
    if not isinstance(passphrase, str) or not passphrase or "\0" in passphrase:
        raise ValueError("Expected a nonempty passphrase without NUL")
    encoded = passphrase.encode("utf-8")
    if len(encoded) > 252:
        raise ValueError("Passphrase exceeds upstream input limit")
    return hashlib.sha256(encoded + b"\0MCP").digest()[::-1][:16]


@dataclass(frozen=True)
class Packet:
    sequence: int
    crypto_source: int
    mcp_address: int
    opcode: int
    payload: bytes


def decrypt_mcp(frame: bytes, key: bytes) -> Packet:
    if len(key) != 16:
        raise ValueError("MCP key must contain 16 bytes")
    # 14 envelope bytes + 3 MCP header bytes; MTL maximum is 40.
    # The trailer is outside the authenticated envelope; match ESP receive
    # behavior rather than requiring the local transmit default of 0xff.
    if not 17 <= len(frame) <= 40:
        raise ValueError("Invalid MCP frame envelope")
    expected = hmac.new(key, bytes(8) + frame[:-9], hashlib.sha256).digest()[::-1][:8]
    if not hmac.compare_digest(expected, frame[-9:-1]):
        raise ValueError("MCP authentication failed")
    iv = frame[:3] + b"\0" + frame[3:5] + bytes(10)
    decryptor = Cipher(algorithms.AES(key), OFB(iv)).decryptor()
    plaintext = decryptor.update(frame[5:-9]) + decryptor.finalize()
    return Packet(int.from_bytes(frame[:3], "little"),
                  int.from_bytes(frame[3:5], "little"),
                  int.from_bytes(plaintext[:2], "little"), plaintext[2], plaintext[3:])


def encrypt_mcp(plaintext: bytes, key: bytes, sequence: int, source=0x8000) -> bytes:
    """Encode once using a durably allocated sequence; caller owns transport."""
    integer(sequence, 1, 0xffffff, "sequence")
    integer(source, 1, 65535, "source")
    if len(key) != 16 or not 3 <= len(plaintext) <= 26:
        raise ValueError("Invalid MCP transmit input")
    header = sequence.to_bytes(3, "little") + source.to_bytes(2, "little")
    iv = header[:3] + b"\0" + header[3:] + bytes(10)
    encryptor = Cipher(algorithms.AES(key), OFB(iv)).encryptor()
    body = header + encryptor.update(plaintext) + encryptor.finalize()
    mac = hmac.new(key, bytes(8) + body, hashlib.sha256).digest()[::-1][:8]
    return body + mac + b"\xff"


def model_command(target, verb, noun, value=b"\0\0\0"):
    integer(target, 0, 65407, "target")
    integer(verb, 0, 255, "verb")
    integer(noun, 0, 255, "noun")
    if len(value) != 3 or 0x8000 <= target < 0x8080:
        raise ValueError("Invalid command")
    group = target if target < 0x8080 else 0
    destination = 0 if group else target
    return (destination.to_bytes(2, "little") + b"\x73" + bytes([verb, noun])
            + group.to_bytes(2, "big") + b"\0" + value + bytes(2))


def ping_command(target, token):
    integer(token, 1, 255, 'ping token')
    command = bytearray(model_command(target, 6, 255, b'\x01\0\0'))
    command[7] = token
    return bytes(command)


def ping_match(packet, target, token):
    return (packet.crypto_source == target and packet.opcode == 115
            and packet.mcp_address in (0, 32768, target)
            and len(packet.payload) in (7, 10) and packet.payload[:3] == bytes([6,255,token]))


def decode_controller(packet: Packet, authenticated_product=None):
    p = packet.payload
    if packet.opcode != 0x73 or len(p) < 2 or p[0] not in (0, 0x11):
        return None
    result = {"event_type": "mesh_command", "source": packet.crypto_source,
              "sequence": packet.sequence, "opcode": packet.opcode,
              "payload": p.hex()}
    if len(p) >= 4:
        result["target"] = int.from_bytes(p[2:4], "big")
    # Shifted native Accessory layout is qualified only for inventory product 91.
    if (authenticated_product == 91 and len(p) == 10 and p[:2] == b"\x00\x0a"
            and p[4:6] == b"\x01\x00" and not any(p[7:])):
        result.update(brightness=p[6], event_type=
                      "off" if p[6] == 0 else "on" if p[6] == 255 else "brightness")
        return result
    if len(p) != 10 or p[1] != 10 or p[4] or any(p[6:]):
        return result
    if p[0] == 0:
        result.update(brightness=p[5], event_type=
                      "off" if p[5] == 0 else "on" if p[5] == 255 else "brightness")
    elif p[5] in (13, 243):
        result.update(delta=13 if p[5] == 13 else -13,
                      event_type="raise" if p[5] == 13 else "lower")
    return result


def decode_light(packet: Packet):
    """Conservative read/report layouts only; writes never imply observed state."""
    p = packet.payload
    if packet.opcode != 0x73 or len(p) < 4 or p[0] not in (1, 11):
        return None
    if p[1] == 10:
        return {"state": "ON" if p[3] else "OFF", "brightness": p[3]}
    if p[1] == 29 and len(p) >= 6:
        kelvin = int.from_bytes(p[4:6], "big")
        if 2700 <= kelvin <= 5000:
            return {"color_temp": kelvin}
    return None


def plan_light_command(target: int, *, brightness=None, kelvin=None) -> bytes:
    """Return plaintext MCP bytes for review, never send; no broadcast target."""
    integer(target, 1, 65407, "target")
    if 0x8000 <= target < 0x8080:
        raise ValueError("Reserved host address is not a light target")
    if (brightness is None) == (kelvin is None):
        raise ValueError("Choose exactly one light value")
    group = target if target < 0x8080 else 0
    destination = 0 if group else target
    if brightness is not None:
        value = bytes([integer(brightness, 0, 255, "brightness"), 0, 0])
        noun = 10
    else:
        value = b"\x01" + integer(kelvin, 2700, 5000, "kelvin").to_bytes(2, "big")
        noun = 29
    payload = bytes([0, noun]) + group.to_bytes(2, "big") + b"\0" + value + bytes(2)
    return destination.to_bytes(2, "little") + b"\x73" + payload
