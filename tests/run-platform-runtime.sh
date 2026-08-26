#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

PYTHON39=${PYTHON39:-/usr/bin/python3}
PYTHON_CURRENT=${PYTHON_CURRENT:-python3}

for python in "$PYTHON39" "$PYTHON_CURRENT"; do
  "$python" tests/portable-runtime.py
  "$python" tests/python-core.py
  "$python" tests/posix-launcher.py
  "$python" tests/entrypoint-routing.py
  "$python" tests/cockpit-profile.py
  "$python" tests/cockpit-commands.py
  "$python" tests/cockpit-services.py
  "$python" tests/cockpit-cli.py
done

if "$PYTHON_CURRENT" tests/powershell-entrypoint.py; then
  :
else
  powershell_status=$?
  [ "$powershell_status" -eq 77 ] || exit "$powershell_status"
fi

git diff --check
