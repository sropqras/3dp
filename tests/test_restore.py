"""Restore points (farm/restore.py): the pinned original, dating configs by file times, print evidence, staging a
restore as one set and applying / rolling it back together through the gate."""
import copy
import dataclasses
import io
import json
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from farm import cfgchange, configs, db, printers, restore
from tests.fakes import FakeMoonraker
from tests.test_audit_model3mf import GOOD

DAY = 86400
T0 = datetime(2026, 9, 24, 11, 0).timestamp()            # the original config's newest file
EDIT = datetime(2026, 10, 22, 20, 10).timestamp()        # the first session edit (printer.cfg.bak-20261022-2010)
ORIG_PRINTER = b"[printer]\nkinematics: cartesian\nmax_velocity: 300\nmax_accel: 3000\n"
ORIG_MACROS = b"[gcode_macro START_PRINT]\ngcode:\n  G28\n"
FARM_MD = "# Farm\n\n| Date | Printer | Change |\n|---|---|---|\n"


class Restore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (configs.STORE, cfgchange.FARM_MD, restore.ARCHIVE, dict(printers.PRINTERS),
                      (cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S, cfgchange.READY_TIMEOUT_S))
        configs.STORE, restore.ARCHIVE = self.tmp / "snap", self.tmp / "archive"
        cfgchange.FARM_MD = self.tmp / "FARM.md"
        cfgchange.FARM_MD.write_text(FARM_MD)
        cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S, cfgchange.READY_TIMEOUT_S = 0, 0.05, 0.2, 2
        self.m = FakeMoonraker(copy.deepcopy(GOOD))
        edited = ORIG_PRINTER.replace(b"300", b"200")
        # the host as the first editing session left it: edited files, its .bak copies, a secret and its copy
        self.m.config = {"printer.cfg": edited, "macros.cfg": ORIG_MACROS + b"# farm\n",
                         "printer.cfg.bak-20261022-2010": ORIG_PRINTER, "macros.cfg.bak-20261022-2010": ORIG_MACROS,
                         "moonraker.conf": b"[server]\n", "telegram.conf": b"bot_token: 123:SECRET\n",
                         "telegram.conf.bak-20261022-2201": b"bot_token: 123:OLDSECRET\n"}
        self.m.mtimes = {"printer.cfg": EDIT + 60, "macros.cfg": EDIT + 60, "printer.cfg.bak-20261022-2010": T0,
                         "macros.cfg.bak-20261022-2010": T0 - DAY, "moonraker.conf": T0 - 300 * DAY}
        printers.PRINTERS["Ender3MaxNeo"] = dataclasses.replace(printers.PRINTERS["Ender3MaxNeo"], moonraker=self.m.url)
        self.con = db.connect(self.tmp / "farm.db")
        self.con.execute("INSERT INTO artefacts (kind, title, design, maker, created_at) VALUES ('gcode','a','a','printer',0)")

    def tearDown(self):
        configs.STORE, cfgchange.FARM_MD, restore.ARCHIVE, saved, timing = self.saved
        printers.PRINTERS.clear()
        printers.PRINTERS.update(saved)
        cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S, cfgchange.READY_TIMEOUT_S = timing
        self.m.close()

    def print_(self, start, end, status="completed", quality=None):
        self.con.execute("INSERT INTO prints (artefact_id, printer, file, started_at, finished_at, status, source, quality) "
                         "VALUES (1, 'Ender3MaxNeo', ?, ?, ?, ?, 'test', ?)", (f"p{start}.gcode", start, end, status, quality))

    def test_secret_copies_are_secrets(self):
        self.assertTrue(configs.secret("telegram.conf.bak-20261022-2201"))
        self.assertTrue(configs.secret("telegram.conf"))
        self.assertFalse(configs.tracked("telegram.conf.bak-20261022-2201"))
        self.assertFalse(configs.secret("printer.cfg.bak-20261022-2010"))
        self.assertNotIn("telegram.conf.bak-20261022-2201", [f["path"] for f in configs.list_history(self.m.url)])

    def test_origin_comes_from_the_bak_copies_and_is_pinned_once(self):
        configs.snapshot(self.con, "Ender3MaxNeo", self.m.url, actor="backup")
        o = restore.capture_origin(self.con, "Ender3MaxNeo", self.m.url, actor="cli:test")
        self.assertEqual(configs.blob("Ender3MaxNeo", o["files"]["printer.cfg"]), ORIG_PRINTER)
        self.assertEqual(configs.blob("Ender3MaxNeo", o["files"]["macros.cfg"]), ORIG_MACROS)
        self.assertNotIn("telegram.conf", o["files"])
        self.assertEqual(o["meta"]["since"], T0)               # newest original file
        self.assertEqual(o["meta"]["until"], EDIT)             # the first edit
        self.assertEqual(configs.latest(self.con, "Ender3MaxNeo")["kind"], "live")   # the origin isn't "latest"
        again = restore.capture_origin(self.con, "Ender3MaxNeo", self.m.url, actor="cli:test")
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["id"], o["id"])
        md = (restore.ARCHIVE / "Ender3MaxNeo" / "ORIGIN.md").read_text()
        self.assertIn("printer.cfg.bak-20261022-2010", md)
        self.assertEqual((restore.ARCHIVE / "Ender3MaxNeo" / "origin" / "printer.cfg").read_bytes(), ORIG_PRINTER)

    def test_evidence_last_known_good_and_origin(self):
        configs.snapshot(self.con, "Ender3MaxNeo", self.m.url, actor="backup")
        restore.capture_origin(self.con, "Ender3MaxNeo", self.m.url, actor="cli:test")
        self.print_(T0 + DAY, T0 + DAY + 3600)                 # on the original
        self.print_(T0 + 2 * DAY, T0 + 2 * DAY + 3600, quality="good")
        self.print_(T0 - DAY, T0 - DAY + 3600)                 # before the original's newest file: not evidence
        self.print_(EDIT - 600, EDIT + 600)                    # straddles the edit: not evidence for either
        self.print_(EDIT + 3600, EDIT + 7200, "error")         # on the edited config, failed
        self.con.execute("INSERT INTO prints (artefact_id, printer, file, started_at, finished_at, status, source) "
                         "VALUES (1, 'Ender3MaxNeo', 'retraction_tower_PLA.gcode', ?, ?, 'completed', 'test')",
                         (T0 + 3 * DAY, T0 + 3 * DAY + 600))   # calibration: never evidence
        pts = {p["kind"]: p for p in restore.points(self.con, "Ender3MaxNeo", self.m.url)}
        o = pts["origin"]
        self.assertEqual((o["prints"]["completed"], o["prints"]["good"], o["evidence"]), (2, 1, "exact"))
        self.assertIn("last known good", o["labels"])
        live = pts["live"]
        self.assertTrue(live["current"])
        self.assertEqual((live["prints"]["completed"], live["prints"]["failed"]), (0, 1))
        self.print_(EDIT + 2 * 3600, EDIT + 3 * 3600)           # now the edited config has printed too
        lkg = [p for p in restore.points(self.con, "Ender3MaxNeo", self.m.url) if "last known good" in p["labels"]]
        self.assertEqual(lkg[0]["kind"], "live")

    def test_marked_bad_is_not_known_good(self):
        configs.snapshot(self.con, "Ender3MaxNeo", self.m.url, actor="backup")
        restore.capture_origin(self.con, "Ender3MaxNeo", self.m.url, actor="cli:test")
        self.print_(T0 + DAY, T0 + DAY + 3600, quality="bad")
        self.assertFalse(any("last known good" in p["labels"] for p in restore.points(self.con, "Ender3MaxNeo", self.m.url)))

    def test_restore_is_one_set_applied_and_rolled_back_together(self):
        configs.snapshot(self.con, "Ender3MaxNeo", self.m.url, actor="backup")
        o = restore.capture_origin(self.con, "Ender3MaxNeo", self.m.url, actor="cli:test")
        self.m.config["farm_macros.cfg"] = b"[gcode_macro FARM_INFO]\ngcode:\n"
        live_before = dict(self.m.config)
        r = restore.stage_restore(self.con, "Ender3MaxNeo", self.m.url, o["id"], author="ui:Amir")
        self.assertEqual(sorted(r["files"]), ["macros.cfg", "printer.cfg"])
        self.assertEqual(r["files"][-1], "printer.cfg")         # printer.cfg last
        self.assertIn("farm_macros.cfg", r["left_in_place"])
        self.assertIn("+max_velocity: 300", restore.diff(self.con, "Ender3MaxNeo", self.m.url, o["id"]))
        # the restored config has the audit FAILs the original had: reported, not a failed restore
        self.m.on_restart = lambda m: m.settings["printer"].update(max_velocity=300)
        out = cfgchange.apply(self.con, r["changes"][0], "ui:Amir")
        self.assertEqual(out["state"], "applied", out["error"])
        self.assertEqual(self.m.restarts, 1)                    # one restart for the whole set
        self.assertEqual(self.m.config["printer.cfg"], ORIG_PRINTER)
        self.assertEqual(self.m.config["macros.cfg"], ORIG_MACROS)
        states = {cfgchange.get(self.con, i)["state"] for i in r["changes"]}
        self.assertEqual(states, {"applied"})
        self.assertIn("audit_fails_now", json.loads(out["result"]))
        self.m.on_restart = None
        back = cfgchange.rollback(self.con, r["changes"][1], "ui:Amir")
        self.assertEqual(back["state"], "rolled_back")
        self.assertEqual(self.m.restarts, 2)
        for p in ("printer.cfg", "macros.cfg"):
            self.assertEqual(self.m.config[p], live_before[p])
        now = configs.snapshot(self.con, "Ender3MaxNeo", self.m.url, actor="backup")
        with self.assertRaises(cfgchange.Refused):          # restoring what the printer already runs
            restore.stage_restore(self.con, "Ender3MaxNeo", self.m.url, now["id"], author="ui:Amir")

    def test_set_that_klipper_rejects_is_put_back_whole(self):
        configs.snapshot(self.con, "Ender3MaxNeo", self.m.url, actor="backup")
        o = restore.capture_origin(self.con, "Ender3MaxNeo", self.m.url, actor="cli:test")
        before = dict(self.m.config)
        r = restore.stage_restore(self.con, "Ender3MaxNeo", self.m.url, o["id"], author="ui:Amir")
        self.m.after_restart = "error"
        out = cfgchange.apply(self.con, r["changes"][0], "ui:Amir")
        self.assertEqual(out["state"], "rolled_back")
        self.assertEqual({cfgchange.get(self.con, i)["state"] for i in r["changes"]}, {"rolled_back"})
        for p in ("printer.cfg", "macros.cfg"):
            self.assertEqual(self.m.config[p], before[p])

    def test_snapshot_never_takes_a_file_time_newer_than_itself(self):
        s = configs.snapshot(self.con, "Ender3MaxNeo", self.m.url, actor="backup")
        self.con.execute("UPDATE config_snapshots SET mtimes=NULL, taken_at=? WHERE id=?", (EDIT + 120, s["id"]))
        self.m.mtimes["printer.cfg"] = time.time()                # re-uploaded unchanged (a rollback)
        configs.snapshot(self.con, "Ender3MaxNeo", self.m.url, actor="backup")
        mt = configs.get(self.con, s["id"])["mtimes"]
        self.assertNotIn("printer.cfg", mt)
        self.assertIn("macros.cfg", mt)

    def test_history_archive_leaves_secrets_out(self):
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("config/printer.cfg", ORIG_PRINTER)
            f.writestr("config/telegram.conf", "bot_token: X")
            f.writestr("config/moonraker.conf", "[power p]\ntype: http\npassword: hunter2\n")
        self.m.config["20260924-111417.zip"] = z.getvalue()
        self.m.config["printer-20250101_120000.cfg"] = ORIG_PRINTER
        self.m.config["backup_config.tar.gz"] = b"\x1f\x8b"
        h = restore.archive_history("Ender3MaxNeo", self.m.url)
        got = sorted(p.relative_to(restore.ARCHIVE / "Ender3MaxNeo" / "host").as_posix()
                     for p in (restore.ARCHIVE / "Ender3MaxNeo" / "host").rglob("*") if p.is_file())
        self.assertEqual(got, ["20260924-111417/config/printer.cfg", "macros.cfg.bak-20261022-2010",
                               "printer-20250101_120000.cfg", "printer.cfg.bak-20261022-2010"])
        self.assertEqual(len(h["skipped"]), 3)                 # telegram.conf, the moonraker.conf password, the tarball
        self.assertEqual(restore.archive_history("Ender3MaxNeo", self.m.url)["written"], 0)


if __name__ == "__main__":
    unittest.main()
