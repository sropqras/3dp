"""NAS catalogue 2.0: resilient scans, categories + rules, name tags, the query engine, previews (offline)."""
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from farm import db, meshprev, nas, nasindex, nasview
from tests.helpers import box_stl


def line(path, size=1000, when="2026/10/11 21:47:44"):
    return f"-rw-r--r--  {size:>13,} {when} {path}"


FILES = ["Allah/Al-Quddus_180mm.3mf", "Allah/sub/Ya Ali v2.stl", "Allah/photo.jpg", "zahabiya orders/ZA011/plate.stl",
         "Neptune 4 tool holder  - 6180371/files/holder (1).stl", "misc/CE3PRO_benchy_PETG.3mf", "misc/thing.step",
         "garage_misc/hook.stl"]
LISTING = "\n".join(line(p, 1000 + i) for i, p in enumerate(FILES))


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.con = db.connect(self.tmp / "farm.db")
        self.saved = (nas.NAS_DIR, nas.THUMB_DIR, nas.PREVIEW_DIR, nas.CACHE_DIR)
        nas.NAS_DIR = self.tmp / "nas"
        nas.THUMB_DIR, nas.PREVIEW_DIR, nas.CACHE_DIR = nas.NAS_DIR / "_thumbs", nas.NAS_DIR / "_preview", nas.NAS_DIR / "_cache"
        nasview._cache.clear()
        nas.scan(self.con, log=lambda m: None, listing=LISTING)

    def tearDown(self):
        nas.NAS_DIR, nas.THUMB_DIR, nas.PREVIEW_DIR, nas.CACHE_DIR = self.saved

    def cat(self, path):
        cid = self.con.execute("SELECT category_id FROM nas_files WHERE path=?", (path,)).fetchone()[0]
        return self.con.execute("SELECT name FROM nas_categories WHERE id=?", (cid,)).fetchone()[0]

    # ------------------------------------------------------------ scans
    def test_partial_or_failed_listing_hides_nothing_and_unchanged_writes_nothing(self):
        nas.scan(self.con, log=lambda m: None, listing=line(FILES[0]), partial=True)    # rsync exit 23
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM nas_files WHERE gone=1").fetchone()[0], 0)
        r = nas.scan(self.con, log=lambda m: None, listing=LISTING)
        self.assertTrue(r.get("unchanged"))
        with mock.patch.object(nas, "_rsync_rc", side_effect=RuntimeError("rsync exit 10: connection refused")):
            with self.assertRaises(RuntimeError):
                nas.scan(self.con, log=lambda m: None)
        st = nas.status(self.con)
        self.assertFalse(st["online"])
        self.assertIn("connection refused", st["last_error"])
        self.assertEqual(nasview.query(self.con, {})["total"], 7)                      # the cache still answers
        nas.scan(self.con, log=lambda m: None, listing=LISTING.replace("garage_misc/hook.stl", "garage_misc/hook2.stl"))
        self.assertEqual(self.con.execute("SELECT gone FROM nas_files WHERE path='garage_misc/hook.stl'").fetchone()[0], 1)
        self.assertTrue(nas.status(self.con)["online"])

    # ------------------------------------------------------------ categories + tags
    def test_seeded_categories_folder_subtrees_overrides_and_matches(self):
        self.assertEqual(self.cat("Allah/sub/Ya Ali v2.stl"), "Islamic art")            # folder rule covers the subtree
        self.assertEqual(self.cat("zahabiya orders/ZA011/plate.stl"), "Orders")
        self.assertEqual(self.cat("misc/CE3PRO_benchy_PETG.3mf"), "Printer parts & calibration")   # name match
        self.assertEqual(self.cat("misc/thing.step"), "Uncategorised")
        tools = self.con.execute("SELECT id FROM nas_categories WHERE name='Tools'").fetchone()[0]
        with self.assertRaises(PermissionError):
            nasview.assign(self.con, {"category": tools, "folders": ["misc"]}, "agent")
        nasview.assign(self.con, {"category": tools, "folders": ["misc"]}, "ui:Ironman")
        self.assertEqual(self.cat("misc/thing.step"), "Tools")
        self.assertEqual(self.cat("misc/CE3PRO_benchy_PETG.3mf"), "Tools")                # folder beats a name match
        isl = self.con.execute("SELECT id FROM nas_categories WHERE name='Islamic art'").fetchone()[0]
        nasview.assign(self.con, {"category": isl, "paths": ["misc/thing.step"]}, "ui:Ironman")
        self.assertEqual(self.cat("misc/thing.step"), "Islamic art")                      # a file override wins
        new = nasview.category_edit(self.con, {"action": "create", "name": "Garden"}, "ui:Ironman")["id"]
        nasview.assign(self.con, {"category": new, "folders": ["garage_misc"]}, "ui:Ironman")
        nasview.category_edit(self.con, {"action": "delete", "id": new}, "ui:Ironman")
        self.assertEqual(self.cat("garage_misc/hook.stl"), "Uncategorised")              # a deleted category's files
        with self.assertRaises(ValueError):
            uncat = self.con.execute("SELECT id FROM nas_categories WHERE name='Uncategorised'").fetchone()[0]
            nasview.category_edit(self.con, {"action": "delete", "id": uncat}, "ui:Ironman")

    def test_name_tags(self):
        t = nasindex.name_tags("Neptune 4 tool holder  - 6180371/files/holder (1).stl")
        self.assertIn("thing:6180371", t)
        self.assertIn("copy", t)
        self.assertEqual(nasindex.thing_url("thing:6180371"), "https://www.thingiverse.com/thing:6180371")
        t = nasindex.name_tags("misc/CE3PRO_benchy_PETG_v24.3mf")
        self.assertEqual([x for x in t if not x.startswith("thing")], ["for:CE3PRO", "v24", "mat:PETG", "calibration"])
        self.assertIn("size:180mm", nasindex.name_tags("Allah/Al-Quddus_180mm.3mf"))
        nasview.tag(self.con, {"paths": ["garage_misc/hook.stl"], "add": ["keeper"]}, "ui:Ironman")
        nas.scan(self.con, log=lambda m: None, listing=LISTING.replace("1,007", "1,107"))      # the file changed
        self.assertIn("keeper", [r[0] for r in self.con.execute("SELECT tag FROM nas_tags WHERE path='garage_misc/hook.stl'")])

    # ------------------------------------------------------------ query
    def test_query_tokens_facets_paging_and_tree(self):
        q = nasview.query(self.con, {"q": "ext:stl folder:allah"})
        self.assertEqual([r["path"] for r in q["rows"]], ["Allah/sub/Ya Ali v2.stl"])
        self.assertEqual(q["facets"]["ext"], {"stl": 1, "3mf": 1})          # ext facet ignores its own filter
        q = nasview.query(self.con, {"folder": "Allah"})
        self.assertEqual(q["total"], 2)                                       # the subtree, pictures excluded
        self.assertTrue(all(r["pictures"] for r in q["rows"] if r["folder"] == "Allah"))
        q = nasview.query(self.con, {"sort": "size", "desc": True, "limit": 2, "offset": 2})
        self.assertEqual(len(q["rows"]), 2)
        self.assertEqual(q["total"], 7)
        self.assertEqual(nasview.query(self.con, {"tags": ["thingiverse"]})["total"], 1)
        tr = nasview.tree(self.con)
        allah = next(c for c in tr["children"] if c["name"] == "Allah")
        self.assertEqual((allah["count"], [c["name"] for c in allah["children"]]), (2, ["sub"]))
        v = nasview.view_edit(self.con, {"action": "save", "name": "Orders STL", "spec": {"ext": ["stl"], "offset": 9}}, "ui:Ironman")
        self.assertEqual(nasview.views(self.con)[0]["spec"], {"ext": ["stl"]})
        nasview.view_edit(self.con, {"action": "delete", "id": v["id"]}, "ui:Ironman")
        self.assertEqual(nasview.views(self.con), [])

    # ------------------------------------------------------------ previews
    def fake_fetch(self, src: Path):
        def rsync(*args, timeout=0):
            Path(args[-1]).write_bytes(src.read_bytes())
            return ""
        return mock.patch.object(nas, "_rsync", side_effect=rsync)

    def test_preview_build_cache_invalidation_and_offline(self):
        stl = box_stl(self.tmp / "b.stl", 0, 0, 0, 20, 10, 5)
        with self.fake_fetch(stl):
            got = nas.thumb(self.con, "garage_misc/hook.stl")
        self.assertEqual(got[1], "image/png")
        self.assertTrue(got[0].startswith(b"\x89PNG"))
        self.assertFalse(list((self.tmp.parent).glob("nasprev/*")))                      # the original is not kept
        blob = nas.preview_blob(self.con, "garage_misc/hook.stl")
        hl = struct.unpack_from("<I", blob)[0]
        h = json.loads(blob[4:4 + hl])
        self.assertEqual((h["triangles"], h["preview"]), (12, True))
        self.assertAlmostEqual(h["bbox"][0][2], 0)                                       # standing on the bed
        # the file changes on the NAS: the cached preview is no longer valid
        nas.scan(self.con, log=lambda m: None, listing=LISTING.replace("1,007", "2,007"))
        self.assertIsNone(nas.meta(self.con, "garage_misc/hook.stl"))
        # offline: nothing is fetched, the file is queued for later
        with mock.patch.object(nas, "_rsync_rc", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                nas.scan(self.con, log=lambda m: None)
        with mock.patch.object(nas, "_rsync", side_effect=AssertionError("must not fetch while offline")):
            self.assertIsNone(nas.thumb(self.con, "garage_misc/hook.stl"))
            with self.assertRaises(RuntimeError):
                nas.preview_blob(self.con, "garage_misc/hook.stl", full=True)
        self.assertTrue(self.con.execute("SELECT wanted FROM nas_meta WHERE path='garage_misc/hook.stl'").fetchone()[0])

    def test_warm_takes_wanted_first_and_skips_big_files(self):
        stl = box_stl(self.tmp / "b.stl", 0, 0, 0, 5, 5, 5)
        self.con.execute("INSERT INTO nas_meta (path, wanted) VALUES ('Allah/sub/Ya Ali v2.stl', 99)")
        order = []
        with self.fake_fetch(stl), mock.patch.object(nas, "build_preview", side_effect=lambda con, p: order.append(p)):
            nas.warm(self.con, minutes=5, max_bytes=1005, log=lambda m: None)
        self.assertEqual(order[0], "Allah/sub/Ya Ali v2.stl")
        self.assertNotIn("garage_misc/hook.stl", order)                                  # 1007 bytes > the cap

    def test_meshprev_readers_decimate_raster(self):
        stl = box_stl(self.tmp / "b.stl", 0, 0, 0, 30, 20, 10)
        pos, idx = meshprev.read(stl)
        self.assertEqual(len(idx) // 3, 12)
        ascii_stl = self.tmp / "a.stl"
        ascii_stl.write_text("solid x\n" + "".join(
            f"facet normal 0 0 1\nouter loop\nvertex {pos[9*t]} {pos[9*t+1]} {pos[9*t+2]}\nvertex {pos[9*t+3]} {pos[9*t+4]} "
            f"{pos[9*t+5]}\nvertex {pos[9*t+6]} {pos[9*t+7]} {pos[9*t+8]}\nendloop\nendfacet\n" for t in range(12)) + "endsolid\n")
        self.assertEqual(len(meshprev.read(ascii_stl)[1]) // 3, 12)
        # a dense mesh gets thinned to the cap
        import math
        from array import array
        p2, i2 = array("f"), array("I")
        n = 120
        for a in range(n):
            for b in range(n):
                th, ph = 2 * math.pi * a / n, math.pi * b / n
                p2.extend((math.cos(th) * math.sin(ph) * 50, math.sin(th) * math.sin(ph) * 50, math.cos(ph) * 50))
        for a in range(n - 1):
            for b in range(n - 1):
                k = a * n + b
                i2.extend((k, k + n, k + 1, k + 1, k + n, k + n + 1))
        d, di = meshprev.decimate(p2, i2, 5000)
        self.assertLessEqual(len(di) // 3, 5000)
        self.assertGreater(len(di) // 3, 500)
        png = meshprev.render_png(*meshprev.read(stl), size=64)
        self.assertTrue(png.startswith(b"\x89PNG") and len(png) > 100)
        # a 3MF's own thumbnail wins over drawing one
        tmf = self.tmp / "t.3mf"
        with zipfile.ZipFile(tmf, "w") as z:
            z.writestr("3D/3dmodel.model", '<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                       '<resources><object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
                       '<vertex x="0" y="10" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
                       '</resources><build><item objectid="1"/></build></model>')
            z.writestr("Metadata/thumbnail.png", b"\x89PNG-embedded")
        out = meshprev.build(tmf, "t.3mf")
        self.assertEqual((out["thumb"], out["thumb_from"], out["triangles"]), (b"\x89PNG-embedded", "embedded", 1))


if __name__ == "__main__":
    unittest.main()
