"""Maintenance log and reminders, counted in the printer's own print hours (Moonraker history totals).

Each entry records what was done and the print hours at that moment; a kind is due when the printer has printed
its interval since the last entry. Intervals are farm-wide defaults ('*'), overridable per printer, editable in the
Printers tab. A kind never logged on a printer shows "no record" rather than guessing it's overdue.
"""
from . import db

KINDS = ("nozzle", "belts", "lubrication", "hotend_clean", "bed", "fans", "other")
DEFAULT_HOURS = {"nozzle": 300, "belts": 500, "lubrication": 200, "hotend_clean": 150, "bed": 1000, "fans": 800}


def intervals(con, printer: str) -> dict:
    out = dict(DEFAULT_HOURS)
    for p in ("*", printer):
        for kind, hours in con.execute("SELECT kind, hours FROM maintenance_intervals WHERE printer=?", (p,)):
            out[kind] = hours
    return out


def set_interval(con, kind: str, hours: float, printer: str = "*") -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if hours <= 0:
        con.execute("DELETE FROM maintenance_intervals WHERE printer=? AND kind=?", (printer, kind))
        return
    con.execute("INSERT INTO maintenance_intervals (printer, kind, hours) VALUES (?,?,?) "
                "ON CONFLICT(printer, kind) DO UPDATE SET hours=excluded.hours", (printer, kind, float(hours)))


def log(con, printer: str, kind: str, note: str, actor: str, print_hours: float | None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    cur = con.execute("INSERT INTO maintenance (printer, kind, note, actor, at, print_hours) VALUES (?,?,?,?,?,?)",
                      (printer, kind, (note or "").strip()[:500], actor, db.now(), print_hours))
    return db.row(con.execute("SELECT * FROM maintenance WHERE id=?", (cur.lastrowid,)).fetchone())


def entries(con, printer: str, limit: int = 30) -> list[dict]:
    return [db.row(r) for r in con.execute("SELECT * FROM maintenance WHERE printer=? ORDER BY id DESC LIMIT ?",
                                           (printer, limit))]


def status(con, printer: str, print_hours: float | None) -> list[dict]:
    """Per kind: hours since last done, the interval, and whether it's due."""
    iv = intervals(con, printer)
    out = []
    for kind in KINDS:
        last = con.execute("SELECT at, print_hours, note FROM maintenance WHERE printer=? AND kind=? ORDER BY id DESC "
                           "LIMIT 1", (printer, kind)).fetchone()
        hours = iv.get(kind)
        since = None
        if last and last["print_hours"] is not None and print_hours is not None:
            since = round(max(0.0, print_hours - last["print_hours"]), 1)
        out.append({"kind": kind, "interval": hours, "last_at": last["at"] if last else None,
                    "since_hours": since, "due": bool(hours and since is not None and since >= hours),
                    "soon": bool(hours and since is not None and hours * 0.9 <= since < hours),
                    "no_record": last is None})
    return out
