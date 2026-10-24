"""Read-only status of printers on stock Creality firmware (no Moonraker): the CR-10 SE at 192.168.1.39.

They push their state as JSON over a WebSocket on port 9999 (what Creality Print uses). This module only LISTENS:
it opens the socket, sends nothing but the WebSocket handshake, reads the pushed fields and closes. It never sends a
command. Stdlib only.

The state codes aren't documented by Creality, so reading is deliberately cautious (it guards power switching):
a printer is idle only when its `state` is one seen idle AND no nozzle or bed target is set; anything else, including
a state code we haven't seen, counts as printing.

status(host)   {state, file, progress, nozzle, bed, targets...} or raises
busy(st)       True unless the status is provably idle
hosts()        FARM_CREALITY_HOSTS (comma separated LAN IPs), set in farm.conf
"""
import base64
import json
import os
import socket
import time

PORT = 9999
IDLE_STATES = (0, 2, 3, 4)     # 4 seen idle on the CR-10 SE (2026-10-24); 0 idle / 2 finished / 3 failed on Creality OS


def hosts() -> list[str]:
    return [h.strip() for h in os.environ.get("FARM_CREALITY_HOSTS", "").split(",") if h.strip()]


def status(host: str, timeout: float = 4, port: int = PORT) -> dict:
    """The fields the printer pushes in the first few seconds (it sends its full state right after connecting)."""
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("closed during the handshake")
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("not a WebSocket")

        def take(n):
            nonlocal buf
            while len(buf) < n:
                chunk = s.recv(65536)
                if not chunk:
                    raise EOFError
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out

        fields, end = {}, time.time() + timeout
        try:
            while time.time() < end and not ("state" in fields and "targetNozzleTemp" in fields):
                h = take(2)
                n = h[1] & 127
                if n == 126:
                    n = int.from_bytes(take(2), "big")
                elif n == 127:
                    n = int.from_bytes(take(8), "big")
                if h[1] & 128:          # a server never masks; skip the key if it did
                    take(4)
                data = take(n)
                if h[0] & 15 == 1:      # text frame: JSON
                    try:
                        d = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(d, dict):
                        fields.update(d)
                elif h[0] & 15 == 8:    # close
                    break
        except (socket.timeout, EOFError):
            pass
    finally:
        s.close()
    if "state" not in fields:
        raise ConnectionError("no state in the status feed")
    num = lambda k: float(fields.get(k) or 0)
    return {"state": fields.get("state"), "device_state": fields.get("deviceState"),
            "file": fields.get("printFileName") or "", "progress": fields.get("printProgress"),
            "nozzle": num("nozzleTemp"), "nozzle_target": num("targetNozzleTemp"),
            "bed": num("bedTemp0"), "bed_target": num("targetBedTemp0"), "model": fields.get("model"),
            "hostname": fields.get("hostname")}


def busy(st: dict) -> bool:
    """Printing (or can't be proven idle). Cautious on purpose: unknown codes count as printing."""
    try:
        state = int(st.get("state"))
    except (TypeError, ValueError):
        return True
    return state not in IDLE_STATES or st.get("nozzle_target", 0) > 0 or st.get("bed_target", 0) > 0
