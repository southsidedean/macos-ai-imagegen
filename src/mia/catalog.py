"""The models mia knows are good. Each is pinned to a commit, so "make it again" means the same weights.

A license attaches to the checkpoint, not the family, so each entry's was read from that repository.
FLUX.1 dev joins as an opt-in once it's been tried: it's gated, and non-commercial.
"""

from __future__ import annotations

from dataclasses import dataclass

from mia.engine import MiaError

OPEN, OPEN_WITH_TERMS, NON_COMMERCIAL, UNKNOWN = "open", "open with terms", "non-commercial", "unknown"


@dataclass(frozen=True)
class Model:
    id: str
    title: str
    family: str  # which class in the engine runs it
    repo: str
    revision: str
    license: str
    license_class: str
    download_gb: float
    steps: int
    guidance: float


MODELS = (
    Model(
        id="flux2-klein-4b-q4",
        title="FLUX.2 klein 4B, 4-bit",
        family="flux2-klein",
        repo="mflux-community/flux2-klein-4b-mflux-q4",
        revision="794cd159538149ad9830848508c31f0ea7088e58",
        license="Apache 2.0",
        license_class=OPEN,
        download_gb=4.6,
        steps=4,
        guidance=1.0,  # klein's distilled models accept nothing else
    ),
    Model(
        id="flux1-schnell-q4",
        title="FLUX.1 schnell, 4-bit",
        family="flux1",
        repo="mflux-community/flux-1-schnell-mflux-q4",
        revision="df1dada0a0e25bb8df6e1f7e4664959bf9ab86ce",
        license="Apache 2.0",
        license_class=OPEN,
        download_gb=9.6,
        steps=4,
        guidance=4.0,  # mflux's default for FLUX.1
    ),
)
DEFAULT = MODELS[0]


def find(model_id: str) -> Model:
    for model in MODELS:
        if model.id == model_id:
            return model
    known = ", ".join(model.id for model in MODELS)
    raise MiaError(f"there's no model called {model_id!r}; mia knows: {known}")
