import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path

from farm import configs, db, ops, printers
from tests.fakes import FakeMoonraker
from tests.test_audit_model3mf import GOOD

TELEGRAM = """[bot]
server: localhost:7125
bot_token: 123456:SECRET-TOKEN
chat_id: 42

[status_message_content]
content: progress, height
"""


class TelegramPack(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(tempfile.mkdtemp()) / "farm.db")
        self.m = FakeMoonraker(copy.deepcopy(GOOD))
        self.m.config = {"telegram.conf": TELEGRAM.encode(), "printer.cfg": b"[printer]\n"}
        self.m.services = {"klipper-Ender3MaxNeo": "active/running", "moonraker-telegram-bot-Ender3MaxNeo": "active/running"}

    def tearDown(self):
        self.m.close()

    def test_merge_keeps_bot_and_replaces_pack_sections(self):
        new, replaced, kept = ops.merge_pack(TELEGRAM, ops.PACK.read_text())
        self.assertIn("bot_token = 123456:SECRET-TOKEN", new)
        self.assertIn("bot", kept)
        self.assertNotIn("bot", replaced)

    def test_pack_applied_bot_restarted_token_never_logged(self):
        r = ops.telegram_pack(self.con, "Ender3MaxNeo", self.m.url, "Ender3MaxNeo", "ui:Amir", wait_s=0)
        self.assertEqual(r["service"], "moonraker-telegram-bot-Ender3MaxNeo")
        self.assertEqual(self.m.service_restarts, ["moonraker-telegram-bot-Ender3MaxNeo"])
        self.assertIn(b"SECRET-TOKEN", self.m.config["telegram.conf"])
        self.assertEqual(self.m.config[r["backup"]], TELEGRAM.encode())            # old file kept ON the printer
        logged = " ".join(str(tuple(x)) for x in self.con.execute("SELECT * FROM events"))
        self.assertNotIn("SECRET-TOKEN", logged)
        self.assertNotIn("SECRET-TOKEN", repr(r))

    def test_bot_not_starting_restores_old_file(self):
        self.m.service_after = "activating/auto-restart"
        r = ops.telegram_pack(self.con, "Ender3MaxNeo", self.m.url, "Ender3MaxNeo", "ui:Amir", wait_s=0)
        self.assertTrue(r.get("rolled_back"))
        self.assertEqual(self.m.config["telegram.conf"], TELEGRAM.encode())

    def test_refusals(self):
        with self.assertRaises(ops.Refused):
            ops.telegram_pack(self.con, "Ender3MaxNeo", self.m.url, "Ender3MaxNeo", "agent")
        with self.assertRaises(ops.Refused):
            ops.telegram_pack(self.con, "EnderV3SE", self.m.url, "EnderV3SE", "ui:Amir")
        with self.assertRaises(PermissionError):
            configs.read_file(self.m.url, "telegram.conf")                         # the normal path stays closed


class Services(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(tempfile.mkdtemp()) / "farm.db")
        self.m = FakeMoonraker(copy.deepcopy(GOOD))
        self.m.services = {"klipper-Ender3MaxNeo": "active/running", "crowsnest": "active/running",
                           "moonraker-telegram-bot-Ender3Pro": "activating/auto-restart",
                           "moonraker-telegram-bot-EnderV3SE": "activating/auto-restart"}
        self.idle = [{"registry": "Ender3MaxNeo", "job": "standby"}]
        self.printing = [{"registry": "Ender3MaxNeo", "job": "printing"}]
        # a watch-only printer for the gates (the real registry has none since EnderV3SE was handed over)
        ro = dataclasses.replace(printers.PRINTERS["Ender3MaxNeo"], name="EnderV3SE", readonly=True)
        saved = printers.PRINTERS.get("EnderV3SE")
        printers.PRINTERS["EnderV3SE"] = ro
        self.addCleanup(lambda: printers.PRINTERS.__setitem__("EnderV3SE", saved) if saved else printers.PRINTERS.pop("EnderV3SE"))

    def tearDown(self):
        self.m.close()

    def restart(self, svc, host, actor="ui:Amir"):
        return ops.restart_service(self.con, "Ender3MaxNeo", self.m.url, svc, host, actor)

    def test_gates(self):
        with self.assertRaises(ops.Refused):
            self.restart("klipper-Ender3MaxNeo", self.printing)       # would stop a print
        with self.assertRaises(ops.Refused):
            self.restart("crowsnest", self.printing)
        with self.assertRaises(ops.Refused):
            self.restart("moonraker-telegram-bot-EnderV3SE", self.idle)   # watch-only printer's service
        with self.assertRaises(ops.Refused):
            self.restart("crowsnest", self.idle + [{"registry": "EnderV3SE", "readonly": True, "job": "standby"}])
        with self.assertRaises(ops.Refused):
            self.restart("sshd", self.idle)                           # not Moonraker-controllable
        with self.assertRaises(ops.Refused):
            self.restart("klipper-Ender3MaxNeo", self.idle, actor="agent")
        self.assertEqual(self.m.service_restarts, [])

    def test_bot_restart_allowed_even_while_printing(self):
        r = self.restart("moonraker-telegram-bot-Ender3Pro", self.printing)
        self.assertEqual((r["state"], self.m.service_restarts), ("active/running", ["moonraker-telegram-bot-Ender3Pro"]))


class Scripts(unittest.TestCase):
    def test_camera_and_instance_scripts(self):
        cam = ops.host_script("camera", name="s1pro", device="usb-Web_Camera_Web_Camera_240729163401-video-index0", port="8082")
        self.assertIn("-p 8082 -d $DEV", cam)
        self.assertIn("ustreamer-s1pro.service", cam)
        with self.assertRaises(ops.Refused):
            ops.host_script("camera", name="x", device="../../etc/passwd", port="8082")
        inst = ops.host_script("instance", name="neo2", port="7130", klipper_commit="ce7002bed")
        self.assertIn("moonraker-neo2.service", inst)
        self.assertIn("klippy-env-neo2", inst)
        self.assertNotIn("RemainAfterExit=yes", inst)      # stopped units must come back (the s1pro lesson)

    def test_retire_script_moves_aside_never_deletes(self):
        r = ops.host_script("retire", name="EnderCR10SE; rm -rf /", port="7127")
        self.assertIn("NAME=EnderCR10SErm-rf", r)                 # the name can't carry shell
        r = ops.host_script("retire", name="EnderCR10SE", port="7127")
        self.assertIn("/etc/systemd/retired/", r)
        self.assertIn("grep -q \"port: 7127\"", r)              # refuses if the name and port don't match
        self.assertNotRegex(r, r"\brm\s+-")
        with self.assertRaises(ops.Refused):
            ops.host_script("retire", name="../", port="7127")


if __name__ == "__main__":
    unittest.main()
