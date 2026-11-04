"""The mia command. Every other way into mia does what this does, through the same API."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from mia import __version__, doctor


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mia", description="Generate AI images on your own Mac.")
    parser.add_argument("--version", action="version", version=f"mia {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="is this machine ready? changes nothing")
    parser.parse_args(argv)
    return doctor.run()
