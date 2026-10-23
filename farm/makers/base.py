"""Helpers every adapter uses: STL facts, unpacking piece zips, the bed-fit check, multipart fields."""
import io
import re
import struct
import zipfile
from pathlib import Path

from . import GenContext, MakerError, ModelOut


def safe_stem(text: str, default: str = "model") -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("_") or default


def stl_info(path: Path) -> tuple[tuple, int]:
    """(bbox, triangles) of a binary STL; MakerError if it isn't one (an HTML error page saved as .stl...)."""
    data = Path(path).read_bytes()
    if len(data) < 84:
        raise MakerError(f"{Path(path).name}: not an STL ({len(data)} bytes)")
    n = struct.unpack("<I", data[80:84])[0]
    if len(data) != 84 + 50 * n or n == 0:
        raise MakerError(f"{Path(path).name}: not a binary STL (header says {n} triangles, size {len(data)})")
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    for i in range(n):
        v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
        for k in range(9):
            a = k % 3
            lo[a] = min(lo[a], v[k])
            hi[a] = max(hi[a], v[k])
    return (tuple(lo), tuple(hi)), n


def model_out(path: Path, label: str) -> ModelOut:
    bbox, tris = stl_info(path)
    return ModelOut(path=Path(path), label=label, bbox=bbox, triangles=tris)


def unzip_stls(blob: bytes, out_dir: Path) -> list[Path]:
    """The zip's own STLs only (flat names), into out_dir - which callers keep per job, so one job never picks up
    another's pieces from a shared folder."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not name.lower().endswith(".stl"):
                continue
            (out_dir / name).write_bytes(z.read(info))
            got.append(out_dir / name)
    return sorted(got)


def fresh_dir(base: Path) -> Path:
    """base, or base_2, base_3... - the first that doesn't exist yet (jobs of one order share the run folder)."""
    d, n = Path(base), 2
    while d.exists():
        d, n = Path(f"{base}_{n}"), n + 1
    return d


def fresh_file(path: Path, data: bytes) -> Path:
    """Write data to path, or beside it as name_2.stl... when another job already wrote different bytes there."""
    path, n, stem = Path(path), 2, Path(path).stem
    while path.exists() and path.read_bytes() != data:
        path, n = path.with_name(f"{stem}_{n}{path.suffix}"), n + 1
    path.write_bytes(data)
    return path


def fit_problems(models: list[ModelOut], ctx: GenContext) -> list[str]:
    w, d, h = ctx.bed_mm
    out = []
    for m in models:
        (x0, y0, z0), (x1, y1, z1) = m.bbox
        sx, sy, sz = x1 - x0, y1 - y0, z1 - z0
        if not ((sx <= w and sy <= d) or (sy <= w and sx <= d)) or sz > h:
            out.append(f"{m.path.name} is {sx:.0f} x {sy:.0f} x {sz:.0f} mm; bed is {w:.0f} x {d:.0f} x {h:.0f}")
    return out


def form_fields(params: dict) -> dict:
    """Multipart form values the way the Flask makers parse them (booleans as 1/0)."""
    return {k: ("1" if v is True else "0" if v is False else v) for k, v in params.items() if v is not None}


def http_error(e: Exception, what: str) -> MakerError:
    """Map a transport/HTTP failure to retryable (5xx, timeout, refused) or not (4xx = bad request)."""
    msg = str(e)
    m = re.search(r"HTTP (\d{3})", msg)
    retryable = not (m and 400 <= int(m.group(1)) < 500)
    return MakerError(f"{what}: {msg[:300]}", retryable=retryable)


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def nested_params(spec_params: dict, params: dict, defaults: dict | None = None) -> dict:
    """Flat form fields -> the maker's nested JSON: a spec param with "path": "lithophane.topDiameter" lands
    there; params["overrides"] (a JSON object) is merged on top for anything the form doesn't expose."""
    out = deep_merge({}, defaults or {})
    for name, p in spec_params.items():
        if name == "overrides" or name not in params or params[name] is None:
            continue
        node = out
        keys = p.get("path", name).split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = params[name]
    return deep_merge(out, params.get("overrides") or {})


def defaults_from(url: str, key: str | None = None, timeout: float = 10) -> dict:
    """A maker's own /api/defaults (or a key inside it); {} if it can't be read - the spec defaults still apply."""
    from .. import net
    try:
        d = net.get_json(url, timeout=timeout)
        return d.get(key, {}) if key else d
    except Exception:
        return {}


OVERRIDES = {"type": "json", "label": "Advanced: JSON merged into the maker's parameters", "advanced": True}
