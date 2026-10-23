"""Maker plugins: every maker app (likhan, lithophane, lamp, namakdani, holder) behind one contract.

A maker turns parameters (+ uploaded files) into printable models for ONE job (item). The farm owns
everything after that: which bed, which profile, slicing, colour changes, the gate, the printer.

Contract `farm-maker/1`:
  spec        what the maker is, how to call it, which inputs/params it takes, what it typically prints with
  health()    is the maker app up (GET /health)
  defaults()  param defaults (spec, overlaid by the app's own /api/defaults where it has one)
  validate()  pure checks of params/files against the spec and the target bed, before any call is made
  generate()  call the app, write models to ctx.out_dir, return GenResult - may block up to spec.timeout_s

Adapters live in this package (one file per maker) and register in MAKERS. A maker app may later serve
GET /farm-maker.json (same shape as MakerSpec) so its schema lives with its code; the adapter stays
authoritative until then.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CONTRACT = "farm-maker/1"


class MakerError(Exception):
    """A maker call failed. retryable: worth trying again later (5xx, timeout, connection) vs a bad request."""

    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


@dataclass(frozen=True)
class FileInput:
    name: str                  # request field
    accept: tuple              # e.g. ("image/*",) or (".ttf", ".otf")
    required: bool = False
    private: bool = False      # licensed/customer files: never served back by Farm Control
    label: str = ""


@dataclass(frozen=True)
class MakerSpec:
    id: str
    name: str
    url: str                   # base URL (config.MAKER_URLS)
    mode: str                  # "sync" | "async"
    timeout_s: int
    params: dict               # {name: {"type": number|integer|string|boolean|enum, "default", "min", "max",
                               #         "enum": [...], "label", "personalise": bool, "advanced": bool}}
    inputs: tuple = ()
    poll_s: int = 0
    typical: dict = field(default_factory=dict)   # {"profiles": [...], "layer_heights": [...], "material": ...}
    colour_changes: str = "none"                   # "none" | "farm": the farm may insert M600 at named heights
    colour_heights: tuple = ()                     # names generate() can return, e.g. ("base_top",)
    supports: bool = False
    experimental: bool = False


@dataclass
class GenContext:
    params: dict
    files: dict                # {input name: Path}
    bed_mm: tuple              # usable (width, depth, max_z) of the target printer
    nozzle: float
    layer_height: float
    out_dir: Path
    log: Callable[[str], None] = print
    label: str = ""


@dataclass
class ModelOut:
    path: Path
    label: str
    bbox: tuple                # ((x0, y0, z0), (x1, y1, z1))
    triangles: int


@dataclass
class GenResult:
    models: list               # [ModelOut]
    colour_zs: dict = field(default_factory=dict)   # {"base_top": 4.0}
    stats: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    maker_version: str = ""


class Maker:
    spec: MakerSpec

    def health(self) -> bool:
        from .. import net
        try:
            net.get_json(f"{self.spec.url}/health", timeout=5)
            return True
        except Exception:
            return False

    def defaults(self) -> dict:
        return {k: v.get("default") for k, v in self.spec.params.items() if "default" in v}

    def validate(self, params: dict, files: dict, ctx: GenContext | None = None) -> list[str]:
        """Spec-driven checks; adapters extend with maker-specific ones (e.g. does it fit the bed)."""
        problems = []
        for name, p in self.spec.params.items():
            if name not in params:
                continue
            v, t = params[name], p.get("type")
            if t in ("number", "integer"):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    problems.append(f"{name}: expected a number, got {v!r}")
                    continue
                if "min" in p and v < p["min"] or "max" in p and v > p["max"]:
                    problems.append(f"{name}: {v} outside [{p.get('min')}, {p.get('max')}]")
            elif t == "enum" and v not in p.get("enum", ()):
                problems.append(f"{name}: {v!r} not one of {p.get('enum')}")
            elif t == "json" and not isinstance(v, dict):
                problems.append(f"{name}: expected a JSON object")
            elif t == "string" and not isinstance(v, str):
                problems.append(f"{name}: expected text")
            elif t == "boolean" and not isinstance(v, bool):
                problems.append(f"{name}: expected true/false, got {v!r}")
        unknown = sorted(set(params) - set(self.spec.params))
        if unknown:
            problems.append(f"unknown parameter(s) for {self.spec.id}: {', '.join(unknown)}")
        for fi in self.spec.inputs:
            if fi.required and not files.get(fi.name):
                problems.append(f"missing file: {fi.name}")
        return problems

    def colour_z(self, name: str, params: dict) -> float | None:
        """The height (mm) of a named colour change for these params, known BEFORE generating (so an order whose
        layer height can't put it on a layer top is refused up front). None: only the generated model knows."""
        if name == "base_top" and "base_thickness_mm" in self.spec.params:
            return float(params.get("base_thickness_mm", self.spec.params["base_thickness_mm"].get("default", 2.0)))
        return None

    def accepts(self, input_name: str, filename: str) -> bool:
        """Does this file suit that input (an image input takes images, a font input takes .ttf/.otf, ...)."""
        fi = next((f for f in self.spec.inputs if f.name == input_name), None)
        if fi is None:
            return False
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        images = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
        for a in fi.accept:
            if a == "image/*" and ext in images or a.lower() == ext:
                return True
        return False

    def generate(self, ctx: GenContext) -> GenResult:
        raise NotImplementedError

    def manifest(self) -> dict:
        """This adapter's spec in the /farm-maker.json shape (what a maker app could serve itself)."""
        s = self.spec
        return {"contract": CONTRACT, "id": s.id, "name": s.name, "mode": s.mode, "timeout_s": s.timeout_s,
                "inputs": [vars(i) for i in s.inputs], "params": s.params, "typical": s.typical,
                "colour_changes": s.colour_changes, "colour_heights": list(s.colour_heights),
                "supports": s.supports, "experimental": s.experimental}


MAKERS: dict[str, Maker] = {}


def register(maker: Maker) -> Maker:
    MAKERS[maker.spec.id] = maker
    return maker


def get(maker_id: str) -> Maker:
    _load()
    try:
        return MAKERS[maker_id]
    except KeyError:
        raise SystemExit(f"unknown maker {maker_id!r}; known: {', '.join(MAKERS)}")


def all_makers() -> dict:
    _load()
    return dict(MAKERS)


def _load():
    # each adapter registers itself on import
    from . import holder, lamp, likhan, lithophane, namakdani  # noqa: F401
