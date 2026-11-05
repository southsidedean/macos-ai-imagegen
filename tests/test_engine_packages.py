"""The real engine's packages fit together. CI's macOS job installs them; no model ever runs here.

Each check is something that broke, or would break quietly, when mflux or a dependency moves.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

pytest.importorskip("mflux", reason="the engine extra isn't installed (uv sync --extra engine)")


def test_mflux_loads_on_a_pillow_with_the_security_fixes() -> None:
    # mflux 0.18 crashed at startup on Pillow 13, and older Pillow has 39 known vulnerabilities.
    from importlib.metadata import version

    from mflux.utils.image_util import ImageUtil

    assert ImageUtil
    assert int(version("pillow").split(".")[0]) >= 13


def test_mlx_is_the_version_the_expected_pixels_came_from() -> None:
    from importlib.metadata import version

    script = (Path(__file__).parent.parent / "scripts" / "real-engine.sh").read_text(encoding="utf-8")
    assert f"mlx {version('mlx')}" in script, "new mlx: update the expected pixels in real-engine.sh"
    assert f"mflux {version('mflux')}" in script, "new mflux: update the expected pixels in real-engine.sh"


def test_the_classes_and_arguments_mia_uses_are_there() -> None:
    from mflux.callbacks.callback_registry import CallbackRegistry
    from mflux.models.common.config import ModelConfig
    from mflux.models.flux.variants.txt2img.flux import Flux1
    from mflux.models.flux2.variants import Flux2Klein

    assert callable(ModelConfig.flux2_klein_4b) and callable(ModelConfig.schnell)
    assert hasattr(CallbackRegistry, "register")
    for model_class in (Flux1, Flux2Klein):
        parameters = inspect.signature(model_class.generate_image).parameters
        assert {"seed", "prompt", "num_inference_steps", "width", "height", "guidance"} <= set(parameters)
        assert "model_path" in inspect.signature(model_class.__init__).parameters


def test_the_step_loop_lets_mias_stop_through() -> None:
    # mia cancels by raising from the step callback, which works only while the loop catches nothing
    # broader than KeyboardInterrupt.
    from mflux.models.flux2.variants import Flux2Klein

    source = inspect.getsource(Flux2Klein.generate_image)
    assert "except KeyboardInterrupt" in source
    assert "except Exception" not in source


def test_the_engine_refuses_nothing_at_import_and_sets_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import mlx.core as mx

    from mia import catalog, mflux_engine

    assert callable(mx.set_cache_limit)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    made: dict[str, object] = {}

    class Stand:
        def __init__(self, **settings: object) -> None:
            made.update(settings)
            self.callbacks = type("R", (), {"register": lambda _s, _c: None})()

    monkeypatch.setattr("mflux.models.flux2.variants.Flux2Klein", Stand)
    engine = mflux_engine.MfluxEngine(catalog.DEFAULT, tmp_path)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert made["model_path"] == str(tmp_path)
    assert engine.version
