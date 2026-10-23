"""Tiny stdlib HTTP helpers (no third-party deps): JSON GET/POST and multipart uploads."""
import json
import uuid
import urllib.error
import urllib.request
from pathlib import Path


def _send(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        raise RuntimeError(f"HTTP {e.code} from {req.full_url}: {body[:300].decode(errors='replace')}") from None


def get_json(url: str, timeout: float = 10):
    _, _, body = _send(urllib.request.Request(url), timeout)
    return json.loads(body)


def post_json(url: str, timeout: float = 10):
    _, _, body = _send(urllib.request.Request(url, data=b"", method="POST"), timeout)
    return json.loads(body)


def post_multipart(url: str, fields: dict, files: dict, timeout: float = 120):
    """files: {field_name: Path}. Returns (status, headers, body bytes)."""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, path in files.items():
        path = Path(path)
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{path.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return _send(req, timeout)


def post_json_body(url: str, obj, timeout: float = 30):
    """POST a JSON body. Returns (status, headers, body bytes)."""
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return _send(req, timeout)


def post_bytes(url: str, data: bytes, content_type: str, timeout: float = 120):
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": content_type})
    return _send(req, timeout)


def get_bytes(url: str, timeout: float = 60):
    return _send(urllib.request.Request(url), timeout)
