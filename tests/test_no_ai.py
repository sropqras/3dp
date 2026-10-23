"""The farm runs on its own tooling: no AI model is called anywhere in the pipeline, the web server or the jobs.
Claude sessions develop the tooling; they are never a step the farm depends on (Ironman, 2026-10-23)."""
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

    @unittest.skipUnless(JOBS.is_dir(), "jobctl definitions live on aali-cli")
    def test_farm_jobs_never_escalate_to_ai(self):
        for d in sorted(JOBS.glob("farm-*")):
            conf, run = (d / "job.conf").read_text(), (d / "run.sh").read_text()
            self.assertRegex(conf, r"(?m)^AI_ESCALATE=0", d.name)            # no Claude diagnosis on failure
            self.assertIn("export NOTIFY_PLAIN=1", run, d.name)               # no Ollama summaries
            self.assertIsNone(re.search(r"\bai_(local|haiku|sonnet)\b|\bescalate\b|claude", run), d.name)


if __name__ == "__main__":
    unittest.main()
