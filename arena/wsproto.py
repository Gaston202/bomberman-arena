"""
Minimal WebSocket (RFC 6455) implemented by hand on top of a raw TCP stream.
Browsers cannot open plain TCP/UDP sockets, WebSocket is their only way to keep a TCP
connection open to the server.  The server speaks it directly on its TCP socket (no library).
"""
import base64
import hashlib
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_PAYLOAD = 1 << 16


def accept_key(client_key: str) -> str:
    return base64.b64encode(hashlib.sha1((client_key + GUID).encode()).digest()).decode()


def encode_frame(payload: bytes, opcode=0x1, mask_key=None) -> bytes:
    """Server frames are not masked; pass mask_key (4 bytes) to build a client frame (tests)."""
    n = len(payload)
    head = bytes([0x80 | opcode])
    flag = 0x80 if mask_key else 0
    if n < 126:
        head += bytes([flag | n])
    elif n < 65536:
        head += bytes([flag | 126]) + struct.pack(">H", n)
    else:
        head += bytes([flag | 127]) + struct.pack(">Q", n)
    if mask_key:
        return head + mask_key + bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return head + payload


async def read_frame(reader):
    """-> (fin, opcode, payload).  Raises on EOF / oversized frame."""
    b1, b2 = await reader.readexactly(2)
    fin, opcode = bool(b1 & 0x80), b1 & 0x0F
    masked, n = bool(b2 & 0x80), b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await reader.readexactly(8))[0]
    if n > MAX_PAYLOAD:
        raise ValueError("frame too large")
    mask = await reader.readexactly(4) if masked else None
    data = await reader.readexactly(n) if n else b""
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return fin, opcode, data
