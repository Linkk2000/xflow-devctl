"""Public Python entrypoint; command routing remains centralized in :mod:`xflow.cli`."""

import sys


try:
    from .cli import main
except ModuleNotFoundError as exc:
    if exc.name != "yaml":
        raise
    print(
        "[ERROR] dependency checks require PyYAML; run: python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from None

raise SystemExit(main())
