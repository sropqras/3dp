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
