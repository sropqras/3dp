"""lamp-maker (:8111): up to four photos -> lithophane lamp shade; also its plaque maker (/plaque).

The shade is always requested as STL: the 3MF lamp-maker writes carries an Ender 3 V3 SE 0.4 profile that fits
no farm printer, and the farm slices with its own profiles anyway. A shade wider than the bed comes back as
bed-sized sections (zip) - each section its own bed.
"""
import json
from pathlib import Path

from .. import config, net
from . import FileInput, GenContext, GenResult, MakerError, Maker, MakerSpec, register
from .base import (OVERRIDES, defaults_from, fit_problems, http_error, model_out, nested_params, safe_stem,
                   unzip_stls)

N, I, B, S = "number", "integer", "boolean", "string"


def _post_parameters(url, parameters, files, timeout, what):
    try:
        return net.post_multipart(url, {"parameters": json.dumps(parameters)}, files, timeout=timeout)
    except Exception as e:
        raise http_error(e, what) from None


def _models_from(headers, body, out_dir: Path, stem: str, label: str):
    if "zip" in headers.get("Content-Type", "") or body[:2] == b"PK":
        (out_dir / f"{stem}.zip").write_bytes(body)
        stls = unzip_stls(body, out_dir / f"{stem}_parts")
        return [model_out(s, f"{label} {s.stem}".strip()) for s in stls]
    stl = out_dir / f"{stem}.stl"
    stl.write_bytes(body)
    return [model_out(stl, label or stem)]


class Lamp(Maker):
    spec = MakerSpec(
        id="lamp", name="Lamp Maker (lithophane lamp shades)", url=config.MAKER_URLS["lamp"], mode="sync",
        timeout_s=900,
        inputs=tuple(FileInput(f"fileToUpload{i}", ("image/*",), required=(i == 1), label=f"Photo {i}")
                     for i in range(1, 5)),
        params={
            "top_diameter": {"type": N, "path": "lithophane.topDiameter", "min": 40, "max": 400, "label": "Top diameter mm"},
            "bottom_diameter": {"type": N, "path": "lithophane.bottomDiameter", "min": 40, "max": 400, "label": "Bottom diameter mm"},
            "shade_height": {"type": N, "path": "lithophane.shadeHeight", "min": 40, "max": 400, "label": "Shade height mm"},
            "wave_num": {"type": I, "path": "lithophane.waveNum", "min": 0, "max": 20, "label": "Waves"},
            "wave_size": {"type": N, "path": "lithophane.waveSize", "advanced": True},
            "min_thickness": {"type": N, "path": "lithophane.minThickness", "min": 0.2, "max": 5, "advanced": True},
            "max_thickness": {"type": N, "path": "lithophane.maxThickness", "min": 0.5, "max": 10, "advanced": True},
            "resolution": {"type": N, "path": "lithophane.resolution", "min": 0.1, "max": 1, "advanced": True},
            "overhang_angle": {"type": N, "path": "lithophane.overhangAngle", "min": 10, "max": 60, "advanced": True},
            "border_style": {"type": S, "path": "borders.style", "label": "Border style"},
            "overrides": OVERRIDES,
        },
        typical={"profiles": ["neo-hyperpla-028-litho"], "layer_heights": [0.2, 0.28], "material": "pla"},
    )

    def defaults(self):
        return {**super().defaults(), "_maker": defaults_from(f"{self.spec.url}/api/defaults", "defaults")}

    def generate(self, ctx: GenContext) -> GenResult:
        base = defaults_from(f"{self.spec.url}/api/defaults", "defaults")
        p = nested_params(self.spec.params, ctx.params, base)
        w, d, h = ctx.bed_mm
        lit = p.get("lithophane", {})
        widest = max(float(lit.get("topDiameter", 0) or 0), float(lit.get("bottomDiameter", 0) or 0))
        p["split"] = {**p.get("split", {}), "bedWidthMm": w, "bedDepthMm": d, "bedMarginMm": 5, "maxHeightMm": h,
                      "enabled": bool(p.get("split", {}).get("enabled")) or widest > min(w, d) - 10}
        p["output"] = {**p.get("output", {}), "format": "stl"}
        files = {k: Path(v) for k, v in ctx.files.items() if k.startswith("fileToUpload") and v}
        ctx.log(f"lamp: POST {self.spec.url}/api/generate-stl photos={len(files)} split={p['split']['enabled']}")
        status, headers, body = _post_parameters(f"{self.spec.url}/api/generate-stl", p, files, self.spec.timeout_s,
                                                 "lamp /api/generate-stl")
        models = _models_from(headers, body, ctx.out_dir, safe_stem(ctx.label, "lampshade"), ctx.label)
        ctx.log(f"lamp: {status} {len(models)} part(s)")
        return GenResult(models=models, stats={"parts": len(models), "split": p["split"]["enabled"]},
                         warnings=fit_problems(models, ctx))


class Plaque(Maker):
    spec = MakerSpec(
        id="plaque", name="Plaque Maker (lamp-maker /plaque)", url=config.MAKER_URLS["lamp"], mode="sync",
        timeout_s=300,
        inputs=(FileInput("image", ("image/*",), label="Image (optional)"),
                FileInput("font", (".ttf", ".otf"), private=True, label="Font file (optional)")),
        params={
            "text": {"type": S, "label": "Text", "personalise": True},
            "font_size_mm": {"type": N, "min": 5, "max": 300, "label": "Font size mm"},
            "letter_height_mm": {"type": N, "min": 0.4, "max": 30, "label": "Letter height mm"},
            "base": {"type": B, "default": True, "label": "Base plate"},
            "base_thickness_mm": {"type": N, "min": 0.4, "max": 30, "default": 2, "label": "Base thickness mm"},
            "base_margin_mm": {"type": N, "min": 0, "max": 100, "label": "Margin mm"},
            "base_corner_style": {"type": "enum", "enum": ["square", "round"], "label": "Corners"},
            "base_corner_radius_mm": {"type": N, "min": 0, "max": 100, "advanced": True},
            "overrides": OVERRIDES,
        },
        typical={"profiles": ["neo-petg-036"], "layer_heights": [0.4], "material": "petg"},
        colour_changes="farm", colour_heights=("base_top",),
    )

    def generate(self, ctx: GenContext) -> GenResult:
        base = defaults_from(f"{self.spec.url}/api/plaque-defaults", "defaults")
        p = nested_params(self.spec.params, ctx.params, base)
        p.update(bed_width_mm=ctx.bed_mm[0], bed_depth_mm=ctx.bed_mm[1])
        files = {k: Path(v) for k, v in ctx.files.items() if k in ("image", "font") and v}
        ctx.log(f"plaque: POST {self.spec.url}/api/generate-plaque text={p.get('text')!r}")
        status, headers, body = _post_parameters(f"{self.spec.url}/api/generate-plaque", p, files, self.spec.timeout_s,
                                                 "plaque /api/generate-plaque")
        models = _models_from(headers, body, ctx.out_dir, safe_stem(p.get("text") or ctx.label, "plaque"), ctx.label)
        info = {k: headers.get(k) for k in ("X-Plaque-Pieces", "X-Plaque-Triangles", "X-Plaque-Forced-Cuts")}
        colour_zs = {"base_top": float(p.get("base_thickness_mm", 2.0))} if p.get("base", True) else {}
        return GenResult(models=models, colour_zs=colour_zs, stats=info, warnings=fit_problems(models, ctx))


register(Lamp())
register(Plaque())
