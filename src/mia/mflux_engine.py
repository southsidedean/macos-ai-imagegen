"""The real engine: mflux on MLX. Everything specific to mflux stays here, because its API moves.

This module is imported only when a real model is about to run, so nothing else pays to load it. What
it does comes from tests on a real Mac, recorded in PLAN.md under "Spike results so far".
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

from mia.catalog import Model
from mia.engine import Cancelled, MiaError, Progress, Request

# MLX's reusable cache grows to about 20 GB by default, and on a Mac doing other work that made jobs
# take 35 to 160 seconds. At this size every job took 32, with the same pixels.
CACHE_LIMIT = 2 * 1024**3


class _Stop(Exception):
    """Raised from the step callback. mflux's loop catches only KeyboardInterrupt, so this ends the job."""


class _Steps:
    """mflux's step callback. It fires as a step starts, because MLX evaluates lazily."""

    def __init__(self) -> None:
        self.progress: Progress = lambda _step, _total: None
        self.cancelled: Callable[[], bool] = lambda: False
        self.total = 0

    def call_in_loop(self, t: int, **_: Any) -> None:
        self.progress(t + 1, self.total)
        if self.cancelled():
            raise _Stop(f"stopped at step {t + 1} of {self.total}")


class MfluxEngine:
    name = "mflux"

    def __init__(self, model: Model, folder: Path) -> None:
        # Only the model manager downloads: a job must never start one. This has to be set before
        # huggingface_hub is imported, which reads it once.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        try:
            import mlx.core as mx
            from mflux.models.common.config import ModelConfig
            from mflux.models.flux.variants.txt2img.flux import Flux1
            from mflux.models.flux2.variants import Flux2Klein
            from mflux.utils.exceptions import StopImageGenerationException
        except ImportError as missing:
            raise MiaError(f"the image engine isn't installed ({missing.name}): run uv sync --extra engine") from missing
        # Both, because the pixels depend on both: see the pin in pyproject.toml.
        self.version = f"{version('mflux')}, mlx {version('mlx')}"
        self._model = model
        self._interrupted = StopImageGenerationException
        mx.set_cache_limit(CACHE_LIMIT)
        if model.family == "flux2-klein":
            self._loaded = Flux2Klein(model_config=ModelConfig.flux2_klein_4b(), model_path=str(folder))
        elif model.family == "flux1":
            self._loaded = Flux1(model_config=ModelConfig.schnell(), model_path=str(folder))
        else:
            raise MiaError(f"this version of mia can't run {model.family} models")
        self._steps = _Steps()
        self._loaded.callbacks.register(self._steps)

    def generate(self, request: Request, output: Path, progress: Progress, cancelled: Callable[[], bool]) -> str:
        self._steps.progress, self._steps.cancelled, self._steps.total = progress, cancelled, request.steps
        try:
            result = self._loaded.generate_image(
                seed=request.seed,
                prompt=request.prompt,
                num_inference_steps=request.steps,
                width=request.width,
                height=request.height,
                guidance=self._model.guidance,
            )
        except _Stop as stopped:
            raise Cancelled(str(stopped)) from stopped
        except self._interrupted as stopped:  # Ctrl-C, which mflux's loop turns into this
            raise Cancelled("stopped") from stopped
        image = result.image
        # mflux's own save writes the prompt, the model's path, and the home folder into EXIF and XMP.
        # Saving the plain picture ourselves, with its info cleared, writes none of that.
        image.info.clear()
        image.save(output, format="PNG")
        return hashlib.sha256(image.tobytes()).hexdigest()
