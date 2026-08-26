#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

PYTHON39=${PYTHON39:-python3.9}
PYTHON_CURRENT=${PYTHON_CURRENT:-python3}
BASE_REF=${BASE_REF:-origin/main}

preflight_python() {
  label=$1
  candidate=$2
  expected=$3

  if [ -z "$candidate" ]; then
    printf '[ERROR] %s interpreter is empty; set %s.\n' "$label" "$label" >&2
    return 1
  fi
  if ! command -v "$candidate" >/dev/null 2>&1 && [ ! -x "$candidate" ]; then
    printf '[ERROR] %s interpreter is not executable or not on PATH: %s\n' "$label" "$candidate" >&2
    return 1
  fi

  if probe_output=$("$candidate" -c '
import importlib
import os
import platform
import sys

label = sys.argv[1]
expected = sys.argv[2]
major_minor = sys.version_info[:2]
print(
    "DEVCTL_PREFLIGHT label={label} path={path} version={version}".format(
        label=label,
        path=os.path.realpath(sys.executable),
        version=platform.python_version(),
    )
)

if major_minor < (3, 9):
    print(
        "[ERROR] {label} requires Python 3.9 or newer; got {version}".format(
            label=label, version=platform.python_version()
        ),
        file=sys.stderr,
    )
    raise SystemExit(3)
if expected == "3.9" and major_minor != (3, 9):
    print(
        "[ERROR] PYTHON39 must be Python 3.9.x; got {version}".format(
            version=platform.python_version()
        ),
        file=sys.stderr,
    )
    raise SystemExit(4)
if expected == "current" and major_minor == (3, 9):
    print(
        "[ERROR] PYTHON_CURRENT must differ from Python 3.9 major.minor; got {version}".format(
            version=platform.python_version()
        ),
        file=sys.stderr,
    )
    raise SystemExit(5)

missing = []
for module_name in ("xflow", "yaml", "jsonschema", "PIL"):
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        missing.append("{name} ({kind}: {detail})".format(
            name=module_name,
            kind=type(exc).__name__,
            detail=exc,
        ))
if missing:
    print(
        "[ERROR] {label} missing required imports: {missing}".format(
            label=label, missing=", ".join(missing)
        ),
        file=sys.stderr,
    )
    raise SystemExit(6)
' "$label" "$expected" 2>&1); then
    :
  else
    probe_status=$?
    [ -n "$probe_output" ] && printf '%s\n' "$probe_output"
    printf '[ERROR] %s preflight failed for %s (exit %s).\n' "$label" "$candidate" "$probe_status" >&2
    return 1
  fi

  case "$probe_output" in
    *"DEVCTL_PREFLIGHT label="*)
      printf '%s\n' "$probe_output"
      ;;
    *)
      [ -n "$probe_output" ] && printf '%s\n' "$probe_output"
      printf '[ERROR] %s did not identify itself as Python: %s\n' "$label" "$candidate" >&2
      return 1
      ;;
  esac
}

check_diff_scope() {
  if ! git rev-parse --verify "$BASE_REF^{commit}" >/dev/null 2>&1; then
    printf '[ERROR] BASE_REF does not resolve to a commit: %s\n' "$BASE_REF" >&2
    return 1
  fi
  printf '[runner] diff check against BASE_REF=%s\n' "$BASE_REF"
  git diff --check "$BASE_REF"...HEAD
  printf '[runner] cached diff check\n'
  git diff --check --cached
  printf '[runner] worktree diff check\n'
  git diff --check
}

run_python_suite() {
  label=$1
  python=$2
  printf '[runner] %s full regression\n' "$label"
  for test_file in \
    tests/portable-runtime.py \
    tests/python-core.py \
    tests/posix-launcher.py \
    tests/entrypoint-routing.py \
    tests/cockpit-profile.py \
    tests/cockpit-commands.py \
    tests/cockpit-services.py \
    tests/cockpit-cli.py \
    tests/approval-binding.py \
    tests/project-config.py \
    tests/classification-core.py \
    tests/contract-core.py \
    tests/contract-diff.py \
    tests/task-state.py \
    tests/trace-core.py
  do
    printf '[runner] %s %s\n' "$label" "$test_file"
    "$python" "$test_file"
  done
  printf '[runner] %s pycompile\n' "$label"
  "$python" -m compileall -q xflow tests
}

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

check_diff_scope
preflight_python PYTHON39 "$PYTHON39" 3.9
preflight_python PYTHON_CURRENT "$PYTHON_CURRENT" current

run_python_suite PYTHON39 "$PYTHON39"
run_python_suite PYTHON_CURRENT "$PYTHON_CURRENT"

printf '[runner] review-gate.sh\n'
bash tests/review-gate.sh

if "$PYTHON_CURRENT" tests/powershell-entrypoint.py; then
  :
else
  powershell_status=$?
  if [ "$powershell_status" -eq 77 ]; then
    printf '[runner] PowerShell capability unavailable; skipped (rc=77)\n'
  else
    exit "$powershell_status"
  fi
fi

printf 'platform runtime regression ok\n'
