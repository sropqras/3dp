"""Stage 1 (legacy entry point): ask the likhan-maker server for the model. The request logic now lives in the
maker adapter (farm/makers/likhan.py); this keeps the old call shape for scripts that use it."""
from pathlib import Path

from .makers import GenContext, MakerError, get


def generate(run, server: str, params: dict, font_file: str | None) -> list[Path]:
    if font_file and not Path(font_file).is_file():
        run.fail(f"font file not found: {font_file}")
    maker = get("likhan")
    ctx = GenContext(params=params, files={"font": font_file} if font_file else {},
                     bed_mm=(params.get("bed_width_mm", 220), params.get("bed_depth_mm", 220), 1e9),
                     nozzle=0, layer_height=0, out_dir=run.dir, log=run.log)
    try:
        res = maker.generate(ctx)
    except MakerError as e:
        run.fail(str(e))
    run.step("likhan", server=server, params=params, font_file=font_file, response=res.stats,
             stls=[str(m.path) for m in res.models])
    return [m.path for m in res.models]
