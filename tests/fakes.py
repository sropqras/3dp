"""A fake maker app (HTTP) for offline pipeline tests."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import box_stl


class FakeMaker:
    """Answers POST /generate with an STL box (or the status set in .fail_with), records requests."""

    def __init__(self, tmp):
        self.tmp, self.requests, self.fail_with = tmp, [], None
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((self.path, body))
                if outer.fail_with:
                    self.send_response(outer.fail_with)
                    self.end_headers()
                    self.wfile.write(b"nope")
                    return
                stl = box_stl(outer.tmp / "reply.stl", 0, 0, 0, 185, 103, 10).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "model/stl")
                self.send_header("X-Likhan-Pieces", "1")
                self.end_headers()
                self.wfile.write(stl)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class FakeMoonraker:
    """Enough of Moonraker for the gate: status, configfile settings (audit), file metadata, upload, start."""

    def __init__(self, settings: dict):
        import json as _json
        import urllib.parse as up
        self.settings, self.state, self.paused, self.klipper = settings, "standby", False, "ready"
        self.files, self.starts = {}, []
        self.config = {}            # path -> bytes: the printer's config root
        self.restarts = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def _json(self, code, obj):
                b = _json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                u = up.urlparse(self.path)
                q = up.parse_qs(u.query)
                if u.path == "/printer/objects/query":
                    if "configfile" in u.query:
                        return self._json(200, {"result": {"status": {"configfile": {"settings": outer.settings}}}})
                    return self._json(200, {"result": {"status": {
                        "print_stats": {"state": outer.state, "filename": "", "print_duration": 0, "message": ""},
                        "virtual_sdcard": {"progress": 0.0, "file_position": 0},
                        "webhooks": {"state": outer.klipper, "state_message": "Printer is ready"},
                        "pause_resume": {"is_paused": outer.paused},
                        "gcode_move": {"speed_factor": 1.0, "extrude_factor": 1.0},
                        "display_status": {"message": None},
                        "extruder": {"temperature": 25.0, "target": 0.0},
                        "heater_bed": {"temperature": 24.0, "target": 0.0}}}})
                if u.path == "/server/files/list" and q.get("root") == ["config"]:
                    return self._json(200, {"result": [{"path": p, "size": len(b), "modified": 0}
                                                       for p, b in outer.config.items()]})
                if u.path.startswith("/server/files/config/"):
                    p = up.unquote(u.path[len("/server/files/config/"):])
                    if p not in outer.config:
                        return self._json(404, {"error": "no file"})
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(outer.config[p])
                    return
                if u.path == "/server/webcams/list":
                    return self._json(200, {"result": {"webcams": []}})
                if u.path == "/server/history/list":
                    return self._json(200, {"result": {"jobs": []}})
                if u.path == "/server/files/metadata":
                    name = q.get("filename", [""])[0]
                    if name not in outer.files:
                        return self._json(404, {"error": "no file"})
                    return self._json(200, {"result": {"size": outer.files[name]}})
                return self._json(404, {})

            def do_POST(self):
                u = up.urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if u.path == "/server/files/upload":
                    import re as _re
                    folder = _re.search(rb'name="path"\r\n\r\n([^\r]*)', body).group(1).decode()
                    fname = _re.search(rb'filename="([^"]+)"', body).group(1).decode()
                    data = body.split(b"\r\n\r\n", 3)[-1].rsplit(b"\r\n--", 1)[0]
                    outer.files[f"{folder}/{fname}"] = len(data)
                    return self._json(201, {"result": "ok"})
                if u.path == "/printer/print/start":
                    outer.starts.append(up.parse_qs(u.query)["filename"][0])
                    outer.state = "printing"
                    return self._json(200, {"result": "ok"})
                return self._json(404, {})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class FakeMakers:
    """Every maker app's routes on one server: STL for any build call, ids/jobs for the two-step makers.
    .status forces an HTTP error code on the build calls (4xx vs 5xx handling)."""

    def __init__(self, tmp):
        import json as _json
        self.tmp, self.calls, self.status = tmp, [], None
        outer = self
        stl = box_stl(tmp / "any.stl", 0, 0, 0, 50, 40, 10).read_bytes()

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body, ctype="application/json", headers=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else _json.dumps(body).encode())

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if self.path.endswith("defaults"):
                    return self._send(200, {"defaults": {"lithophane": {"topDiameter": 100, "bottomDiameter": 120}}})
                if self.path.startswith("/api/holders/") and "/files/" in self.path:
                    return self._send(200, stl, "model/stl")
                if self.path.startswith("/api/holders/"):
                    return self._send(200, {"id": "h1", "state": "done", "report": {"watertight": True}})
                return self._send(200, {"ok": True})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.calls.append(("POST", self.path))
                if self.path in ("/api/upload", "/api/images"):
                    return self._send(201, {"id": "u1", "kind": "stl"})
                if outer.status:
                    return self._send(outer.status, {"error": "forced"})
                if self.path == "/api/holders":
                    return self._send(202, {"id": "h1", "state": "queued"})
                return self._send(200, stl, "model/stl", [("X-Namakdani-Stats", "%7B%22watertight%22%3Atrue%7D")])

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
