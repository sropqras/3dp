"""namakdani-maker (:6111): an STL/3MF/photo -> namak dani (salt dish: plinth + bowl) STL.

Two calls in one generate(): upload (the maker keeps it in memory, last 6 only - so the id is never stored and
a retry uploads again), then build as STL (its 3MF has a negative-volume bowl that needs the slicer's boolean).
"""
import json
import urllib.parse
from pathlib import Path

from .. import config, net
from . import FileInput, GenContext, GenResult, MakerError, Maker, MakerSpec, register
from .base import OVERRIDES, fit_problems, http_error, model_out, nested_params, safe_stem

N, B, S = "number", "boolean", "string"


class Namakdani(Maker):
    spec = MakerSpec(
        id="namakdani", name="Namak Dani Maker (salt dishes)", url=config.MAKER_URLS["namakdani"], mode="sync",
        timeout_s=600,
        inputs=(FileInput("file", (".stl", ".3mf", "image/*"), required=True, label="Shape: STL, 3MF or photo"),),
        params={
            "widthMm": {"type": N, "min": 20, "max": 300, "default": 60, "label": "Width mm"},
            "lengthMm": {"type": N, "min": 20, "max": 300, "label": "Length mm (blank = keep ratio)"},
            "heightMm": {"type": N, "min": 5, "max": 200, "label": "Height mm (blank = auto)"},
            "bowl": {"type": B, "default": True, "label": "Bowl"},
            "wallMm": {"type": N, "min": 1, "max": 20, "default": 3.7, "advanced": True},
            "lipMm": {"type": N, "min": 0, "max": 20, "default": 2, "advanced": True},
            "shape": {"type": S, "default": "rect", "advanced": True},
            "resolutionMm": {"type": N, "min": 0.1, "max": 2, "default": 0.25, "advanced": True},
            "overrides": OVERRIDES,
        },
        typical={"profiles": ["neo-hyperpla-028-litho"], "layer_heights": [0.2, 0.28], "material": "pla"},
    )

    def generate(self, ctx: GenContext) -> GenResult:
        src = Path(ctx.files["file"])
        try:
            _, _, body = net.post_multipart(f"{self.spec.url}/api/upload", {}, {"file": src}, timeout=120)
            upload = json.loads(body)
            params = nested_params(self.spec.params, ctx.params)
            ctx.log(f"namakdani: uploaded {src.name} as {upload.get('id')} ({upload.get('kind')}); building STL")
            status, headers, stl_bytes = net.post_json_body(
                f"{self.spec.url}/api/build", {"id": upload["id"], "params": params, "format": "stl"},
                timeout=self.spec.timeout_s)
        except MakerError:
            raise
        except Exception as e:
            raise http_error(e, "namakdani upload/build") from None
        stats = {}
        if headers.get("X-Namakdani-Stats"):
            try:
                stats = json.loads(urllib.parse.unquote(headers["X-Namakdani-Stats"]))
            except ValueError:
                stats = {"raw": headers["X-Namakdani-Stats"][:200]}
        stl = ctx.out_dir / f"{safe_stem(ctx.label or src.stem, 'namakdani')}_namakdani.stl"
        stl.write_bytes(stl_bytes)
        models = [model_out(stl, ctx.label or stl.stem)]
        warnings = fit_problems(models, ctx) + [str(w) for w in stats.get("warnings", [])]
        if stats.get("watertight") is False:
            raise MakerError("namakdani returned a mesh that isn't watertight", retryable=False)
        return GenResult(models=models, stats=stats, warnings=warnings)


register(Namakdani())
