"""likhan-maker (:4111): text or image -> raised-letter plate STL, auto-split into bed-sized pieces (zip)."""
from pathlib import Path

from .. import config, net
from . import FileInput, GenContext, GenResult, Maker, MakerSpec, register
from .base import fit_problems, form_fields, fresh_dir, fresh_file, http_error, model_out, safe_stem, unzip_stls

N, I, B, S = "number", "integer", "boolean", "string"


class Likhan(Maker):
    spec = MakerSpec(
        id="likhan", name="Likhan Maker (plates & lettering)", url=config.MAKER_URLS["likhan"], mode="sync",
        timeout_s=300,
        inputs=(FileInput("font", (".ttf", ".otf"), private=True, label="Font file (licensed fonts by name)"),
                FileInput("image", ("image/*",), label="Image (image mode)")),
        params={
            "mode": {"type": "enum", "enum": ["text", "image"], "default": "text", "label": "Mode"},
            "text": {"type": S, "label": "Text", "personalise": True},
            "font_name": {"type": S, "label": "Font (server list)", "advanced": True},
            "font_size_mm": {"type": N, "min": 5, "max": 300, "default": 30, "label": "Font size mm"},
            "target_width_mm": {"type": N, "min": 5, "max": 2000, "label": "Target width mm", "advanced": True},
            "letter_height_mm": {"type": N, "min": 0.4, "max": 30, "default": 3, "label": "Letter height mm"},
            "monospace": {"type": B, "default": False, "label": "Fixed-width characters"},
            "image_threshold": {"type": I, "min": 0, "max": 255, "advanced": True},
            "image_invert": {"type": B, "advanced": True},
            "image_target_width_mm": {"type": N, "min": 5, "max": 2000, "advanced": True},
            "image_target_height_mm": {"type": N, "min": 5, "max": 2000, "advanced": True},
            "image_clean_background": {"type": B, "advanced": True},
            "image_enhance_resolution": {"type": B, "advanced": True},
            "base": {"type": B, "default": True, "label": "Base plate"},
            "base_corner_style": {"type": "enum", "enum": ["square", "round", "chamfer"], "default": "square",
                                  "label": "Corners"},
            "base_corner_radius_mm": {"type": N, "min": 0, "max": 100, "default": 4, "label": "Corner radius mm"},
            "base_thickness_mm": {"type": N, "min": 0.4, "max": 30, "default": 2, "label": "Base thickness mm"},
            "base_margin_mm": {"type": N, "min": 0, "max": 100, "default": 5, "label": "Margin mm"},
            "base_width_mm": {"type": N, "min": 10, "max": 2000, "label": "Fixed plate width mm"},
            "base_depth_mm": {"type": N, "min": 10, "max": 2000, "label": "Fixed plate depth mm"},
            "text_embed_mm": {"type": N, "min": 0, "max": 5, "default": 0.2, "advanced": True},
            "bed_margin_mm": {"type": N, "min": 0, "max": 50, "default": 5, "advanced": True},
            "auto_split": {"type": B, "default": True, "advanced": True},
            "min_piece_mm": {"type": N, "min": 5, "max": 200, "default": 15, "advanced": True},
        },
        typical={"profiles": ["neo-petg-036"], "layer_heights": [0.4], "material": "petg"},
        colour_changes="farm", colour_heights=("base_top",),
    )

    def validate(self, params, files, ctx=None):
        problems = super().validate(params, files, ctx)
        if params.get("mode", "text") == "text" and not str(params.get("text", "")).strip():
            problems.append("text: required in text mode")
        if params.get("mode") == "image" and not files.get("image"):
            problems.append("missing file: image (image mode)")
        return problems

    def generate(self, ctx: GenContext) -> GenResult:
        params = dict(ctx.params)
        # size pieces for the printer it will actually print on, not the form default
        params["bed_width_mm"], params["bed_depth_mm"] = ctx.bed_mm[0], ctx.bed_mm[1]
        fields = form_fields(params)
        files = {}
        if ctx.files.get("font"):
            files["font"] = Path(ctx.files["font"])
            fields["font_name"] = "custom"
        if ctx.files.get("image"):
            files["image"] = Path(ctx.files["image"])
        ctx.log(f"likhan: POST {self.spec.url}/generate text={params.get('text')!r} "
                f"font={files['font'].name if 'font' in files else params.get('font_name')}")
        try:
            status, headers, body = net.post_multipart(f"{self.spec.url}/generate", fields, files,
                                                       timeout=self.spec.timeout_s)
        except Exception as e:
            raise http_error(e, "likhan /generate") from None
        info = {k: headers.get(k) for k in ("X-Likhan-Pieces", "X-Likhan-Width", "X-Likhan-Height",
                                             "X-Likhan-Triangles")}
        ctx.log(f"likhan: {status} pieces={info['X-Likhan-Pieces']} size={info['X-Likhan-Width']}x"
                f"{info['X-Likhan-Height']} mm triangles={info['X-Likhan-Triangles']}")
        stem = safe_stem(params.get("text") or "model")
        if "zip" in headers.get("Content-Type", ""):
            pieces = fresh_dir(ctx.out_dir / "pieces")
            fresh_file(ctx.out_dir / f"{stem}_pieces.zip", body)
            stls = unzip_stls(body, pieces)
        else:
            # the STL name becomes the EXCLUDE_OBJECT label and the G-code file name
            stls = [fresh_file(ctx.out_dir / f"{stem}.stl", body)]
        models = [model_out(s, ctx.label or s.stem) for s in stls]
        colour_zs = {"base_top": float(params.get("base_thickness_mm", 2.0))} if params.get("base", True) else {}
        return GenResult(models=models, colour_zs=colour_zs, stats=info, warnings=fit_problems(models, ctx))


register(Likhan())
