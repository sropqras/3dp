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


def parse_klipper(files: dict, main: str = "printer.cfg") -> dict:
    """Klipper's view of a config (enough for farm/audit.py): [include]s, sections merged, `key: value` or
    `key = value`, indented continuation lines, SAVE_CONFIG ignored, numbers as floats."""
    import re as _re
    out = {}

    def load(name, seen):
        if name in seen or name not in files:
            return
        seen.add(name)
        text = files[name].decode() if isinstance(files[name], bytes) else files[name]
        sec, key = None, None
        for line in text.split("\n"):
            if line.startswith("#*#"):
                continue
            m = _re.match(r"^\[([^\]]+)\]", line)
            if m:
                head = m.group(1).strip()
                if head.lower().startswith("include "):
                    load(head.split(" ", 1)[1].strip(), seen)
                    sec = None
                    continue
                sec, key = head.lower(), None
                out.setdefault(sec, {})
                continue
            if sec is None or not line.strip() or line.lstrip().startswith(("#", ";")) and not line[:1].isspace():
                continue
            if line[:1] in (" ", "\t") and key:
                out[sec][key] = (out[sec][key] + "\n" + line.strip()).strip()
                continue
            m = _re.match(r"^([A-Za-z0-9_]+)[ \t]*[:=][ \t]*(.*)$", line)
            if m:
                key = m.group(1).lower()
                val = _re.sub(r"[ \t]+[#;].*$", "", m.group(2)).strip()
                try:
                    out[sec][key] = float(val)
                except ValueError:
                    out[sec][key] = val
    load(main, set())
    return out


class FakeMoonraker:
    """Enough of Moonraker for the gate: status, configfile settings (audit), file metadata, upload, start."""

    def __init__(self, settings: dict):
        import json as _json
        import urllib.parse as up
        self.settings, self.state, self.paused, self.klipper = settings, "standby", False, "ready"
        self.files, self.starts = {}, []
        self.config = {}            # path -> bytes: the printer's config root
        self.settings_from_config = False   # True: configfile settings are parsed from self.config, like Klipper
        self.services = {"klipper": "active/running"}   # name -> "active/sub" (what /machine/system_info reports)
        self.service_restarts = []
        self.service_after = "active/running"   # state a restarted service comes back in
        self.restarts = 0
        self.after_restart = "ready"   # Klipper's state after FIRMWARE_RESTART (tests set "error")
        self.console = []           # gcode_store entries [{"message", "time", "type"}]
        self.on_restart = None      # callable(fake) run at FIRMWARE_RESTART (e.g. change settings)
        self.config_file = "/home/aali/Ender3MaxNeo_data/config/printer.cfg"   # names the instance
        self.webcams = []           # what /server/webcams/item registered
        self.history = []           # /server/history/list entries (paged with start/limit like Moonraker)
        self.meta = {}              # filename -> extra /server/files/metadata fields (thumbnails, slicer, ...)
        self.blobs = {}             # gcodes-root path -> bytes (thumbnails)
        self.klipper_msg = None     # Klipper's state_message when not ready (default: "config error")
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
                        settings = parse_klipper(outer.config) if outer.settings_from_config else outer.settings
                        return self._json(200, {"result": {"status": {"configfile": {"settings": settings}}}})
                    return self._json(200, {"result": {"status": {
                        "print_stats": {"state": outer.state, "filename": "", "print_duration": 0, "message": ""},
                        "virtual_sdcard": {"progress": 0.0, "file_position": 0},
                        "webhooks": {"state": outer.klipper, "state_message": "Printer is ready"
                                     if outer.klipper == "ready" else outer.klipper_msg or "config error"},
                        "pause_resume": {"is_paused": outer.paused},
                        "gcode_move": {"speed_factor": 1.0, "extrude_factor": 1.0},
                        "display_status": {"message": None},
                        "extruder": {"temperature": 25.0, "target": 0.0},
                        "heater_bed": {"temperature": 24.0, "target": 0.0}}}})
                if u.path == "/server/files/list" and q.get("root") == ["config"]:
                    return self._json(200, {"result": [{"path": p, "size": len(b), "modified": 0}
                                                       for p, b in outer.config.items()]})
                if u.path == "/server/files/list" and q.get("root") == ["gcodes"]:
                    return self._json(200, {"result": [{"path": p, "size": n, "modified": 1000.0}
                                                       for p, n in outer.files.items()]})
                if u.path.startswith("/server/files/gcodes/"):
                    p = up.unquote(u.path[len("/server/files/gcodes/"):])
                    if p not in outer.blobs:
                        return self._json(404, {"error": "no file"})
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(outer.blobs[p])
                    return
                if u.path.startswith("/server/files/config/"):
                    p = up.unquote(u.path[len("/server/files/config/"):])
                    if p not in outer.config:
                        return self._json(404, {"error": "no file"})
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(outer.config[p])
                    return
                if u.path == "/printer/info":
                    return self._json(200, {"result": {"state": outer.klipper, "state_message": "Printer is ready"
                                                       if outer.klipper == "ready" else outer.klipper_msg or "config error",
                                                       "software_version": "v0.13.0-fake",
                                                       "config_file": outer.config_file}})
                if u.path == "/server/info":
                    return self._json(200, {"result": {"klippy_state": outer.klipper, "failed_components": [],
                                                       "warnings": [], "moonraker_version": "v0.9.3-fake"}})
                if u.path == "/machine/update/status":
                    return self._json(200, {"result": {"version_info": {"klipper": {"version": "v0.13.0-fake"}}}})
                if u.path == "/machine/system_info":
                    return self._json(200, {"result": {"system_info": {"service_state": {
                        n: {"active_state": st.split("/")[0], "sub_state": st.split("/")[1]}
                        for n, st in outer.services.items()}}}})
                if u.path == "/machine/proc_stats":
                    return self._json(200, {"result": {"cpu_temp": 40.0, "system_memory": {"total": 100, "available": 60}}})
                if u.path == "/server/files/directory":
                    return self._json(200, {"result": {"disk_usage": {"total": 100, "used": 40, "free": 60}}})
                if u.path == "/server/history/totals":
                    return self._json(200, {"result": {"job_totals": {"total_jobs": 3, "total_print_time": 7200}}})
                if u.path == "/server/gcode_store":
                    return self._json(200, {"result": {"gcode_store": outer.console}})
                if u.path == "/server/webcams/list":
                    return self._json(200, {"result": {"webcams": outer.webcams}})
                if u.path == "/server/history/list":
                    start, limit = int(q.get("start", ["0"])[0]), int(q.get("limit", ["50"])[0])
                    since = float(q.get("since", ["0"])[0])
                    js = [h for h in outer.history if h.get("start_time", 0) >= since]
                    return self._json(200, {"result": {"count": len(js[start:start + limit]),
                                                       "jobs": js[start:start + limit]}})
                if u.path == "/server/files/metadata":
                    name = q.get("filename", [""])[0]
                    if name not in outer.files:
                        return self._json(404, {"error": "no file"})
                    return self._json(200, {"result": {"size": outer.files[name], **outer.meta.get(name, {})}})
                return self._json(404, {})

            def do_POST(self):
                u = up.urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if u.path == "/server/files/upload":
                    import re as _re
                    root = _re.search(rb'name="root"\r\n\r\n([^\r]*)', body).group(1).decode()
                    folder = _re.search(rb'name="path"\r\n\r\n([^\r]*)', body).group(1).decode()
                    fname = _re.search(rb'filename="([^"]+)"', body).group(1).decode()
                    data = body.split(b'filename="', 1)[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                    key = f"{folder}/{fname}" if folder else fname
                    if root == "config":
                        outer.config[key] = data
                    else:
                        outer.files[key] = len(data)
                    return self._json(201, {"result": "ok"})
                if u.path == "/machine/services/restart":
                    svc = up.parse_qs(u.query)["service"][0]
                    outer.service_restarts.append(svc)
                    outer.services[svc] = outer.service_after
                    return self._json(200, {"result": "ok"})
                if u.path == "/printer/firmware_restart":
                    outer.restarts += 1
                    if outer.on_restart:
                        outer.on_restart(outer)
                    outer.klipper = outer.after_restart
                    return self._json(200, {"result": "ok"})
                if u.path == "/server/webcams/item":
                    outer.webcams.append(_json.loads(body))
                    return self._json(200, {"result": {"webcam": outer.webcams[-1]}})
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
