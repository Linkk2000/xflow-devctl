"""Compatibility entrypoint for the Task 5 command-capability checks."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("cockpit-commands.py")), run_name="__main__")
