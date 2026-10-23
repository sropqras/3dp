"""holder-maker (:9111, work in progress): 1-4 photos -> BIC lighter holder STL. The only async maker:
upload images, create a holder job (202), poll it, then fetch holder.stl. Its sculpted roses need supports,
so only a profile with supports=True can take it - and its review needs extra care (experimental)."""
import json
import mimetypes
import time
from pathlib import Path

from .. import config, net
from . import FileInput, GenContext, GenResult, MakerError, Maker, MakerSpec, register
from .base import OVERRIDES, fit_problems, http_error, model_out, nested_params, safe_stem

N, S = "number", "string"


class Holder(Maker):
    spec = MakerSpec(
        id="holder", name="Holder Maker (lighter holders, WIP)", url=config.MAKER_URLS["holder"], mode="async",
        timeout_s=1800, poll_s=5,
        inputs=tuple(FileInput(f"image{i}", ("image/*",), required=(i == 1), label=f"Photo {i}") for i in range(1, 5)),
        params={
            "lighter": {"type": "enum", "enum": ["maxi", "mini", "slim", "custom"], "default": "maxi", "label": "Lighter"},
            "cutout": {"type": "boolean", "default": False,
                       "label": "Photos are already cut out (transparent PNG): use their transparency, skip auto-detection"},
            "clearance": {"type": N, "min": 0, "max": 2, "default": 0.35, "advanced": True},
            "fit": {"type": "enum", "enum": ["photo", "contain", "stretch"], "default": "photo", "advanced": True},
            "recipes": {"type": "json", "advanced": True,
                        "label": "Per-photo recipes {\"image1\": {...}} (optional: without one the subject is found automatically)"},
            "overrides": OVERRIDES,
        },
        typical={"profiles": ["neo-pla-020-supports"], "layer_heights": [0.2], "material": "pla"},
        supports=True, experimental=True,
    )

    def generate(self, ctx: GenContext) -> GenResult:
        deadline = time.time() + self.spec.timeout_s
        try:
            ids = []
            recipes = ctx.params.get("recipes") or {}
            for key, img in [(k, Path(v)) for k, v in sorted(ctx.files.items()) if k.startswith("image") and v]:
                ctype = mimetypes.guess_type(img.name)[0] or "image/png"
                _, _, body = net.post_bytes(f"{self.spec.url}/api/images", img.read_bytes(), ctype, timeout=120)
                entry = {"id": json.loads(body)["id"], "name": img.stem}
                if recipes.get(key):
                    entry["recipe"] = recipes[key]
                elif ctx.params.get("cutout"):
                    entry["recipe"] = {"matte": "alpha"}
                ids.append(entry)
            params = nested_params(self.spec.params, {k: v for k, v in ctx.params.items() if k not in ("recipes", "cutout")})
            _, _, body = net.post_json_body(f"{self.spec.url}/api/holders", {"images": ids, "params": params}, timeout=60)
            job = json.loads(body)
            ctx.log(f"holder: job {job['id']} queued ({len(ids)} photo(s))")
            while job.get("state") not in ("done", "failed"):
                if time.time() > deadline:
                    raise MakerError(f"holder job {job['id']} still {job.get('state')} after {self.spec.timeout_s} s",
                                     retryable=True)
                time.sleep(self.spec.poll_s)
                job = net.get_json(f"{self.spec.url}/api/holders/{job['id']}", timeout=30)   # fresh connection each poll
            if job["state"] == "failed":
                raise MakerError(f"holder job {job['id']} failed: {job.get('error') or job.get('stage')}")
            _, _, stl_bytes = net.get_bytes(f"{self.spec.url}/api/holders/{job['id']}/files/holder.stl", timeout=120)
        except MakerError:
            raise
        except Exception as e:
            raise http_error(e, "holder") from None
        report = job.get("report") or {}
        if report.get("watertight") is False:
            raise MakerError("holder returned a mesh that isn't watertight", retryable=False)
        stl = ctx.out_dir / f"{safe_stem(ctx.label, 'holder')}_holder.stl"
        stl.write_bytes(stl_bytes)
        models = [model_out(stl, ctx.label or stl.stem)]
        return GenResult(models=models, stats={"job": job["id"], **{k: report.get(k) for k in ("triangles", "volumeCm3")}},
                         warnings=fit_problems(models, ctx) + [str(w) for w in report.get("warnings", [])])


register(Holder())
