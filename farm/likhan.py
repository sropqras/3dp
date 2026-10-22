"""Stage 1: ask the likhan-maker server for the model (single STL, or a zip of bed-sized pieces)."""
import re
import zipfile
from pathlib import Path

from . import net


def _safe_stem(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "model"


def generate(run, server: str, params: dict, font_file: str | None) -> list[Path]:
    fields = {k: ("1" if v is True else "0" if v is False else v) for k, v in params.items()}
    files = {}
    if font_file:
        font = Path(font_file)
        if not font.is_file():
            run.fail(f"font file not found: {font}")
        files["font"] = font
        fields["font_name"] = "custom"

    run.log(f"likhan: POST {server}/generate text={params.get('text')!r} font={Path(font_file).name if font_file else params.get('font_name')}")
    status, headers, body = net.post_multipart(f"{server}/generate", fields, files)
    info = {k: headers.get(k) for k in ("X-Likhan-Pieces", "X-Likhan-Width", "X-Likhan-Height", "X-Likhan-Triangles")}
    run.log(f"likhan: {status} pieces={info['X-Likhan-Pieces']} size={info['X-Likhan-Width']}x{info['X-Likhan-Height']} mm "
            f"triangles={info['X-Likhan-Triangles']}")

    ctype = headers.get("Content-Type", "")
    if "zip" in ctype:
        zpath = run.path("likhan_pieces.zip")
        zpath.write_bytes(body)
        out = run.path("pieces")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(out)
        stls = sorted(out.glob("*.stl"))
    else:
        # the STL name becomes the EXCLUDE_OBJECT label and the G-code file name
        stl = run.path(f"{_safe_stem(params.get('text') or 'model')}.stl")
        stl.write_bytes(body)
        stls = [stl]

    run.step("likhan", server=server, params=params, font_file=font_file, response=info, stls=[str(s) for s in stls])
    return stls
