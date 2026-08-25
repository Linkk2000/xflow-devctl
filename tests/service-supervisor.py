"""Compatibility entrypoint for the Task 6 service supervisor tests."""

from __future__ import annotations

import runpy
from pathlib import Path


runpy.run_path(str(Path(__file__).with_name("cockpit-services.py")), run_name="__main__")
