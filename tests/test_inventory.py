"""Inventory (artefacts + prints from the printers, read-only), the OMV NAS catalogue, the upload maker, and
switched-off printers (a note, not an alarm; Reconnect only from that state)."""
import copy
import dataclasses
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from farm import cfgchange, db, fleet, hosts, inventory, jobs, makers, monitor, nas, printers
from tests.fakes import FakeMoonraker
from tests.helpers import box_stl
from tests.test_audit_model3mf import GOOD

H = lambda fn, st, t, **kw: {"filename": fn, "status": st, "start_time": t, "end_time": t + 3600,
                             "print_duration": 3500, "filament_used": 1234.0, **kw}


class Names(unittest.TestCase):
    def test_slicer_and_farm_names_meet_the_nas_name(self):
        k = inventory.design_key
        self.assertEqual(k("Al-Quddus_180mm_12h16m_0.28mm_220C_PLA_ENDER3S1PLUS.gcode"), k("Allah/Al-Quddus_180mm.3mf"))
        self.assertEqual(k("funnel-v3(2)_2h0m_0.24mm_220C_PLA_ENDER3S1PRO.gcode"), "funnel v3")
        self.assertEqual(k("organizer-with-bow-ready_50m_0.20mm_220C_PLA_.gcode"), "organizer with bow ready")
        self.assertEqual(inventory.title_of("farm/qabrastaan/CH02+A11_0.4mm_petg_Ender3MaxNeo.gcode"), "CH02+A11")
        self.assertEqual(inventory.labels_in("CH02+A11"), ["CH02", "A11"])
        self.assertEqual(inventory.labels_in("Pointer"), [])


class Backfill(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(tempfile.mkdtemp()) / "farm.db")
        self.m = FakeMoonraker(copy.deepcopy(GOOD))
        self.p = dataclasses.replace(printers.PRINTERS["s1pro"], moonraker=self.m.url)
        self.m.files = {"Al-Quddus_180mm_12h16m_0.28mm_220C_PLA_ENDER3S1PRO.gcode": 10,
                        "Al-Quddus_180mm_9h0m_0.20mm_220C_PLA_ENDER3S1PRO.gcode": 10,
                        "farm/run/A01_0.4mm_petg_s1pro.gcode": 10, "notes.txt": 3}
        self.m.meta = {"Al-Quddus_180mm_12h16m_0.28mm_220C_PLA_ENDER3S1PRO.gcode": {
            "slicer": "PrusaSlicer", "layer_height": 0.28, "estimated_time": 44160,
            "thumbnails": [{"width": 16, "height": 16, "relative_path": ".thumbs/a-16.png"},
                           {"width": 300, "height": 300, "relative_path": ".thumbs/a-300.png"}]}}
        self.m.blobs = {".thumbs/a-300.png": b"\x89PNG big"}
        self.m.history = ([H("Al-Quddus_180mm_12h16m_0.28mm_220C_PLA_ENDER3S1PRO.gcode", "completed", 1000 + i)
                           for i in range(150)] +                                # more than one page
                          [H("old_deleted_thing_1h0m_0.20mm_220C_PLA_ENDER3S1PRO.gcode", "error", 5000, exists=False),
                           H("farm/run/A01_0.4mm_petg_s1pro.gcode", "cancelled", 6000)])
        # a farm build uploaded farm/run/A01...: its print belongs to the job's artefact, not a loose G-code
        oid = jobs.create_order(self.con, "cli", "A01 order")
        self.jid = jobs.create_job(self.con, oid, "likhan", "A01", state="generated")
        self.con.execute("INSERT INTO models (job_id, path, kind, label) VALUES (?, '/x/A01.stl', 'stl', 'A01')", (self.jid,))
        jobs.create_build(self.con, oid, "s1pro", [self.jid], remote_path="farm/run/A01_0.4mm_petg_s1pro.gcode",
                          state="queued")

    def tearDown(self):
        self.m.close()

    def test_files_history_and_builds_land_once(self):
        r = inventory.backfill_printer(self.con, self.p, log=lambda m: None)
        self.assertEqual((r["files"], r["history"]), (4, 152))
        again = inventory.backfill_printer(self.con, self.p, log=lambda m: None)            # idempotent
        self.assertEqual(again["new_files"], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM prints").fetchone()[0], 152)
        ds = {d["design"]: d for d in inventory.designs(self.con)}
        q = ds["al quddus 180mm"]
        self.assertEqual(len(q["artefacts"]), 2)                                            # two re-slices, one design
        self.assertEqual(q["printers"]["s1pro"]["completed"], 150)
        self.assertTrue(ds["old deleted thing"]["artefacts"][0]["deleted"])
        a01 = self.con.execute("SELECT * FROM artefacts WHERE job_id=?", (self.jid,)).fetchone()
        self.assertEqual(self.con.execute("SELECT status, source FROM prints WHERE artefact_id=?", (a01["id"],)).fetchone()[:],
                         ("cancelled", "tracker"))
        self.assertIsNone(self.con.execute("SELECT 1 FROM artefacts WHERE remote_path LIKE 'farm/run/%'").fetchone())

    def test_thumbnail_is_the_largest(self):
        inventory.backfill_printer(self.con, self.p, log=lambda m: None)
        aid = self.con.execute("SELECT id FROM artefacts WHERE meta LIKE '%a-300%'").fetchone()[0]
        with mock.patch.dict(printers.PRINTERS, {"s1pro": self.p}):
            self.assertEqual(inventory.thumb(self.con, aid)[0], b"\x89PNG big")

    def test_marks_are_a_persons_and_survive_a_backfill(self):
        inventory.backfill_printer(self.con, self.p, log=lambda m: None)
        pid = self.con.execute("SELECT id FROM prints WHERE status='completed' LIMIT 1").fetchone()[0]
        with self.assertRaises(inventory.Refused):
            inventory.mark(self.con, pid, "good", "agent")
        inventory.mark(self.con, pid, "bad", "ui:Amir", "stringing")
        self.m.history[0]["end_time"] = 9999
        inventory.backfill_printer(self.con, self.p, log=lambda m: None)
        self.assertEqual(self.con.execute("SELECT quality FROM prints WHERE id=?", (pid,)).fetchone()[0], "bad")
        q = {d["design"]: d for d in inventory.designs(self.con)}["al quddus 180mm"]
        self.assertEqual((q["printers"]["s1pro"]["bad"], q["ok"]), (1, 149))

    def test_listing_groups(self):
        inventory.backfill_printer(self.con, self.p, log=lambda m: None)
        by_p = inventory.listing(self.con, "printer")
        self.assertEqual(by_p["groups"][0]["key"], "s1pro")
        by_m = {g["key"] for g in inventory.listing(self.con, "maker")["groups"]}
        self.assertEqual(by_m, {"printer-file", "likhan"})
        self.assertEqual(inventory.listing(self.con, "printer", printed_only=True)["total"], 1)
        self.assertEqual(inventory.listing(self.con, "printer", q="quddus")["total"], 1)


class Portfolio(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.con = db.connect(self.tmp / "farm.db")
        self.aid = self.con.execute(
            "INSERT INTO artefacts (kind, title, design, maker, printer, remote_path, meta, created_at) "
            "VALUES ('gcode','Lamp','lamp','printer-file','s1pro','lamp.gcode',?,0)", ('{"layer_height": 0.2}',)).lastrowid
        inventory.record(self.con, self.aid, "s1pro", H("lamp.gcode", "completed", 10), "history")
        p = mock.patch.object(inventory, "PRODUCTS_DIR", self.tmp / "products")
        p.start()
        self.addCleanup(p.stop)

    def test_promote_is_a_persons_and_prefills_what_it_was_proven_on(self):
        with self.assertRaises(inventory.Refused):
            inventory.promote(self.con, self.aid, {"name": "Lamp"}, "agent")
        with self.assertRaises(ValueError):
            inventory.promote(self.con, self.aid, {"name": " "}, "ui:Amir")
        pr = inventory.promote(self.con, self.aid, {"name": "Moon lamp", "description": "PLA"}, "ui:Amir",
                               (b"\xff\xd8" + b"x" * 200, ".JPG"))
        self.assertEqual((pr["printer"], pr["layer_height"], pr["photo"]), ("s1pro", 0.2, f"{pr['id']}.jpg"))
        self.assertEqual(inventory.product_photo(self.con, pr["id"])[1], "image/jpeg")
        again = inventory.promote(self.con, self.aid, {"name": "Moon lamp v2"}, "ui:Amir")   # update, photo kept
        self.assertEqual((again["id"], again["photo"]), (pr["id"], pr["photo"]))
        with self.assertRaises(ValueError):
            inventory.promote(self.con, self.aid, {"name": "x"}, "ui:Amir", (b"<html>" * 50, "html"))
        self.assertEqual(inventory.listing(self.con, "maker", products_only=True)["total"], 1)
        inventory.unpromote(self.con, pr["id"], "ui:Amir")
        self.assertFalse((self.tmp / "products" / pr["photo"]).exists())
        self.assertEqual(inventory.listing(self.con, "maker", products_only=True)["total"], 0)

    def test_photos_near_the_model_on_the_nas(self):
        nas.scan(self.con, log=lambda m: None, listing=(
            "-rw 10 2025/06/01 09:24:58 Lights/Lamp.stl\n-rw 10 2025/06/01 09:24:58 Lights/lamp photo.jpg\n"
            "-rw 10 2025/06/01 09:24:58 Other/cat.png\n"))
        self.con.execute("INSERT INTO nas_files (path, size, mtime, ext, folder, design, seen_at) "
                         "VALUES ('@ProductImages/IMG_1.JPG', 1, 1, 'jpg', '', '', 1)")
        ph = nas.photos_for(self.con, "lamp")
        self.assertEqual([(x["path"], x["near"]) for x in ph], [("Lights/lamp photo.jpg", True), ("@ProductImages/IMG_1.JPG", False)])
        self.assertEqual(nas.matrix(self.con)["total"], 1)               # pictures aren't catalogue rows
        self.assertEqual(nas._url("@ProductImages/a [1].jpg"), nas.PHOTOS_URL + "a \\[1\\].jpg")


LISTING = """drwxrwxrwx          4,096 2026/10/11 21:47:44 .
-rwxrwxrwx      4,741,782 2025/06/01 09:24:58 Allah/Al-Quddus_180mm.3mf
-rwxrwxrwx         12,000 2025/06/01 09:24:58 Allah/Al-Quddus_180mm.step
-rwxrwxrwx            684 2025/06/01 09:24:58 Allah/box [v2].stl
-rwxrwxrwx            500 2025/06/01 09:24:58 Allah/readme.txt
-rwxrwxrwx            500 2025/06/01 09:24:58 .recycle/old.stl
drwxrwxrwx          4,096 2025/06/01 09:24:58 Allah
"""


class Nas(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.con = db.connect(self.tmp / "farm.db")
        self.con.execute("INSERT INTO artefacts (kind, title, design, maker, printer, remote_path, meta, created_at) "
                         "VALUES ('gcode','Al-Quddus_180mm','al quddus 180mm','printer-file','s1pro','x.gcode','{}',0)")
        inventory.record(self.con, 1, "s1pro", H("x.gcode", "completed", 1), "history")

    def test_scan_matches_and_marks_gone(self):
        r = nas.scan(self.con, log=lambda m: None, listing=LISTING)
        self.assertEqual(r["files"], 3)                                   # no .txt, no .recycle, no dirs
        m = nas.matrix(self.con)
        rows = {x["path"]: x for x in m["rows"]}
        self.assertEqual(rows["Allah/Al-Quddus_180mm.3mf"]["printers"]["s1pro"]["completed"], 1)
        self.assertFalse(rows["Allah/Al-Quddus_180mm.step"]["printable"])
        self.assertEqual(nas.matrix(self.con, matched_only=True)["matched"], 2)
        nas.scan(self.con, log=lambda m: None, listing=LISTING.replace("Allah/box [v2].stl", "Allah/box.stl"))
        self.assertEqual(self.con.execute("SELECT gone FROM nas_files WHERE path='Allah/box [v2].stl'").fetchone()[0], 1)

    def test_import_one_file_escapes_wildcards(self):
        nas.scan(self.con, log=lambda m: None, listing=LISTING)
        calls = []

        def fake_rsync(*args, timeout=0):
            calls.append(args)
            Path(args[-1]).write_bytes(b"x" * 10)
            return ""
        with mock.patch.object(nas, "_rsync", fake_rsync), mock.patch.object(nas, "NAS_DIR", self.tmp / "nas"):
            r = nas.import_file(self.con, "Allah/box [v2].stl", "ui:Amir")
            with self.assertRaises(ValueError):
                nas.import_file(self.con, "Allah/Al-Quddus_180mm.step", "ui:Amir")
            with self.assertRaises(LookupError):
                nas.import_file(self.con, "../../etc/passwd", "ui:Amir")
        self.assertTrue(calls[0][1].endswith("Allah/box \\[v2\\].stl"))
        a = self.con.execute("SELECT * FROM artefacts WHERE id=?", (r["artefact"],)).fetchone()
        self.assertEqual((a["maker"], a["kind"]), ("nas", "model"))
        self.assertEqual(self.con.execute("SELECT artefact_id FROM nas_files WHERE path='Allah/box [v2].stl'").fetchone()[0],
                         r["artefact"])


def three_mf(path: Path, offsets) -> Path:
    """A 3MF with one cube object per offset, spread out like a multi-plate project."""
    core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    v = [(x, y, z) for x in (0, 10) for y in (0, 10) for z in (0, 10)]
    tri = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6),
           (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    mesh = ("<mesh><vertices>" + "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in v) +
            "</vertices><triangles>" + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tri) +
            "</triangles></mesh>")
    objs = "".join(f'<object id="{i + 1}" type="model" name="cube{i + 1}">{mesh}</object>' for i in range(len(offsets)))
    items = "".join(f'<item objectid="{i + 1}" transform="1 0 0 0 1 0 0 0 1 {x} {y} {z}"/>'
                    for i, (x, y, z) in enumerate(offsets))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", f'<model xmlns="{core}"><resources>{objs}</resources><build>{items}</build></model>')
    return path


class UploadMaker(unittest.TestCase):
    def ctx(self, f):
        return makers.GenContext(params={}, files={"file": f}, bed_mm=(220, 220, 250), nozzle=0.4, layer_height=0.2,
                                 out_dir=Path(tempfile.mkdtemp()), log=lambda m: None, label="part")

    def test_3mf_items_become_separate_stls_on_the_bed(self):
        f = three_mf(Path(tempfile.mkdtemp()) / "p.3mf", [(100, 100, 0), (450, 90, 5)])
        r = makers.get("upload").generate(self.ctx(f))
        self.assertEqual([m.label for m in r.models], ["cube1", "cube2"])
        self.assertEqual([m.triangles for m in r.models], [12, 12])
        self.assertEqual(r.models[1].bbox[0][2], 0.0)                     # dropped onto the bed
        self.assertEqual(r.warnings, [])

    def test_ascii_stl_and_refusals(self):
        d = Path(tempfile.mkdtemp())
        binary = box_stl(d / "b.stl", 0, 0, 0, 10, 10, 10)
        n = struct.unpack("<I", binary.read_bytes()[80:84])[0]
        asc = ["solid x"]
        data = binary.read_bytes()
        for i in range(n):
            vs = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
            asc += ["facet normal 0 0 0", "outer loop"] + [f"vertex {vs[k]} {vs[k + 1]} {vs[k + 2]}" for k in (0, 3, 6)] + \
                   ["endloop", "endfacet"]
        (d / "a.stl").write_text("\n".join(asc + ["endsolid x"]))
        self.assertEqual(makers.get("upload").generate(self.ctx(d / "a.stl")).models[0].triangles, 12)
        (d / "x.step").write_text("ISO-10303")
        with self.assertRaises(makers.MakerError):
            makers.get("upload").generate(self.ctx(d / "x.step"))


class SwitchedOff(unittest.TestCase):
    MSG = "mcu 'mcu': Unable to connect\nOnce the underlying issue is corrected, use the\n\"FIRMWARE_RESTART\" command"

    def test_off_is_only_unable_to_connect_while_idle(self):
        self.assertTrue(monitor.powered_off("error", self.MSG, "standby"))
        self.assertFalse(monitor.powered_off("error", self.MSG, "printing"))
        self.assertFalse(monitor.powered_off("error", "Lost communication with MCU 'mcu'", "standby"))
        self.assertFalse(monitor.powered_off("error", "Unable to open config file x.cfg", "standby"))
        self.assertFalse(monitor.powered_off("ready", "Printer is ready"))

    def test_monitor_notes_it_quietly_both_ways(self):
        m = FakeMoonraker(copy.deepcopy(GOOD))
        try:
            m.klipper, m.klipper_msg = "error", self.MSG
            p = dataclasses.replace(printers.PRINTERS["s1pro"], moonraker=m.url)
            state, out = monitor.check(p, {})
            self.assertEqual([lvl for lvl, _ in out], ["silent"])
            self.assertIn("powered off", out[0][1])
            state, out = monitor.check(p, state)
            self.assertEqual(out, [])                                       # once, not every pass
            m.klipper = "ready"
            state, out = monitor.check(p, state)
            self.assertEqual([(lvl, "connected again" in t) for lvl, t in out], [("silent", True)])
            m.klipper, m.klipper_msg = "error", "Unable to open config file x.cfg"
            state, out = monitor.check(p, state)
            self.assertEqual([lvl for lvl, _ in out], ["loud"])
        finally:
            m.close()


class Reconnect(unittest.TestCase):
    def setUp(self):
        self.m = FakeMoonraker(copy.deepcopy(GOOD))
        self.m.klipper, self.m.klipper_msg = "error", SwitchedOff.MSG
        self.m.config_file = "/home/aali/printer_s1pro_data/config/printer.cfg"
        port = int(self.m.url.rsplit(":", 1)[1])
        for p in (mock.patch.object(hosts, "load_hosts",
                                    lambda *a: [{"name": "fakehost", "address": "127.0.0.1", "ports": [port]}]),
                  mock.patch.dict(printers.PRINTERS, {"s1pro": dataclasses.replace(printers.PRINTERS["s1pro"],
                                                                                    moonraker=self.m.url)})):
            p.start()
            self.addCleanup(p.stop)
        fleet._cache.clear()
        self.addCleanup(fleet._cache.clear)

    def tearDown(self):
        self.m.close()

    def test_reconnect_restarts_only_a_switched_off_printer(self):
        self.assertTrue(fleet._instance("s1pro")["off"])
        with self.assertRaises(cfgchange.Refused):
            fleet.reconnect("s1pro", "agent")
        r = fleet.reconnect("s1pro", "ui:Amir", wait_s=3)
        self.assertEqual((r["state"], self.m.restarts), ("ready", 1))
        fleet._cache.clear()
        with self.assertRaises(cfgchange.Refused):                          # ready now: nothing to reconnect
            fleet.reconnect("s1pro", "ui:Amir", wait_s=1)
        self.m.klipper, self.m.klipper_msg = "error", "Unable to open config file x.cfg"
        with self.assertRaises(cfgchange.Refused):                          # a config error isn't a power switch
            fleet.reconnect("s1pro", "ui:Amir", wait_s=1)
        self.assertEqual(self.m.restarts, 1)

    def test_watch_only_refused(self):
        with mock.patch.dict(printers.PRINTERS, {"s1pro": dataclasses.replace(printers.PRINTERS["s1pro"],
                                                                               moonraker=self.m.url, readonly=True)}):
            fleet._cache.clear()
            with self.assertRaises(cfgchange.Refused):
                fleet.reconnect("s1pro", "ui:Amir", wait_s=1)
        self.assertEqual(self.m.restarts, 0)


if __name__ == "__main__":
    unittest.main()
