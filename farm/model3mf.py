"""Read a run's project .3mf for the Farm Control viewer: the meshes in bed coordinates, plus what the visual gate
checks (colour-change heights and colours, layer heights, bed shape), all taken from the file itself.

Served as one binary blob: uint32 header length, JSON header, padding to 4 bytes, float32 xyz positions,
uint32 triangle indices.
"""
import json
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from array import array
from pathlib import Path

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_IDENTITY = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


def _matrix(attr: str | None) -> tuple:
    return tuple(float(v) for v in attr.split()) if attr else _IDENTITY


def _compose(a: tuple, b: tuple) -> tuple:
    """3MF row-vector transforms: applying a then b."""
    m = [[a[0], a[1], a[2], 0], [a[3], a[4], a[5], 0], [a[6], a[7], a[8], 0], [a[9], a[10], a[11], 1]]
    n = [[b[0], b[1], b[2], 0], [b[3], b[4], b[5], 0], [b[6], b[7], b[8], 0], [b[9], b[10], b[11], 1]]
    r = [[sum(m[i][k] * n[k][j] for k in range(4)) for j in range(3)] for i in range(4)]
    return tuple(v for row in r for v in row)


def _model_path(z: zipfile.ZipFile) -> str:
    try:
        rels = z.read("_rels/.rels").decode()
        m = re.search(r'Target="/?([^"]+\.model)"', rels)
        if m:
            return m.group(1)
    except KeyError:
        pass
    return "3D/3dmodel.model"


def _config(z: zipfile.ZipFile) -> dict:
    try:
        text = z.read("Metadata/Slic3r_PE.config").decode(errors="replace")
    except KeyError:
        return {}
    return dict(re.findall(r"^; (\w+) = (.*)$", text, re.M))


def _object_names(z: zipfile.ZipFile) -> dict:
    """PrusaSlicer keeps object names in its own model config, not in the 3MF model."""
    try:
        root = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))
    except KeyError:
        return {}
    return {o.get("id"): m.get("value") for o in root.iter("object")
            for m in o.findall("metadata") if m.get("type") == "object" and m.get("key") == "name"}


def _colour_changes(z: zipfile.ZipFile) -> list[dict]:
    try:
        root = ET.fromstring(z.read("Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml"))
    except KeyError:
        return []
    return [{"print_z": float(c.get("print_z")), "colour": c.get("color") or "#FFFFFF"}
            for c in root.iter("code") if c.get("type") == "0" or c.get("gcode") == "M600"]


def load(path: Path) -> bytes:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(_model_path(z)))
        cfg = _config(z)
        changes = _colour_changes(z)
        prusa_names = _object_names(z)
    objects = {o.get("id"): o for o in root.iter(f"{CORE}object")}
    pos, idx, names = array("f"), array("I"), []

    def emit(obj, m):
        mesh = obj.find(f"{CORE}mesh")
        if mesh is not None:
            base = len(pos) // 3
            for v in mesh.iter(f"{CORE}vertex"):
                x, y, zz = float(v.get("x")), float(v.get("y")), float(v.get("z"))
                pos.extend((x * m[0] + y * m[3] + zz * m[6] + m[9],
                            x * m[1] + y * m[4] + zz * m[7] + m[10],
                            x * m[2] + y * m[5] + zz * m[8] + m[11]))
            for t in mesh.iter(f"{CORE}triangle"):
                idx.extend((base + int(t.get("v1")), base + int(t.get("v2")), base + int(t.get("v3"))))
        comps = obj.find(f"{CORE}components")
        for c in (comps if comps is not None else []):
            emit(objects[c.get("objectid")], _compose(_matrix(c.get("transform")), m))

    for item in root.iter(f"{CORE}item"):
        obj = objects[item.get("objectid")]
        oid = item.get("objectid")
        names.append(obj.get("name") or prusa_names.get(oid) or f"object {oid}")
        emit(obj, _matrix(item.get("transform")))

    xs, ys, zs = pos[0::3], pos[1::3], pos[2::3]
    bed = [[float(a) for a in p.split("x")] for p in cfg.get("bed_shape", "").split(",") if "x" in p]
    first = cfg.get("extruder_colour", "").split(";")[0].strip('"') or "#2B2B2B"
    header = {
        "file": path.name, "objects": names, "vertices": len(pos) // 3, "triangles": len(idx) // 3,
        "bbox": [[min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]] if len(pos) else None,
        "layer_height": float(cfg.get("layer_height") or 0) or None,
        "first_layer_height": float(cfg.get("first_layer_height") or 0) or None,
        "bed_shape": bed, "first_colour": first, "colour_changes": changes,
    }
    head = json.dumps(header).encode()
    pad = b" " * (-(4 + len(head)) % 4)
    return struct.pack("<I", len(head) + len(pad)) + head + pad + pos.tobytes() + idx.tobytes()
