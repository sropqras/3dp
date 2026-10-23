"""Live smoke test of every maker adapter against the REAL maker apps on this host (no printer is touched).

    FARM_PRUSASLICER=prusa-slicer python3 tests/live_makers.py [maker ...]

Each maker: a tiny order through the real pipeline (generate -> plan -> slice -> review) in a throwaway DB and
run folder - holder on the (unconfirmed) supports profile, likhan with the licensed font if it is installed here.
The manual paths too: an STL upload, a multi-object 3MF upload, and one real file imported from the OMV NAS
(read-only rsync of a single small file). Nothing is uploaded to a printer: every build stops at review.
"""
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farm import config, db, jobs, makers, nas, pipeline, runlog, worker  # noqa: E402
from tests.helpers import box_stl  # noqa: E402
from tests.test_inventory import three_mf  # noqa: E402

# under the repo, not /tmp: the Flathub PrusaSlicer has a private /tmp and would not see the run folder
(Path(__file__).resolve().parent.parent / "data" / "tmp").mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(dir=Path(__file__).resolve().parent.parent / "data" / "tmp", prefix="live_makers_"))
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


def png_cutout(path: Path, w=240, h=240) -> Path:
    """An already cut-out subject (RGBA): an opaque flower-ish blob on a transparent background."""
    import math
    rows = b""
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            dx, dy = x - w / 2, y - h / 2
            r, a = math.hypot(dx, dy), math.atan2(dy, dx)
            inside = r < (w * 0.28) * (0.75 + 0.25 * math.cos(5 * a))
            v = int(120 + 100 * math.cos(r / 6)) if inside else 0
            row += bytes([v, v // 2, v // 3, 255 if inside else 0])
        rows += bytes(row)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
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

CASES["holder"] = dict(items=[{"label": "H1", "params": {"lighter": "maxi", "cutout": True},
                                "inputs": {"image1": str(png_cutout(tmp / "cutout.png"))}}],
                      profile="neo-pla-020-supports", layer_height=0.2, printer="Ender3MaxNeo")
font = config.FONTS_DIR / "CharlesWright-Bold.otf"
if font.is_file():   # licensed: used locally, never served or committed
    CASES["likhan"] = dict(items=[{"label": "Z99", "params": {"mode": "text", "text": "Z99", "font_size_mm": 90,
                                                              "base_thickness_mm": 4, "base_width_mm": 185,
                                                              "base_depth_mm": 103, "monospace": True},
                                   "inputs": {"font": str(font)}}],
                           profile="neo-petg-036", layer_height=0.4, printer="Ender3MaxNeo", colour_changes=["base_top"],
                           colours=["black", "white"], colour_hex=["#111111", "#EEEEEE"])
CASES["upload-stl"] = dict(maker="upload", items=[{"label": "box", "inputs": {"file": str(box_stl(tmp / "b.stl", 0, 0, 0, 30, 20, 10))}}],
                           profile="s1pro-petg-020", layer_height=0.2, printer="s1pro")
CASES["upload-3mf"] = dict(maker="upload", items=[{"label": "two cubes", "inputs": {"file": str(three_mf(tmp / "t.3mf", [(0, 0, 0), (400, 0, 3)]))}}],
                           profile="neo-petg-036", layer_height=0.4, printer="enders1plus")


def nas_case():
    """One small printable file from the real NAS catalogue, imported the way the Import button does."""
    nas.NAS_DIR = tmp / "nas"
    nas.scan(con, log=lambda m: None)
    r = con.execute("SELECT path FROM nas_files WHERE ext='stl' AND gone=0 AND size BETWEEN 20000 AND 400000 "
                    "ORDER BY path LIMIT 1").fetchone()
    got = nas.import_file(con, r[0], "cli:live-test")
    return dict(maker="upload", items=[{"label": got["title"][:40], "inputs": {"file": got["path"]}}],
                profile="neo-hyperpla-028-litho", layer_height=0.28, printer="Ender3MaxNeo")


want = sys.argv[1:] or list(CASES) + ["nas-import"]
for name in want:
    t0 = time.time()
    try:
        case = nas_case() if name == "nas-import" else dict(CASES[name])
    except Exception as e:
        print(f"FAIL {name}: {e}")
        continue
    maker = case.pop("maker", name)
    oid, run = pipeline.submit_order(con, source="ui", title=f"LIVE {name}", maker=maker, **case)
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
