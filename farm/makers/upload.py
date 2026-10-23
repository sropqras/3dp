"""upload: a model file a person already has (an STL/3MF upload, a file imported from the NAS) as a maker job.

No app to call: generate() puts the file in the run as binary STL - one per 3MF build item, read with farm/model3mf.py's
helpers (the viewer's reader). Everything after that is the farm's
usual plan -> slice -> review -> approve, so a hand-made model gets the same gates as a generated one.
"""
import shutil
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import model3mf
from . import FileInput, GenContext, GenResult, MakerError, Maker, MakerSpec, register
from .base import fit_problems, model_out, safe_stem


def _normal(t) -> tuple:
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
    ux, uy, uz, vx, vy, vz = bx - ax, by - ay, bz - az, cx - ax, cy - ay, cz - az
    n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5 or 1.0
    # -0.0 for a zero component: PrusaSlicer (admesh) calls an STL ASCII when the 128 bytes after the header hold no
    # byte > 127, which small positive coordinates + zero normals can do; -0.0 sets the sign bit, a harmless 0x80
    return tuple((c / length) or -0.0 for c in n)


def _write_stl(out: Path, tris: list, note: bytes) -> Path:
    with out.open("wb") as f:
        f.write(note.ljust(80, b" ")[:80] + struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", *_normal(t)) + b"".join(struct.pack("<3f", *v) for v in t) + b"\0\0")
    return out


def read_as_ascii(data: bytes) -> bool:
    """Would PrusaSlicer misread this binary STL as ASCII (no byte > 127 right after the header)?"""
    return not any(b > 127 for b in data[80:80 + 128])


def _rewrite_binary(src: Path, out: Path) -> Path:
    data = src.read_bytes()
    n = struct.unpack_from("<I", data, 80)[0]
    tris = [(struct.unpack_from("<3f", data, 84 + 50 * i + 12), struct.unpack_from("<3f", data, 84 + 50 * i + 24),
             struct.unpack_from("<3f", data, 84 + 50 * i + 36)) for i in range(n)]
    return _write_stl(out, tris, b"farm upload (rewritten for the slicer)")


def stls_from_3mf(src: Path, out_dir: Path, stem: str) -> list[tuple[Path, str]]:
    """One binary STL per build item (a project 3MF often spreads several objects past one bed; the farm's bed
    layout places them again), each in its item's own transform, dropped onto z=0."""
    with zipfile.ZipFile(src) as z:
        root = ET.fromstring(z.read(model3mf._model_path(z)))
        names = model3mf._object_names(z)
    objects = {o.get("id"): o for o in root.iter(f"{model3mf.CORE}object")}

    def tris_of(obj, m, acc):
        mesh = obj.find(f"{model3mf.CORE}mesh")
        if mesh is not None:
            vs = []
            for v in mesh.iter(f"{model3mf.CORE}vertex"):
                x, y, zz = float(v.get("x")), float(v.get("y")), float(v.get("z"))
                vs.append((x * m[0] + y * m[3] + zz * m[6] + m[9], x * m[1] + y * m[4] + zz * m[7] + m[10],
                           x * m[2] + y * m[5] + zz * m[8] + m[11]))
            for t in mesh.iter(f"{model3mf.CORE}triangle"):
                acc.append((vs[int(t.get("v1"))], vs[int(t.get("v2"))], vs[int(t.get("v3"))]))
        comps = obj.find(f"{model3mf.CORE}components")
        for c in (comps if comps is not None else []):
            tris_of(objects[c.get("objectid")], model3mf._compose(model3mf._matrix(c.get("transform")), m), acc)
        return acc

    out = []
    for i, item in enumerate(root.iter(f"{model3mf.CORE}item"), 1):
        oid = item.get("objectid")
        tris = tris_of(objects[oid], model3mf._matrix(item.get("transform")), [])
        if not tris:
            continue
        z0 = min(v[2] for t in tris for v in t)
        tris = [tuple((x, y, zz - z0) for x, y, zz in t) for t in tris]
        label = objects[oid].get("name") or names.get(oid) or f"{stem} {i}"
        out.append((_write_stl(out_dir / f"{stem}_{i:02d}.stl", tris, b"farm upload from 3MF"), label))
    if not out:
        raise MakerError(f"{src.name}: no meshes in this 3MF")
    return out


def stl_from_ascii(src: Path, out: Path) -> Path:
    tris, cur = [], []
    for line in src.read_text(errors="replace").splitlines():
        w = line.split()
        if w[:1] == ["vertex"]:
            cur.append(tuple(float(x) for x in w[1:4]))
            if len(cur) == 3:
                tris.append(cur)
                cur = []
    if not tris:
        raise MakerError(f"{src.name}: not an STL")
    return _write_stl(out, tris, b"farm upload from ASCII STL")


class Upload(Maker):
    spec = MakerSpec(
        id="upload", name="Your own model (STL / 3MF)", url="", mode="sync", timeout_s=120,
        inputs=(FileInput("file", (".stl", ".3mf"), required=True, label="Model: STL or 3MF"),),
        params={},
        typical={"profiles": ["neo-petg-036", "s1pro-petg-020"], "layer_heights": [0.2, 0.28]},
    )

    def health(self) -> bool:
        return True

    def generate(self, ctx: GenContext) -> GenResult:
        src = Path(ctx.files["file"])
        stem = safe_stem(ctx.label or src.stem, "upload")
        out = ctx.out_dir / f"{stem}.stl"
        ext = src.suffix.lower()
        if ext == ".3mf":
            models = [model_out(p, label) for p, label in stls_from_3mf(src, ctx.out_dir, stem)]
        elif ext == ".stl":
            head = src.read_bytes()[:5]
            if head.lower() == b"solid" and b"facet" in src.read_bytes()[:400]:
                stl_from_ascii(src, out)
            elif read_as_ascii(src.read_bytes()):
                _rewrite_binary(src, out)
            else:
                shutil.copyfile(src, out)
        else:
            raise MakerError(f"{src.name}: only STL or 3MF can be printed (STEP/OBJ/SCAD: export an STL first)")
        if ext == ".stl":
            models = [model_out(out, ctx.label or src.stem)]
        warnings = fit_problems(models, ctx)
        for m in models:
            if abs(m.bbox[0][2]) > 0.05:
                warnings.append(f"{m.label}: lowest point z={m.bbox[0][2]:.2f} mm, not on the bed (the slicer drops it)")
        return GenResult(models=models, warnings=warnings, maker_version="upload/1",
                         stats={"source": src.name, "objects": len(models),
                                "triangles": sum(m.triangles for m in models)})


register(Upload())
