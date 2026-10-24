"""The farm runs on its own tooling: no language model and no paid or cloud AI is called anywhere in the pipeline,
the web server or the jobs. Claude sessions develop the tooling; they are never a step the farm depends on.
A local vision model (PrintGuard failure detection) is allowed: it costs nothing per use and stays on the LAN
(Ironman, 2026-10-23), so the only thing checked for it is that its address is local."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AI = re.compile(r"anthropic|openai|ollama|:11434|claude\s+-p|api\.claude|generativelanguage|replicate\.com|"
                r"huggingface\.co|remove\.bg", re.I)
JOBS = Path.home() / "agent_workspace" / "infrastructure" / "jobs" / "defs"


class NoAI(unittest.TestCase):
    def test_no_ai_calls_in_farm_code(self):
        hits = [f"{p.relative_to(REPO)}:{i}: {line.strip()[:80]}"
                for p in list((REPO / "farm").rglob("*.py")) + list((REPO / "farm" / "static").glob("*"))
                + list((REPO / "scripts").glob("*")) if p.is_file()
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
                if AI.search(line)]
        self.assertEqual(hits, [])

    def test_vision_service_stays_local(self):
        import ipaddress
        import urllib.parse
        from farm import config
        url = getattr(config, "PRINTGUARD_URL", "")
        if not url:
            return
        host = urllib.parse.urlparse(url).hostname or ""
        local = host in ("localhost",) or host.endswith((".local", ".ts.net"))
        try:
            ip = ipaddress.ip_address(host)
            local = ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")   # Tailscale
        except ValueError:
            pass
        self.assertTrue(local, f"FARM_PRINTGUARD_URL must stay on the LAN/Tailscale, not {host}")

    def test_power_strip_stays_local(self):
        """Printer power (farm/power.py) is the strip's LOCAL API: no farm code names Meross's cloud, and every strip
        address it is given is a LAN one."""
        import os
        from farm import power
        cloud = [str(p.relative_to(REPO)) for p in (REPO / "farm").rglob("*.py")
                 if re.search(r"meross\.com|iotx-|mqtt-[a-z]+\.meross", p.read_text(encoding="utf-8"), re.I)]
        self.assertEqual(cloud, [])
        for ip in [x for x in os.environ.get("FARM_MEROSS_DEVICES", "").split(",") if x.strip()]:
            power.lan_ip(ip.strip())

    @unittest.skipUnless(JOBS.is_dir(), "jobctl definitions live on aali-cli")
    def test_farm_jobs_never_escalate_to_ai(self):
        for d in sorted(JOBS.glob("farm-*")):
            conf, run = (d / "job.conf").read_text(), (d / "run.sh").read_text()
            self.assertRegex(conf, r"(?m)^AI_ESCALATE=0", d.name)            # no Claude diagnosis on failure
            self.assertIn("export NOTIFY_PLAIN=1", run, d.name)               # no Ollama summaries
            self.assertIsNone(re.search(r"\bai_(local|haiku|sonnet)\b|\bescalate\b|claude", run), d.name)


if __name__ == "__main__":
    unittest.main()
