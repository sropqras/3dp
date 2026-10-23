"""Farm-baseline fixes: for each farm/audit.py rule a printer fails, the change that fixes it - generated from the
reference implementation proven on the farm (enders1plus' macros, 2026-10-22), adapted to the printer (its name in
messages, its filament sensor or none) - and staged through farm/cfgchange.py like any other change, so a person
reviews the diff and Applies it through the gate. This is what replaces an AI session copying macros by hand.

A rule that needs judgement (renaming a macro, a START_PRINT that doesn't take temperatures) gets no generated fix:
the Printers tab offers the editor instead.
"""
import re

from . import cfgchange, configs

SAVE_MARK = "\n#*# <---------------------- SAVE_CONFIG"


# ---------------------------------------------------------------- config text helpers

def sections(text: str) -> list[str]:
    head = text.split(SAVE_MARK)[0]
    return re.findall(r"^\[([^\]]+)\]", head, re.M)


def section_span(text: str, name: str):
    head = text.split(SAVE_MARK)[0]
    m = re.search(r"^\[" + re.escape(name) + r"\][ \t]*\n", head, re.M | re.I)
    if not m:
        return None
    nxt = re.search(r"^\[", head[m.end():], re.M)
    end = m.end() + (nxt.start() if nxt else len(head) - m.end())
    return m.start(), end


def replace_section(text: str, name: str, block: str) -> str:
    """Replace [name] (header to the next section) with block, or add block before SAVE_CONFIG / at the end."""
    block = block.rstrip("\n") + "\n\n"
    span = section_span(text, name)
    if span:
        return text[:span[0]] + block + text[span[1]:]
    head, sep, tail = text.partition(SAVE_MARK)
    return head.rstrip("\n") + "\n\n" + block + (sep + tail if sep else "")


def set_option(text: str, section: str, option: str, value: str, create: bool = False) -> str:
    """Like cfgchange.key_edit, but also drops a multi-line value's indented continuation lines, and can add the
    section when it doesn't exist."""
    span = section_span(text, section)
    if span is None:
        if not create:
            raise cfgchange.Refused(f"[{section}] is not in this file")
        return replace_section(text, section, f"[{section}]\n{option}: {value}\n")
    start, end = span
    body = text[start:end]
    lines = body.split("\n")
    out, skipping, done = [], False, False
    for i, line in enumerate(lines):
        if i == 0:
            out.append(line)
            continue
        if skipping and (line.startswith((" ", "\t")) and line.strip()):
            continue
        skipping = False
        if not done and re.match(re.escape(option) + r"[ \t]*[:=]", line):
            out.append(f"{option}: {value}")
            skipping, done = True, True
            continue
        out.append(line)
    if not done:
        out.insert(1, f"{option}: {value}")
    return text[:start] + "\n".join(out) + text[end:]


def macro_body(text: str, name: str) -> str:
    span = section_span(text, f"gcode_macro {name}")
    return text[span[0]:span[1]] if span else ""


# ---------------------------------------------------------------- the reference blocks

def idle_block(printer: str) -> str:
    return f"""[idle_timeout]
# Farm baseline: while paused only the nozzle cools; bed and motors hold the print. RESUME reheats.
# A 12 h cap (_PAUSE_HARD_STOP) then turns the bed off too. The pause itself sets 30 min (PAUSE_MACRO).
gcode:
  {{% if printer.pause_resume.is_paused %}}
    M104 S0
    UPDATE_DELAYED_GCODE ID=_PAUSE_HARD_STOP DURATION=41400
    RESPOND TYPE=echo MSG="Paused 30 min: nozzle off, bed and motors kept on. Bed turns off after 12 h paused. RESUME reheats."
    RESPOND PREFIX=tgnotify MSG="{printer}: still paused after 30 min - nozzle off, bed holding the print"
  {{% else %}}
    TURN_OFF_HEATERS
    M84
  {{% endif %}}
timeout: 3600
"""


def hard_stop_block(printer: str) -> str:
    return f"""[delayed_gcode _PAUSE_HARD_STOP]
# 12 h safety cap on a pause: all heaters off, motors keep holding position so the print can still resume
gcode:
    {{% if printer.pause_resume.is_paused %}}
      TURN_OFF_HEATERS
      RESPOND TYPE=echo MSG="Paused 12 h: all heaters off, motors still holding. RESUME reheats - check the plate is still stuck first."
      RESPOND PREFIX=tgalarm MSG="{printer}: paused 12 h - all heaters OFF. Check the plate is still stuck before RESUME."
    {{% endif %}}
"""


def colour_change_blocks(printer: str, sensor: str | None) -> dict:
    """Guided M600 (no auto-unload: an unattended eject tangles the filament end on the spool), its prompt with
    UNLOAD/LOAD/PURGE/RESUME, and the sensor guard - the S1 Plus set, sensor name per printer."""
    fs_off = f"SET_FILAMENT_SENSOR SENSOR={sensor} ENABLE=0" if sensor else "M117 colour change"
    fs_on = f"SET_FILAMENT_SENSOR SENSOR={sensor} ENABLE=1" if sensor else "M117 printing"
    return {
        "gcode_macro M600": """[gcode_macro M600]
description: Colour change: park, then a Fluidd prompt (UNLOAD / LOAD / PURGE / RESUME); no automatic unload
gcode:
    PAUSE_MACRO
    _DISABLE_FS
    _COLOUR_CHANGE_PROMPT
""",
        "gcode_macro PAUSE_MACRO": """[gcode_macro PAUSE_MACRO]
gcode:
    PAUSE
    SET_IDLE_TIMEOUT TIMEOUT=1800
""",
        "gcode_macro _COLOUR_CHANGE_PROMPT": f"""[gcode_macro _COLOUR_CHANGE_PROMPT]
gcode:
    RESPOND TYPE=command MSG="action:prompt_begin Colour change"
    RESPOND TYPE=command MSG="action:prompt_text Hold the filament, press UNLOAD and clip the old end to its spool."
    RESPOND TYPE=command MSG="action:prompt_text Insert the next colour, press LOAD, PURGE until it runs clean, remove the blob, wipe the nozzle, then RESUME."
    RESPOND TYPE=command MSG="action:prompt_button UNLOAD|UNLOAD_FILAMENT|error"
    RESPOND TYPE=command MSG="action:prompt_button LOAD 75mm|LOAD_FILAMENT|primary"
    RESPOND TYPE=command MSG="action:prompt_button PURGE 45mm|PURGE|secondary"
    RESPOND TYPE=command MSG="action:prompt_footer_button RESUME|_COLOUR_CHANGE_RESUME|warning"
    RESPOND TYPE=command MSG="action:prompt_show"
    RESPOND PREFIX=tgalarm MSG="{printer}: colour change - load the next colour, then RESUME"
""",
        "gcode_macro _COLOUR_CHANGE_RESUME": """[gcode_macro _COLOUR_CHANGE_RESUME]
gcode:
    RESPOND TYPE=command MSG="action:prompt_end"
    # relieve nozzle pressure after PURGE so it doesn't ooze a ring across the plate on the way back
    M83
    G1 E-2 F1800
    G4 P2000
    RESUME
""",
        "gcode_macro _DISABLE_FS": f"""[gcode_macro _DISABLE_FS]
description: no runout trigger while the filament is out on purpose
gcode:
    {fs_off}
""",
        "gcode_macro _ENABLE_FS": f"""[gcode_macro _ENABLE_FS]
gcode:
    {fs_on}
""",
    }


# ---------------------------------------------------------------- per-rule fixes

def _sensor(settings: dict, files: dict) -> str | None:
    """The filament sensor's name as written in the config: Moonraker's settings lowercase section names, but
    SET_FILAMENT_SENSOR SENSOR= needs the name exactly as the config declares it."""
    for text in files.values():
        m = re.search(r"^\[filament_(?:switch|motion)_sensor ([^\]]+)\]", text.split(SAVE_MARK)[0], re.M)
        if m:
            return m.group(1).strip()
    for k in settings:
        if k.startswith(("filament_switch_sensor ", "filament_motion_sensor ")):
            return k.split(" ", 1)[1]
    return None


def _file_with(files: dict, section: str, default: str) -> str:
    for path, text in files.items():
        if section_span(text, section):
            return path
    return default


def fixable(rule: str) -> bool:
    """Has a generated fix (fixes_for knows it) - the Printers tab shows "Stage fix" only for these."""
    return (rule.startswith(("max_velocity", "max_accel", "pause_resume recover_velocity")) or
            rule.endswith(": insert does not resume") or
            rule in ("START_PRINT runs CLEAR_PAUSE", "M600 macro exists", "M600 does not auto-unload",
                     "M600 shows the colour-change prompt", "colour change alerts Telegram (tgalarm)",
                     "idle_timeout is finite (<= 4 h)", "idle timeout is pause-aware", "12 h pause safety cap exists"))


def fixes_for(rule: str, settings: dict, files: dict, printer: str) -> list[dict]:
    """[{path, content, summary}] that fix `rule`, or [] when it needs a person (open the editor).
    files: {path: text} of the printer's farm-tracked config."""
    macros = "macros.cfg" if "macros.cfg" in files else "printer.cfg"
    out = {}

    def edit(path, fn, summary):
        cur = out.get(path, {}).get("content", files.get(path, ""))
        out[path] = {"path": path, "content": fn(cur), "summary": summary}

    if rule.startswith("max_velocity"):
        p = _file_with(files, "printer", "printer.cfg")
        edit(p, lambda t: set_option(t, "printer", "max_velocity", "200"), "[printer] max_velocity: 200")
    elif rule.startswith("max_accel"):
        p = _file_with(files, "printer", "printer.cfg")
        edit(p, lambda t: set_option(t, "printer", "max_accel", "3000"), "[printer] max_accel: 3000")
    elif rule.startswith("pause_resume recover_velocity"):
        p = _file_with(files, "pause_resume", "printer.cfg")
        edit(p, lambda t: set_option(t, "pause_resume", "recover_velocity", "150", create=True),
             "[pause_resume] recover_velocity: 150")
    elif rule == "START_PRINT runs CLEAR_PAUSE":
        p = _file_with(files, "gcode_macro START_PRINT", macros)

        def add_clear(t):
            span = section_span(t, "gcode_macro START_PRINT")
            body = t[span[0]:span[1]]
            m = re.search(r"^gcode:[ \t]*\n((?:[ \t]*\{%.*%\}[ \t]*\n)*)", body, re.M)   # after leading {% set %}
            if not m:
                raise cfgchange.Refused("START_PRINT has no gcode: block")
            nxt = body[m.end():].split("\n", 1)[0]
            indent = re.match(r"[ \t]*", nxt).group(0) or "    "
            new = body[:m.end()] + f"{indent}# farm baseline: a stale pause made PAUSE answer 'already paused'\n" \
                                   f"{indent}CLEAR_PAUSE\n" + body[m.end():]
            return t[:span[0]] + new + t[span[1]:]
        edit(p, add_clear, "START_PRINT runs CLEAR_PAUSE")
    elif rule in ("M600 macro exists", "M600 does not auto-unload", "M600 shows the colour-change prompt",
                  "colour change alerts Telegram (tgalarm)"):
        # Minimal: change only what's missing. A printer's existing colour-change macros are tuned (anti-ooze
        # resume, prompt wording) - a fix never replaces them wholesale.
        blocks = colour_change_blocks(printer, _sensor(settings, files))
        p600 = _file_with(files, "gcode_macro M600", macros)
        prompt_name = next((re.match(r"\s*(\S+)", l).group(1) for l in macro_body(files.get(p600, ""), "M600").splitlines()
                            if "PROMPT" in l and not l.lstrip().startswith(("#", "description"))), None)

        def add_missing(t):
            """Add each set member the printer doesn't define anywhere."""
            for name, block in blocks.items():
                if not any(section_span(x, name) for pth, x in files.items() if pth != macros) and not section_span(t, name):
                    t = replace_section(t, name, block)
            return t

        if rule == "M600 macro exists":
            edit(macros, add_missing, "farm guided colour change (M600 + prompt, no auto-unload, tgalarm)")
        elif rule == "M600 does not auto-unload":
            def drop_unload(t):
                span = section_span(t, "gcode_macro M600")
                body = t[span[0]:span[1]]
                body = re.sub(r"^[ \t]*UNLOAD_FILAMENT\b.*\n", "", body, flags=re.M)
                return t[:span[0]] + body + t[span[1]:]
            edit(p600, drop_unload, "M600 no longer unloads on its own (UNLOAD is a prompt button)")
        elif rule == "M600 shows the colour-change prompt":
            def add_prompt(t):
                t = add_missing(t)
                span = section_span(t, "gcode_macro M600")
                body = t[span[0]:span[1]]
                if "_COLOUR_CHANGE_PROMPT" not in body:
                    body = body.rstrip("\n") + "\n    _COLOUR_CHANGE_PROMPT\n\n"
                return t[:span[0]] + body + t[span[1]:]
            edit(p600, add_prompt, "M600 shows the guided colour-change prompt")
        else:   # tgalarm: one line in the printer's own prompt macro
            target = prompt_name or "_COLOUR_CHANGE_PROMPT"
            pp = _file_with(files, f"gcode_macro {target}", macros)

            def add_alarm(t):
                span = section_span(t, f"gcode_macro {target}")
                if span is None:
                    return add_missing(t)
                body = t[span[0]:span[1]].rstrip("\n")
                line = f'    RESPOND PREFIX=tgalarm MSG="{printer}: colour change - load the next colour, then RESUME"'
                return t[:span[0]] + body + "\n" + line + "\n\n" + t[span[1]:]
            edit(pp, add_alarm, f"colour change alerts Telegram (tgalarm line in {target})")
    elif rule.endswith(": insert does not resume"):
        sec = rule.split(":")[0]
        p = _file_with(files, sec, "printer.cfg")
        edit(p, lambda t: set_option(t, sec, "insert_gcode", "\n  M117 Filament inserted - LOAD and PURGE from the prompt"),
             f"[{sec}] insert_gcode no longer resumes")
    elif rule in ("idle_timeout is finite (<= 4 h)", "idle timeout is pause-aware"):
        p = _file_with(files, "idle_timeout", "printer.cfg")
        edit(p, lambda t: replace_section(t, "idle_timeout", idle_block(printer)), "paused-safe idle_timeout (30 min / 60 min)")
    elif rule == "12 h pause safety cap exists":
        edit(macros, lambda t: replace_section(t, "delayed_gcode _PAUSE_HARD_STOP", hard_stop_block(printer)),
             "12 h pause safety cap (_PAUSE_HARD_STOP)")
    return [v for v in out.values() if v["content"] != files.get(v["path"])]


def stage_fix(con, printer: str, url: str, rule: str, settings: dict, author: str) -> list[dict]:
    live = {f["path"]: configs.read_file(url, f["path"]).decode("utf-8", "replace") for f in configs.list_files(url)}
    staged = []
    for f in fixes_for(rule, settings, live, printer):
        staged.append(cfgchange.stage(con, printer, url, f["path"], new_content=f["content"],
                                      reason=f"farm baseline: {rule}", author=author))
    if not staged:
        raise cfgchange.Refused(f"no generated fix for '{rule}' - open the editor (Stage change) for this one")
    return staged
