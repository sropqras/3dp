"""The farm standard for every printer's front end: one macro library (printers/standard/farm_macros.cfg, the same
bytes on every printer) plus a small per-printer values block, and one Fluidd layout (macro groups, temperature
presets from the farm profiles). The printers' macros all descend from one ancestor file with the numbers written
in; the library takes the numbers out into [gcode_macro _FARM_PRINTER] so the macros themselves are identical.

Macros go on a printer only as staged config changes (farm/baseline.py's fix for the "farm macro library is
current" rule), applied by a person through the gate. The Fluidd layout is a person's action in the Printers tab;
what it replaces is kept in the farm DB so it can be put back.
"""
import json
import posixpath
import re
import time

from . import db, net, printers

LIBRARY = printers.REPO / "printers" / "standard" / "farm_macros.cfg"
LIBRARY_FILE = "farm_macros.cfg"
INCLUDE = f"[include {LIBRARY_FILE}]"
GROUPS = ("Print", "Filament", "Bed", "Tune", "Utility")
SAVE_MARK = "\n#*# <---------------------- SAVE_CONFIG"


def library_text() -> str:
    return LIBRARY.read_text(encoding="utf-8")


def version(text: str | None = None) -> str:
    m = re.search(r"^variable_version:\s*'([^']+)'", text if text is not None else library_text(), re.M)
    return m.group(1) if m else ""


def library_macros(text: str | None = None) -> list[str]:
    return re.findall(r"^\[gcode_macro (\S+)\]", text if text is not None else library_text(), re.M)


def reached(files: dict, main: str = "printer.cfg") -> list[str]:
    """The config files Klipper actually loads: printer.cfg and what its [include]s reach (no globs)."""
    todo, seen = [main], []
    while todo:
        path = todo.pop(0)
        if path in seen or path not in files:
            continue
        seen.append(path)
        for inc in re.findall(r"^\s*\[include\s+([^\]]+?)\s*\]", files[path].split(SAVE_MARK)[0], re.M | re.I):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), inc.strip()))
            if "*" not in target and not target.startswith(".."):
                todo.append(target)
    return seen


def _num(text: str, pattern: str, default: float) -> float:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else default


def printer_values(files: dict, sensor: str | None) -> dict:
    """This printer's numbers, read from the ancestral macros it has now (so nothing changes when they move)."""
    from .baseline import macro_body
    have = "".join(macro_body(t, "_FARM_PRINTER") for t in files.values())
    if have:   # already on the library: its values are the printer's (a person may have tuned them)
        var = dict(re.findall(r"^variable_(\w+):\s*(.*?)\s*$", have, re.M))
        num = lambda k, d: float(var[k]) if re.match(r"^-?[\d.]+$", var.get(k, "")) else d
        return {"nozzle_temp": num("nozzle_temp", 220), "bed_temp": num("bed_temp", 70),
                "pid_nozzle": num("pid_nozzle", 220), "pid_bed": num("pid_bed", 70),
                "sensor": var.get("sensor", "''").strip("'\"") if "sensor" in var else (sensor or "")}
    allt = {name: "".join(macro_body(t, name) for t in files.values()) for name in ("HEAT", "PID_EXTRUDER", "PID_BED", "BED_MESH")}
    nozzle = _num(allt["HEAT"], r"TARGET=(\d+)", 220)
    return {"nozzle_temp": nozzle, "bed_temp": _num(allt["BED_MESH"], r"default\((\d+)\)", 70),
            "pid_nozzle": _num(allt["PID_EXTRUDER"], r"TARGET=(\d+)", nozzle),
            "pid_bed": _num(allt["PID_BED"], r"TARGET=(\d+)", 70), "sensor": sensor or ""}


def sensor_in_use(files: dict, sensor: str | None) -> str | None:
    """The sensor the printer's own _DISABLE_FS really switches: a config can declare a runout sensor whose macros
    are commented out on purpose (the MaxNeo has no sensor fitted) - the library must not switch it on."""
    from .baseline import macro_body
    body = "".join(macro_body(t, "_DISABLE_FS") for t in files.values())
    if not body:
        return sensor
    m = re.search(r"^[ \t]+SET_FILAMENT_SENSOR\s+SENSOR=(\S+)", body, re.M | re.I)
    return m.group(1) if m else None


def printer_block(v: dict) -> str:
    n = lambda x: int(x) if float(x).is_integer() else x
    return f"""[gcode_macro _FARM_PRINTER]
description: Utility | this printer's values for the farm macro library ({LIBRARY_FILE})
variable_nozzle_temp: {n(v['nozzle_temp'])}
variable_bed_temp: {n(v['bed_temp'])}
variable_pid_nozzle: {n(v['pid_nozzle'])}
variable_pid_bed: {n(v['pid_bed'])}
variable_sensor: '{v['sensor']}'
gcode:
    FARM_INFO
"""


def install(files: dict, sensor: str | None) -> dict:
    """{path: new text} that puts the library on a printer: the library file itself, the include and the values
    block in the macros file, and every same-named ancestral macro taken out of the files Klipper loads (so there
    is one definition of each). Library first: it must exist before anything includes it."""
    from .baseline import macro_body, remove_section, replace_section
    lib = library_text()
    names = library_macros(lib)
    loaded = reached(files) or [p for p in files if p.endswith(".cfg")]
    macros = "macros.cfg" if "macros.cfg" in loaded else "printer.cfg"
    values = printer_values({p: files[p] for p in loaded}, sensor_in_use({p: files[p] for p in loaded}, sensor))
    out = {LIBRARY_FILE: lib}
    for path in loaded:
        if path == LIBRARY_FILE:
            continue
        t = files[path]
        for name in names + (["_FARM_PRINTER"] if path != macros else []):
            t = remove_section(t, f"gcode_macro {name}")
        if path == macros:
            if not re.search(r"^\s*\[include\s+" + re.escape(LIBRARY_FILE) + r"\s*\]", t, re.M):
                t = f"{INCLUDE}   # farm macro library - the same file on every farm printer\n" + t
            t = replace_section(t, "gcode_macro _FARM_PRINTER", printer_block(values))
        if t != files[path]:
            out[path] = t
    if files.get(LIBRARY_FILE) == lib:
        out.pop(LIBRARY_FILE)
    return out


def check(settings: dict) -> tuple[bool, str]:
    """Audit: the library is loaded at the repo's version."""
    want = version()
    have = str((settings.get("gcode_macro farm_info") or {}).get("variable_version", "")).strip("'\"")
    if not have:
        return False, f"not installed (library {want}: Stage fix puts it on, one definition of each shared macro)"
    return have == want, f"{have}" + ("" if have == want else f" - the repo has {want}")


# ---------------------------------------------------------------- Fluidd

# the ancestral names every printer shares, for macros whose description doesn't carry a group (yet)
NAME_GROUPS = [("Print", r"^(start_print|print_end|pause|resume|cancel_print|m600|pause_macro|fc_resume|set_pause_.*|"
                         r"set_print_stats_info|line_purge|m205|m900)$"),
               ("Filament", r"^(load_filament|unload_filament|purge)$"),
               ("Bed", r"^(bed_.*|g29|calibrate_z_ui|ui_bed_tram_check|z_.*calibrate.*|probe_.*)$"),
               ("Tune", r"^(pid.*|press_advance.*|shaper|tune_.*|autotune_.*|inputshaper.*)$"),
               ("Utility", r"^(cool|heat|nozzle_mid|disable_steppers|farm_info|hello_world)$")]


def _group(desc: str, name: str = "") -> str:
    g = (desc or "").split("|", 1)[0].strip()
    if "|" in (desc or "") and g in GROUPS:
        return g
    return next((grp for grp, rx in NAME_GROUPS if re.match(rx, name.lower())), "")


def preset_temps() -> list[dict]:
    """One Fluidd temperature preset per farm filament (from the profiles' presets), same names everywhere."""
    from . import profiles
    seen, out = set(), []
    for p in profiles.PROFILES.values():
        try:
            t = profiles.temperatures(p)
        except Exception:
            continue
        name = f"Farm {p.material.upper()} {t['nozzle']}/{t['bed']}"
        if name not in seen:
            seen.add(name)
            out.append({"name": name, "nozzle": t["nozzle"], "bed": t["bed"]})
    return out


def fluidd_layout(current: dict, macros: dict, presets: list[dict]) -> dict:
    """The farm's Fluidd settings for one instance: its macro panel grouped Print / Filament / Bed / Tune / Utility
    by each macro's description, standard macros' look (shared ids so every printer matches), and the farm's
    temperature presets added in front of the printer's own. `macros` is {lower name: description}."""
    cats = [{"id": f"farm-{g.lower()}", "name": g} for g in GROUPS]
    old = {m.get("name"): m for m in (current.get("macros") or {}).get("stored", []) if isinstance(m, dict)}
    stored = []
    for name, desc in sorted(macros.items()):
        if name.startswith("_"):
            continue
        g = _group(desc, name)
        prev = old.get(name, {})
        stored.append({"name": name, "alias": prev.get("alias", ""), "color": prev.get("color", ""),
                       "visible": prev.get("visible", True),
                       "disabledWhilePrinting": g in ("Bed", "Tune", "Utility", "Filament") and name not in ("cool",),
                       "categoryId": f"farm-{g.lower()}" if g else prev.get("categoryId", "0")})
    ui = json.loads(json.dumps(current.get("uiSettings") or {}))
    dash = ui.setdefault("dashboard", {})
    own = [p for p in dash.get("tempPresets") or [] if not str(p.get("id", "")).startswith("farm-")]
    farm = [{"id": f"farm-{re.sub(r'[^a-z0-9]+', '-', p['name'].lower())}", "name": p["name"],
             "values": {"extruder": {"value": p["nozzle"], "type": "heater", "active": True},
                        "heater_bed": {"value": p["bed"], "type": "heater", "active": True}}} for p in presets]
    dash["tempPresets"] = farm + own
    return {"macros": {**(current.get("macros") or {}), "categories": cats, "stored": stored},
            "uiSettings": ui}


def _fluidd(url: str) -> dict:
    try:
        return net.get_json(f"{url}/server/database/item?namespace=fluidd", timeout=10)["result"]["value"]
    except Exception:
        return {}


def _put(url: str, key: str, value):
    net.post_json_body(f"{url}/server/database/item", {"namespace": "fluidd", "key": key, "value": value}, timeout=10)


def fluidd_preview(url: str, macros: dict) -> dict:
    cur = _fluidd(url)
    new = fluidd_layout(cur, macros, preset_temps())
    groups = {}
    for m in new["macros"]["stored"]:
        groups.setdefault(next((c["name"] for c in new["macros"]["categories"] if c["id"] == m["categoryId"]), "other"), []).append(m["name"])
    return {"groups": groups, "presets": [p["name"] for p in new["uiSettings"]["dashboard"]["tempPresets"]]}


def fluidd_apply(con, printer: str, url: str, macros: dict, actor: str) -> dict:
    """Write the farm layout into the instance's Fluidd settings (Moonraker DB, namespace fluidd). Only the macro
    panel and the temperature presets change; what was there is kept in the farm DB for Undo."""
    if not actor.startswith(("ui:", "cli:")):
        raise PermissionError("only a person changes a printer's Fluidd layout")
    cur = _fluidd(url)
    new = fluidd_layout(cur, macros, preset_temps())
    before = {"macros": cur.get("macros"), "uiSettings": cur.get("uiSettings")}
    rid = con.execute("INSERT INTO ui_pushes (printer, url, before, after, actor, at) VALUES (?,?,?,?,?,?)",
                      (printer, url, json.dumps(before), json.dumps(new), actor, time.time())).lastrowid
    for k in ("macros", "uiSettings"):
        _put(url, k, new[k])
    return {"id": rid, **fluidd_preview(url, macros)}


def fluidd_undo(con, rid: int, actor: str) -> dict:
    if not actor.startswith(("ui:", "cli:")):
        raise PermissionError("only a person changes a printer's Fluidd layout")
    r = con.execute("SELECT * FROM ui_pushes WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise LookupError(f"no Fluidd push {rid}")
    before = json.loads(r["before"])
    for k, v in before.items():
        if v is not None:        # a key Fluidd never had stays as the push left it (Fluidd fills its defaults)
            _put(r["url"], k, v)
    con.execute("UPDATE ui_pushes SET undone_by=?, undone_at=? WHERE id=?", (actor, time.time(), rid))
    return {"id": rid, "printer": r["printer"], "restored": [k for k, v in before.items() if v is not None]}


def live_macros(url: str) -> dict:
    """{lower macro name: description} as Klipper loaded them."""
    s = net.get_json(f"{url}/printer/objects/query?configfile=settings", timeout=10)["result"]["status"]["configfile"]["settings"]
    return {k[len("gcode_macro "):]: (v.get("description") or "") for k, v in s.items() if k.startswith("gcode_macro ")}
