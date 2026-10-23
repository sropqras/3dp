"""What a NAS path says about the file, and which farm category it belongs to.

OMV keeps its own folders; the farm lays its own categories over them. A category is reached by rules: a `folder`
rule covers that folder and everything under it, a `file` rule is a person's per-file override, a `match` rule is a
regex on the file name. The most specific wins (file > deepest folder > match). Categories are seeded once from the
top-level folders that existed on 2026-10-23; after that they are Ironman's to edit in the catalogue.

Tags come from the names themselves (Thingiverse ids, printer prefixes, sizes, versions, copies, materials,
calibration pieces) and are refreshed at every scan; a person's own tags (source 'person') are never touched.
"""
import re
from pathlib import PurePosixPath

from . import db

UNCATEGORISED = "Uncategorised"

# name -> (colour, [folder prefixes], [filename regexes])
SEED = [
    ("Islamic art", "#2e7d32", ["Allah", "Quran-Related", "YaHussain", "YaAli", "Kaabaa", "Masjid-Nabawi", "syedna",
                                "dawoodi bohra art", "Maarusah", "WarasMubarak", "Hasan"], []),
    ("Orders", "#c62828", ["zahabiya orders", "sakina mami order"], [r"(?i)\border\b"]),
    ("Printer parts & calibration", "#1565c0", ["3d printer parts", "Creality Ender 3 Pi Cam Mount - 2886101",
                                                "Neptune 4 tool holder  - 6180371",
                                                "Smart compact temperature calibration tower - 2729076"],
     [r"(?i)calibrat|benchy|xyz.?cube|temp.?tower|retraction"]),
    ("Garage & motorcycles", "#6d4c41", ["garage_misc", "Motorcycles"], []),
    ("Household", "#00838f", ["household", "Aquarium", "Bookmarks"], []),
    ("Tools", "#ef6c00", ["generic_tools", "sander", "soldering-clamp-model_files"], []),
    ("Toys & games", "#8e24aa", ["Games", "characters", "animals", "trinkets",
                                 "The Cyclone_ triple lift, triple track marble machine - 1340784"], []),
    ("Seasonal", "#d84315", ["Halloween_Chritmas"], []),
    ("Electronics", "#455a64", ["Electronics", "Laptop Parts", "Power Bank 3 - 4882346"], []),
    ("Lithophanes", "#9e9d24", ["01_litho parts"], [r"(?i)litho"]),
    ("Archive", "#757575", ["STL Archive"], []),
    ("Product photos", "#ad1457", ["@ProductImages"], []),
    (UNCATEGORISED, "#9e9e9e", [], []),
]

PRINTER_PREFIX = re.compile(r"^(CE3PRO|CE3S1PRO|CE3S1PLUS|CE3V3SE|ENDER3S1PLUS|ENDER3S1PRO|ENDER3MAX|ENDER3V3SE|"
                            r"MAXNEO|CR10SE|NEPTUNE4)[_ -]", re.I)
THINGIVERSE = re.compile(r" - (\d{6,8})(?:/|$)")
SIZE = re.compile(r"(?<![\d.])(\d{2,4}(?:\.\d)?)\s?mm\b", re.I)
VERSION = re.compile(r"(?i)(?:^|[ _\-])v(\d+(?:\.\d+)?)(?=$|[ _\-.])")
COPY = re.compile(r"\((\d+)\)$|[ _]copy$", re.I)
MATERIALS = ["PETG", "PLA", "TPU", "ABS", "ASA", "CF", "PA", "PC"]


def name_tags(path: str) -> list[str]:
    """Tags a file's own path gives away. Stable order, no duplicates."""
    p = PurePosixPath(path)
    stem, tags = p.stem, []
    m = THINGIVERSE.search(path)
    if m:
        tags += ["thingiverse", f"thing:{m.group(1)}"]
    m = PRINTER_PREFIX.match(stem)
    if m:
        tags.append(f"for:{m.group(1).upper()}")
    m = SIZE.search(stem)
    if m:
        tags.append(f"size:{m.group(1)}mm")
    m = VERSION.search(stem)
    if m:
        tags.append(f"v{m.group(1)}")
    if COPY.search(stem):
        tags.append("copy")
    words = set(re.split(r"[^A-Za-z]+", stem.upper()))
    tags += [f"mat:{x}" for x in MATERIALS if x in words]
    if re.search(r"(?i)calibrat|tower|test|benchy", stem):
        tags.append("calibration")
    if p.suffix.lower() in (".step", ".stp", ".f3d", ".fcstd", ".scad"):
        tags.append("cad-source")
    return list(dict.fromkeys(tags))


def thing_url(tag: str) -> str:
    return f"https://www.thingiverse.com/thing:{tag.split(':', 1)[1]}" if tag.startswith("thing:") else ""


def seed(con) -> bool:
    """Categories + folder/match rules, once (an empty nas_categories table). Returns True when it seeded."""
    if con.execute("SELECT 1 FROM nas_categories LIMIT 1").fetchone():
        return False
    with db.tx(con):
        for pos, (name, colour, folders, matches) in enumerate(SEED):
            cid = con.execute("INSERT INTO nas_categories (name, colour, pos) VALUES (?,?,?)", (name, colour, pos)).lastrowid
            for f in folders:
                con.execute("INSERT OR IGNORE INTO nas_rules (category_id, kind, pattern, created_by) VALUES (?,?,?,?)",
                            (cid, "folder", f, "seed"))
            for rx in matches:
                con.execute("INSERT OR IGNORE INTO nas_rules (category_id, kind, pattern, created_by) VALUES (?,?,?,?)",
                            (cid, "match", rx, "seed"))
    return True


class Resolver:
    """Category of a path under the current rules (built once per resolve pass)."""

    def __init__(self, con):
        rules = con.execute("SELECT category_id, kind, pattern FROM nas_rules ORDER BY pos, id").fetchall()
        self.files = {r[2]: r[0] for r in rules if r[1] == "file"}
        self.folders = {r[2].strip("/"): r[0] for r in rules if r[1] == "folder"}
        self.matches = []
        for r in rules:
            if r[1] == "match":
                try:
                    self.matches.append((re.compile(r[2]), r[0]))
                except re.error:
                    continue
        row = con.execute("SELECT id FROM nas_categories WHERE name=?", (UNCATEGORISED,)).fetchone()
        self.default = row[0] if row else None

    def __call__(self, path: str) -> int | None:
        if path in self.files:
            return self.files[path]
        parts = path.split("/")[:-1]
        for n in range(len(parts), 0, -1):          # deepest folder rule first
            hit = self.folders.get("/".join(parts[:n]))
            if hit:
                return hit
        name = path.rsplit("/", 1)[-1]
        for rx, cid in self.matches:
            if rx.search(name):
                return cid
        return self.default


def resolve(con, paths: list[str] | None = None) -> int:
    """Store each file's category (all files, or just these). Called after a scan and after any rule change."""
    seed(con)
    r = Resolver(con)
    rows = con.execute("SELECT path, category_id FROM nas_files" + ("" if paths is None else
                       " WHERE path IN (%s)" % ",".join("?" * len(paths))), paths or []).fetchall()
    changed = [(r(p), p) for p, cur in rows if r(p) != cur]
    if changed:
        with db.tx(con):
            con.executemany("UPDATE nas_files SET category_id=? WHERE path=?", changed)
    return len(changed)


def retag(con, files: list[dict]) -> None:
    """Refresh the automatic tags of these files (a person's tags stay)."""
    with db.tx(con):
        for f in files:
            con.execute("DELETE FROM nas_tags WHERE path=? AND source='auto'", (f["path"],))
            con.executemany("INSERT OR IGNORE INTO nas_tags (path, tag, source) VALUES (?,?, 'auto')",
                            [(f["path"], t) for t in name_tags(f["path"])])
