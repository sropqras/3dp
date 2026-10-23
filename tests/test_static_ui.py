"""Farm Control's look: the shared stylesheet and layout script are served, both pages use them, and every colour is a
theme token (so light and dark both cover the whole page)."""
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from farm import web

STATIC = Path(web.__file__).resolve().parent / "static"
PAGES = ("index.html", "printers.html")
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")


def css_blocks(text: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every innermost rule (a rule inside @media comes out with its own selector)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", text)]


def page_css(name: str) -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", (STATIC / name).read_text(encoding="utf-8"), flags=re.S))


def tokens(decls: str) -> set[str]:
    return set(re.findall(r"(--[\w-]+)\s*:", decls))


class StaticFilesServed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, "", b""

    def test_shared_files_have_their_types(self):
        for path, ctype in (("/static/farm.css", "text/css"), ("/static/ui.js", "text/javascript"),
                            ("/static/printers.js", "text/javascript"), ("/static/operator.js", "text/javascript")):
            code, got, _ = self.get(path)
            self.assertEqual((code, got.split(";")[0]), (200, ctype), path)

    def test_operator_default_is_filled_in(self):
        self.assertNotIn(b"__DEFAULT__", self.get("/static/operator.js")[2])

    def test_only_the_allowlist_is_reachable(self):
        for path in ("/static/index.html", "/static/../web.py", "/static/%2e%2e/web.py", "/static/nope.css", "/static/"):
            self.assertNotEqual(self.get(path)[0], 200, path)


class Pages(unittest.TestCase):
    def test_pages_load_the_theme_before_the_body(self):
        for name in PAGES:
            head = (STATIC / name).read_text(encoding="utf-8").split("</head>")[0]
            self.assertIn('<script src="/static/ui.js"></script>', head, name)     # theme on <html> before first paint
            self.assertIn('<link rel="stylesheet" href="/static/farm.css">', head, name)
            self.assertIn("data-settings", (STATIC / name).read_text(encoding="utf-8"), name)

    def test_panel_ids_are_unique_per_page(self):
        for name in PAGES:
            ids = re.findall(r'data-panel="([^"$]+)"', (STATIC / name).read_text(encoding="utf-8"))
            self.assertEqual(len(ids), len(set(ids)), name)


class ColoursAreTokens(unittest.TestCase):
    def test_no_colour_outside_a_token_declaration(self):
        sheets = {"farm.css": (STATIC / "farm.css").read_text(encoding="utf-8"), **{n: page_css(n) for n in PAGES}}
        for name, text in sheets.items():
            for sel, decls in css_blocks(text):
                for d in decls.split(";"):
                    prop = d.split(":", 1)[0].strip()
                    if prop.startswith("--"):
                        continue
                    self.assertIsNone(COLOUR.search(d), f"{name}: {sel} {{ {d.strip()} }} - use a token from farm.css")

    def test_no_literal_colour_in_inline_styles_or_svg_paint(self):
        """Data colours (a spool's hex, a colour picker) come in through ${...} and are allowed; literals are not."""
        for name in (*PAGES, "printers.js", "operator.js", "ui.js"):
            text = (STATIC / name).read_text(encoding="utf-8")
            for attr in re.findall(r'(?:style|stroke|fill)="([^"]*)"', text):
                literal = re.sub(r"\$\{[^}]*\}", "", attr)
                self.assertIsNone(COLOUR.search(literal), f"{name}: {attr!r}")

    def test_light_and_dark_define_the_same_colour_tokens(self):
        blocks = css_blocks((STATIC / "farm.css").read_text(encoding="utf-8"))
        dark = next(d for s, d in blocks if s.endswith(":root") and "--bg" in d)
        light = next(d for s, d in blocks if s == ':root[data-theme="light"]')
        system = next(d for s, d in blocks if s.startswith(':root:not([data-theme="dark"])'))   # inside the light @media
        self.assertEqual(tokens(dark), tokens(light))
        self.assertEqual(tokens(dark), tokens(system))
        self.assertEqual(re.sub(r"\s+", " ", light).strip(), re.sub(r"\s+", " ", system).strip())   # one light theme

    def test_every_token_used_is_defined(self):
        css = (STATIC / "farm.css").read_text(encoding="utf-8")
        defined = tokens(css)
        for name in ("farm.css", *PAGES, "printers.js", "ui.js", "operator.js"):
            text = (STATIC / name).read_text(encoding="utf-8")
            used = set(re.findall(r"var\((--[\w-]+)", text))
            self.assertFalse(used - defined, f"{name} uses undefined tokens")


if __name__ == "__main__":
    unittest.main()
