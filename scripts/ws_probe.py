#!/usr/bin/env python3
"""WebSocket 探针 —— 实时打印服务端推来的播报。

    python scripts/ws_probe.py                 # 连 localhost:8000
    python scripts/ws_probe.py 192.168.1.5 8000

配合另一个终端里的 curl 用，用来肉眼确认播报链路通了。
只用标准库，不需要 websockets 包。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys

# ★ Windows 控制台默认 GBK，下面的 ▶ 会直接撑爆 UnicodeEncodeError。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

TIMEOUT = 10


def handshake(host: str, port: int) -> socket.socket:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET /v1/stream HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s = socket.create_connection((host, port), timeout=TIMEOUT)
    s.sendall(req.encode())
    resp = s.recv(4096)
    if b" 101 " not in resp:
        s.close()
        raise ConnectionError(f"握手失败（服务起了吗？）:\n{resp.decode(errors='replace')[:200]}")
    return s


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被关闭")
        buf += chunk
    return buf


def read_text_frame(sock: socket.socket) -> str | None:
    """读一个 WebSocket 文本帧。非文本帧返回 None。"""
    hdr = recv_exact(sock, 2)
    opcode = hdr[0] & 0x0F
    if opcode == 0x8:  # close
        raise ConnectionError("服务端关闭了连接")
    if opcode == 0x9:  # ping -> 回 pong
        sock.sendall(b"\x8a\x80" + os.urandom(4))
        return None

    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(sock, 8))[0]

    mask = recv_exact(sock, 4) if hdr[1] & 0x80 else None
    payload = bytearray(recv_exact(sock, length))
    if mask:
        for i in range(len(payload)):
            payload[i] ^= mask[i % 4]

    return payload.decode("utf-8", "replace") if opcode == 0x1 else None


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000

    try:
        sock = handshake(host, port)
    except (ConnectionError, OSError) as e:
        print(f"连不上 ws://{host}:{port}/v1/stream —— {e}", file=sys.stderr)
        return 1

    print(f"已连接 ws://{host}:{port}/v1/stream （Ctrl-C 退出）\n")
    try:
        while True:
            raw = read_text_frame(sock)
            if raw is None:
                continue
            msg = json.loads(raw)
            if msg["type"] == "announcement":
                d = msg["data"]
                print(f"  ▶ [{d['source']:11s} p{d['priority']}] {d['text']}")
                print(f"      震动={d['haptic']}  ttl={d['ttl_ms']}ms  key={d['dedup_key']}")
            else:
                print(f"  · {msg['type']}: {msg.get('data')}")
    except KeyboardInterrupt:
        print("\n退出")
    except ConnectionError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
