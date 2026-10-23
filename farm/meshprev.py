"""Previews of NAS models without a slicer or any library: read STL/3MF, thin the mesh, and either hand it to Farm
Control's WebGL viewer (the same blob /api/model serves) or draw a small PNG thumbnail.

- STL: binary or ASCII (binary when the size is exactly 84 + 50 n; a binary file may start with "solid").
- 3MF: the core spec plus the production extension Bambu/Orca use (objects in 3D/Objects/*.model, reached by
  components with p:path); build-item transforms applied. A 3MF's own thumbnail (Metadata/thumbnail.png,
  Metadata/plate_1.png, ...) is preferred over drawing one.
- decimate: vertex clustering on a grid until the triangle count fits - crude, stable and fast enough in Python.
- render_png: flat-shaded isometric z-buffer raster, written with zlib + struct. Deterministic: no AI, no GPU.
"""
import json
import math
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from array import array
from pathlib import Path

from .model3mf import CORE, _compose, _matrix

PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
THUMBS = re.compile(r"(?i)^(metadata/(thumbnail|plate_\d+|top_\d+|pick_\d+)\.png|auxiliaries/\.thumbnails/thumbnail_(middle|3mf)\.png)$")


class PreviewError(Exception):
    pass


# ---------------------------------------------------------------- readers

def mesh_from_stl(data: bytes):
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    pos, idx = array("f"), array("I")
    if n >= 0 and len(data) == 84 + 50 * n:
        for i in range(n):
            v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
            pos.extend(v)
        idx.extend(range(n * 3))
        return pos, idx
    text = data.decode("ascii", "replace")
    vs = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    if not vs or len(vs) % 3:
        raise PreviewError("not a readable STL")
    for v in vs:
        pos.extend(float(x) for x in v)
    idx.extend(range(len(vs)))
    return pos, idx


def mesh_from_3mf(path: Path):
    with zipfile.ZipFile(path) as z:
        models = {n: ET.fromstring(z.read(n)) for n in z.namelist() if n.lower().endswith(".model")}
    if not models:
        raise PreviewError("3MF without a model")
    root_name = next((n for n in models if n.lower() == "3d/3dmodel.model"), next(iter(models)))
    objects = {(n, o.get("id")): o for n, r in models.items() for o in r.iter(f"{CORE}object")}
    pos, idx = array("f"), array("I")

    def emit(fname, obj, m, depth=0):
        if depth > 8:
            return
        mesh = obj.find(f"{CORE}mesh")
        if mesh is not None:
            base = len(pos) // 3
            for v in mesh.iter(f"{CORE}vertex"):
                x, y, zz = float(v.get("x")), float(v.get("y")), float(v.get("z"))
                pos.extend((x * m[0] + y * m[3] + zz * m[6] + m[9], x * m[1] + y * m[4] + zz * m[7] + m[10],
                            x * m[2] + y * m[5] + zz * m[8] + m[11]))
            for t in mesh.iter(f"{CORE}triangle"):
                idx.extend((base + int(t.get("v1")), base + int(t.get("v2")), base + int(t.get("v3"))))
        comps = obj.find(f"{CORE}components")
        for c in (comps if comps is not None else []):
            target = (c.get(f"{PROD}path") or "/" + fname).lstrip("/")
            child = objects.get((target, c.get("objectid"))) or objects.get((fname, c.get("objectid")))
            if child is not None:
                emit(target if (target, c.get("objectid")) in objects else fname, child,
                     _compose(_matrix(c.get("transform")), m), depth + 1)

    for item in models[root_name].iter(f"{CORE}item"):
        target = (item.get(f"{PROD}path") or "/" + root_name).lstrip("/")
        obj = objects.get((target, item.get("objectid")))
        if obj is not None:
            emit(target, obj, _matrix(item.get("transform")))
    if not idx:
        raise PreviewError("3MF with no triangles")
    return pos, idx


def embedded_thumb(path: Path) -> bytes | None:
    """The 3MF's own picture (slicers store one), largest first."""
    try:
        with zipfile.ZipFile(path) as z:
            cands = sorted((i for i in z.infolist() if THUMBS.match(i.filename)), key=lambda i: -i.file_size)
            return z.read(cands[0]) if cands else None
    except (zipfile.BadZipFile, OSError):
        return None


def read(path: Path):
    path = Path(path)
    if path.suffix.lower() == ".3mf":
        return mesh_from_3mf(path)
    return mesh_from_stl(path.read_bytes())


# ---------------------------------------------------------------- mesh work

def bbox(pos):
    xs, ys, zs = pos[0::3], pos[1::3], pos[2::3]
    return [[min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]]


def decimate(pos, idx, max_tris: int = 60000):
    """Vertex clustering: snap vertices to a grid, drop collapsed and repeated triangles; coarser grid until the
    count fits. Returns new (pos, idx) with shared vertices."""
    n = len(idx) // 3
    if n <= max_tris and len(pos) // 3 <= max_tris * 3:
        return pos, idx
    (x0, y0, z0), (x1, y1, z1) = bbox(pos)
    span = max(x1 - x0, y1 - y0, z1 - z0) or 1.0
    cells = 512
    while True:
        h = span / cells
        key_of, out_pos, out_idx, seen = {}, array("f"), array("I"), set()
        remap = array("I", bytes(4 * (len(pos) // 3)))
        for i in range(len(pos) // 3):
            k = (int((pos[3 * i] - x0) / h), int((pos[3 * i + 1] - y0) / h), int((pos[3 * i + 2] - z0) / h))
            j = key_of.get(k)
            if j is None:
                j = key_of[k] = len(out_pos) // 3
                out_pos.extend((x0 + (k[0] + .5) * h, y0 + (k[1] + .5) * h, z0 + (k[2] + .5) * h))
            remap[i] = j
        for t in range(n):
            a, b, c = remap[idx[3 * t]], remap[idx[3 * t + 1]], remap[idx[3 * t + 2]]
            if a == b or b == c or a == c:
                continue
            key = (a, b, c) if a < b and a < c else (b, c, a) if b < c else (c, a, b)
            if key in seen:
                continue
            seen.add(key)
            out_idx.extend((a, b, c))
        if len(out_idx) // 3 <= max_tris or cells <= 16:
            return out_pos, out_idx
        cells = int(cells / math.sqrt(len(out_idx) / 3 / max_tris) * 0.9)


def to_blob(pos, idx, name: str) -> bytes:
    """The /api/model format: uint32 header length, JSON header, padding, float32 positions, uint32 indices.
    The model is moved to the middle of the viewer's default 300 mm bed, standing on it (designer coordinates are
    arbitrary)."""
    if len(pos):
        (x0, y0, z0), (x1, y1, z1) = bbox(pos)
        dx, dy, dz = 150 - (x0 + x1) / 2, 150 - (y0 + y1) / 2, -z0
        pos = array("f", (v + (dx, dy, dz)[i % 3] for i, v in enumerate(pos)))
    header = {"file": name, "objects": [name], "vertices": len(pos) // 3, "triangles": len(idx) // 3,
              "bbox": bbox(pos) if len(pos) else None, "layer_height": None, "first_layer_height": None,
              "bed_shape": [], "first_colour": "#E07A2B", "colour_changes": [], "preview": True}
    head = json.dumps(header).encode()
    pad = b" " * (-(4 + len(head)) % 4)
    return struct.pack("<I", len(head) + len(pad)) + head + pad + pos.tobytes() + idx.tobytes()


# ---------------------------------------------------------------- thumbnail raster

def _png(w: int, h: int, rgb: bytearray) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def render_png(pos, idx, size: int = 256, colour=(224, 122, 43), bg=(238, 238, 236)) -> bytes:
    """Isometric view (from the front-right, above), flat-shaded, z-buffered. A flat piece (a plaque, a plate:
    height under a quarter of its footprint) is seen almost from above instead, so its relief reads."""
    pos, idx = decimate(pos, idx, 50000)
    (x0, y0, z0), (x1, y1, z1) = bbox(pos)
    flat = (z1 - z0) < 0.25 * max(x1 - x0, y1 - y0)
    ca, sa = math.cos(math.radians(-45 if not flat else -10)), math.sin(math.radians(-45 if not flat else -10))
    tilt = 55 if not flat else 20
    ct, st = math.cos(math.radians(tilt)), math.sin(math.radians(tilt))
    pts = []
    for i in range(len(pos) // 3):
        x, y, z = pos[3 * i], pos[3 * i + 1], pos[3 * i + 2]
        x, y = x * ca - y * sa, x * sa + y * ca          # spin around Z
        y, z = y * ct - z * st, y * st + z * ct          # tilt towards the viewer
        pts.append((x, z, y))                            # screen x, screen up, depth (bigger = nearer)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if not pts:
        raise PreviewError("empty mesh")
    sx, sy = max(xs) - min(xs) or 1, max(ys) - min(ys) or 1
    scale = (size * 0.86) / max(sx, sy)
    ox, oy = (size - sx * scale) / 2 - min(xs) * scale, (size - sy * scale) / 2 - min(ys) * scale
    scr = [(p[0] * scale + ox, size - (p[1] * scale + oy), p[2]) for p in pts]
    zbuf = [-1e30] * (size * size)
    img = bytearray(bytes(bg) * (size * size))
    light = (0.35, 0.55, 0.76)
    for t in range(len(idx) // 3):
        a, b, c = scr[idx[3 * t]], scr[idx[3 * t + 1]], scr[idx[3 * t + 2]]
        pa, pb, pc = pts[idx[3 * t]], pts[idx[3 * t + 1]], pts[idx[3 * t + 2]]
        ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
        vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
        shade = 0.35 + 0.65 * abs(nx * light[0] + ny * light[1] + nz * light[2]) / ln
        col = bytes(min(255, int(ch * shade)) for ch in colour)
        x_min, x_max = max(0, int(min(a[0], b[0], c[0]))), min(size - 1, int(max(a[0], b[0], c[0])) + 1)
        y_min, y_max = max(0, int(min(a[1], b[1], c[1]))), min(size - 1, int(max(a[1], b[1], c[1])) + 1)
        den = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(den) < 1e-12:
            continue
        for py in range(y_min, y_max + 1):
            for px in range(x_min, x_max + 1):
                w1 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / den
                w2 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / den
                w3 = 1 - w1 - w2
                if w1 < -1e-6 or w2 < -1e-6 or w3 < -1e-6:
                    continue
                z = w1 * a[2] + w2 * b[2] + w3 * c[2]
                k = py * size + px
                if z > zbuf[k]:
                    zbuf[k] = z
                    img[3 * k:3 * k + 3] = col
    return _png(size, size, img)


def build(path: Path, name: str, max_tris: int = 120000) -> dict:
    """{thumb: PNG bytes, thumb_from, blob: viewer blob, triangles, bbox} for one local model file."""
    path = Path(path)
    pos, idx = read(path)
    full_tris = len(idx) // 3
    small = decimate(pos, idx, max_tris)
    emb = embedded_thumb(path) if path.suffix.lower() == ".3mf" else None
    thumb = emb or render_png(*small)
    return {"thumb": thumb, "thumb_from": "embedded" if emb else "render", "blob": to_blob(*small, name),
            "triangles": full_tris, "bbox": bbox(pos)}
