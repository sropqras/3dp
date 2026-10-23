"""Live smoke test of every maker adapter against the REAL maker apps on this host (no printer is touched).

    FARM_PRUSASLICER=prusa-slicer python3 tests/live_makers.py [maker ...]

Each maker: a tiny order through the real pipeline (generate -> plan -> slice -> review) in a throwaway DB and
run folder. holder needs a supports profile that doesn't exist yet, so only its adapter's generate() runs.
"""
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farm import db, jobs, makers, pipeline, runlog, worker  # noqa: E402
from tests.helpers import box_stl  # noqa: E402

tmp = Path(tempfile.mkdtemp())
runlog.RUNS_DIR = tmp / "runs"
con = db.connect(tmp / "farm.db")


def png(path: Path, w=160, h=120) -> Path:
    """A grey gradient with a dark disc: enough contrast for a lithophane/holder to be non-trivial."""
    rows = b""
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            v = int(40 + 180 * x / w)
            if (x - w / 2) ** 2 + (y - h / 2) ** 2 < (h / 4) ** 2:
                v = 20
            row += bytes([v, v, v])
        rows += bytes(row)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


photo = png(tmp / "photo.png")
CASES = {
    "lithophane": dict(items=[{"label": "L1", "params": {"width_mm": 80, "height_mm": 60, "resolution_mm": 0.5},
                               "inputs": {"image": str(photo)}}],
                       profile="neo-hyperpla-028-litho", layer_height=0.28, printer="enders1plus"),
    "lamp": dict(items=[{"label": "Lamp1", "params": {"top_diameter": 90, "bottom_diameter": 110, "shade_height": 90,
                                                      "resolution": 0.6},
                         "inputs": {"fileToUpload1": str(photo)}}],
                 profile="neo-hyperpla-028-litho", layer_height=0.28, printer="Ender3MaxNeo"),
    "plaque": dict(items=[{"label": "PQ1", "params": {"text": "AZ", "font_size_mm": 40, "base_thickness_mm": 2}}],
                   profile="neo-petg-036", layer_height=0.4, printer="Ender3MaxNeo", colour_changes=["base_top"],
                   colours=["black", "white"], colour_hex=["#111111", "#EEEEEE"]),
    "namakdani": dict(items=[{"label": "ND1", "params": {"widthMm": 60},
                              "inputs": {"file": str(box_stl(tmp / "shape.stl", 0, 0, 0, 40, 30, 10))}}],
                      profile="neo-hyperpla-028-litho", layer_height=0.28, printer="Ender3MaxNeo"),
}

want = sys.argv[1:] or list(CASES) + ["holder"]
for name in want:
    t0 = time.time()
    if name == "holder":
        m = makers.get("holder")
        ctx = makers.GenContext(params={"lighter": "maxi"}, files={"image1": photo}, bed_mm=(290, 290, 300),
                                nozzle=0.6, layer_height=0.2, out_dir=tmp, log=lambda x: None, label="H1")
        try:
            res = m.generate(ctx)
            print(f"OK   holder (generate only): {res.models[0].path.name} {res.models[0].triangles} triangles, "
                  f"bbox {[[round(v) for v in c] for c in res.models[0].bbox]}, {time.time() - t0:.0f}s, "
                  f"warnings {res.warnings[:2]}")
        except makers.MakerError as e:
            print(f"FAIL holder: {e}")
        continue
    case = dict(CASES[name])
    oid, run = pipeline.submit_order(con, source="ui", title=f"LIVE {name}", maker=name, **case)
    for (jid,) in con.execute("SELECT id FROM jobs WHERE order_id=?", (oid,)).fetchall():
        worker.enqueue(con, "generate", jid)
    while worker.run_once(con, log=lambda x: None):
        pass
    bs = [b for b in jobs.builds(con) if b["order_id"] == oid]
    failed = con.execute("SELECT kind, last_error FROM tasks WHERE state='failed' AND ref_id IN "
                         "(SELECT id FROM jobs WHERE order_id=?)", (oid,)).fetchall()
    ok = bs and all(b["state"] == "review" for b in bs)
    detail = ", ".join(f"build {b['id']} {b['state']} {b['grams']} g {round((b['estimate_s'] or 0) / 60)} min"
                       + (f" ERROR {b['error'][:150]}" if b["error"] else "") for b in bs)
    print(f"{'OK  ' if ok else 'FAIL'} {name}: {len(bs)} build(s) in {time.time() - t0:.0f}s: {detail} "
          f"{[tuple(f) for f in failed] or ''}")
print(f"(throwaway run folders under {tmp})")
