"""What every engine does, and what a request may ask for. Importing this loads no ML package."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_PROMPT = 4000
MIN_SIDE, MAX_SIDE, SIDE_STEP = 256, 2048, 16  # the models work in blocks of 16 pixels
MAX_STEPS = 50
MAX_SEED = 2**32 - 1

# Called with the step that has just started (from 1), and how many there are. Only "started" can be
# known: MLX evaluates lazily, so mflux reports a step before computing it, and the longest wait comes
# after the last report (the final step and the decode).
Progress = Callable[[int, int], None]


class MiaError(Exception):
    """A problem to tell the person about, in a sentence, with no traceback."""


class Cancelled(MiaError):
    """The job was stopped on request. No image was kept."""


@dataclass(frozen=True)
class Request:
    prompt: str
    seed: int
    width: int
    height: int
    steps: int

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise MiaError("the prompt is empty")
        if len(self.prompt) > MAX_PROMPT:
            raise MiaError(f"the prompt is too long: the limit is {MAX_PROMPT} characters")
        if not 0 <= self.seed <= MAX_SEED:
            raise MiaError(f"the seed must be from 0 to {MAX_SEED}")
        for name, side in (("width", self.width), ("height", self.height)):
            if not MIN_SIDE <= side <= MAX_SIDE or side % SIDE_STEP:
                raise MiaError(f"the {name} must be a multiple of {SIDE_STEP} from {MIN_SIDE} to {MAX_SIDE}")
        if not 1 <= self.steps <= MAX_STEPS:
            raise MiaError(f"the steps must be from 1 to {MAX_STEPS}")


def random_seed() -> int:
    return secrets.randbelow(MAX_SEED + 1)


class Engine(Protocol):
    """An engine with one model loaded, ready for any number of jobs."""

    name: str
    version: str

    def generate(self, request: Request, output: Path, progress: Progress, cancelled: Callable[[], bool]) -> str:
        """Writes a PNG with no metadata, and returns the SHA-256 of its pixels.

        File hashes aren't comparable across engines or settings, and pixels are what "the same
        image" means. Raises Cancelled when `cancelled` turns true, which is checked between steps.
        """
        ...
