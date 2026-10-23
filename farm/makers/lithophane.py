"""lithophane-maker (:5111): photo (and/or text) -> curved lithophane panel STL, optional base and LED stand."""
from pathlib import Path

from .. import config, net
from . import FileInput, GenContext, GenResult, Maker, MakerSpec, register
from .base import fit_problems, form_fields, http_error, model_out, safe_stem

N, B, S = "number", "boolean", "string"


class Lithophane(Maker):
    spec = MakerSpec(
        id="lithophane", name="Lithophane Maker (photo panels)", url=config.MAKER_URLS["lithophane"], mode="sync",
        timeout_s=900,
        inputs=(FileInput("image", ("image/*",), label="Photo"),
                FileInput("text_font_file", (".ttf", ".otf"), private=True, label="Font for the text (optional)")),
        params={
            "width_mm": {"type": N, "min": 10, "max": 600, "default": 120, "label": "Width mm"},
            "height_mm": {"type": N, "min": 10, "max": 600, "default": 105, "label": "Height mm"},
            "radius_mm": {"type": N, "min": 1, "max": 100000, "default": 80, "label": "Curve radius mm (big = flat)"},
            "t_min": {"type": N, "min": 0.1, "max": 10, "default": 1.2, "label": "Thinnest mm (light)"},
            "t_max": {"type": N, "min": 0.2, "max": 20, "default": 3.2, "label": "Thickest mm (dark)"},
            "resolution_mm": {"type": N, "min": 0.1, "max": 2, "default": 0.3, "advanced": True},
            "min_wall": {"type": N, "min": 0.1, "max": 10, "default": 1.2, "advanced": True},
            "fit_mode": {"type": "enum", "enum": ["cover", "contain", "stretch"], "default": "cover", "label": "Photo fit"},
            "border_style": {"type": S, "default": "none", "label": "Border style"},
            "frame_width": {"type": N, "min": 0, "max": 60, "default": 10, "advanced": True},
            "text": {"type": S, "default": "", "label": "Text on the panel", "personalise": True},
            "text_font": {"type": S, "advanced": True},
            "text_style": {"type": S, "advanced": True},
            "text_zone": {"type": S, "advanced": True},
            "auto_contrast": {"type": B, "default": True, "advanced": True},
            "contrast": {"type": N, "min": 0.5, "max": 3, "default": 1.1, "advanced": True},
            "gamma": {"type": N, "min": 0.2, "max": 5, "default": 1.0, "advanced": True},
            "sharpen": {"type": N, "min": 0, "max": 3, "default": 1.0, "advanced": True},
            "invert": {"type": B, "default": False, "advanced": True},
            "mirror": {"type": B, "default": False, "advanced": True},
            "relief_side": {"type": "enum", "enum": ["outer", "inner"], "default": "outer", "advanced": True},
            "watermark": {"type": "enum", "enum": ["keep", "gemini", "crop_bottom"], "default": "keep", "advanced": True},
            "base": {"type": B, "default": True, "label": "Base"},
            "solid_base": {"type": B, "default": True, "advanced": True},
            "helper_disks": {"type": B, "default": True, "advanced": True},
            "base_height": {"type": N, "min": 0, "max": 200, "default": 4, "advanced": True},
            "base_depth": {"type": N, "min": 1, "max": 200, "default": 12, "advanced": True},
            "stand": {"type": B, "default": False, "label": "LED stand"},
            "stand_scale": {"type": N, "min": 1, "max": 2.5, "default": 1.0, "advanced": True},
            "rotate90": {"type": B, "default": True, "advanced": True},
        },
        typical={"profiles": ["neo-hyperpla-028-litho"], "layer_heights": [0.2, 0.28], "material": "pla"},
    )

    def validate(self, params, files, ctx=None):
        problems = super().validate(params, files, ctx)
        if not files.get("image") and not str(params.get("text", "")).strip():
            problems.append("a photo, some text, or both")
        if params.get("t_max", 3.2) <= params.get("t_min", 1.2):
            problems.append("t_max must be thicker than t_min")
        if ctx:
            w, d, h = ctx.bed_mm
            if params.get("width_mm", 120) > max(w, d) or params.get("height_mm", 105) > h:
                problems.append(f"{params.get('width_mm', 120)} x {params.get('height_mm', 105)} mm doesn't fit "
                                f"this printer ({w:.0f} x {d:.0f}, {h:.0f} high)")
        return problems

    def generate(self, ctx: GenContext) -> GenResult:
        files = {k: Path(v) for k, v in ctx.files.items() if k in ("image", "text_font_file") and v}
        ctx.log(f"lithophane: POST {self.spec.url}/generate {ctx.params.get('width_mm', 120)}x"
                f"{ctx.params.get('height_mm', 105)} mm image={files.get('image', Path('-')).name}")
        try:
            status, headers, body = net.post_multipart(f"{self.spec.url}/generate", form_fields(ctx.params), files,
                                                       timeout=self.spec.timeout_s)
        except Exception as e:
            raise http_error(e, "lithophane /generate") from None
        info = {k: headers.get(k) for k in ("X-Litho-Triangles", "X-Litho-Seconds", "X-Litho-Chord", "X-Litho-ArcDeg")}
        stl = ctx.out_dir / f"{safe_stem(ctx.label, 'lithophane')}_litho.stl"
        stl.write_bytes(body)
        ctx.log(f"lithophane: {status} triangles={info['X-Litho-Triangles']} in {info['X-Litho-Seconds']} s")
        models = [model_out(stl, ctx.label or stl.stem)]
        warnings = fit_problems(models, ctx)
        if ctx.nozzle >= 0.6 and ctx.params.get("t_min", 1.2) < 1.2:
            warnings.append(f"t_min {ctx.params.get('t_min')} mm on a {ctx.nozzle} nozzle: thin areas may not print")
        return GenResult(models=models, stats=info, warnings=warnings)


register(Lithophane())
