# Python Core Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move compliance-sensitive devctl behavior to a standard-library-only Python core while preserving the existing command surface and fail-closed academic review gates.

**Architecture:** Keep `devctl` as a thin launcher and add an `xflow/` Python package for checks, approval gates, path handling, encoding, and provider-safe command routing. Migrate in slices: Python preflight and check commands first, then approval gate interception, then provider calls and skill docs.

**Tech Stack:** Bash launcher, Python 3.10+ standard library (`argparse`, `pathlib`, `hashlib`, `json`, `urllib.request`, `subprocess`, `unittest`), existing shell scripts for temporary compatibility.

---

## File Structure

Create or modify these files in `C:\Users\chenh\.codex\xflow\repos\xflow-devctl`:

- Create: `xflow/__init__.py`
  - Package marker. No behavior.
- Create: `xflow/__main__.py`
  - Allows `python -m xflow ...`.
- Create: `xflow/cli.py`
  - Parses arguments and dispatches Python-native commands.
- Create: `xflow/env.py`
  - Runtime discovery, repository root, tool root, product line, platform diagnostics.
- Create: `xflow/paths.py`
  - `.xflow/issue-*` and `.xflow/issue-draft` path resolution.
- Create: `xflow/io.py`
  - UTF-8 file reading, strict compliance reads, normalized Markdown writes.
- Create: `xflow/checks.py`
  - Academic template checks.
- Create: `xflow/approval.py`
  - Local review parsing and fail-closed approval validation.
- Create: `xflow/providers/__init__.py`
  - Provider package marker.
- Create: `xflow/providers/fake.py`
  - Test-only fake provider for no-network command tests.
- Modify: `devctl`
  - Detect Python 3.10+, route Python-native commands to `python -m xflow`, delegate unported commands to current shell scripts.
- Modify: `help.txt`
  - Add `devctl preflight` and note Python 3.10+ runtime.
- Create: `tests/python_core.py`
  - Standard-library unittest suite for Python core behavior.
- Create: `tests/python-launcher.sh`
  - Bash smoke tests for launcher routing and missing-Python behavior.
- Modify: `tests/academic-checks.sh`
  - Keep existing shell regression coverage during migration.
- Later modify in `C:\Users\chenh\.codex\xflow\repos\xflow-skills`:
  - `references/academic-workflow.md`
  - `references/academic-schema-contract.md`
  - `SKILL.md`
  - Document Python requirement and fail-closed install policy.

## Task 1: Add Python Core Skeleton And Preflight

**Files:**
- Create: `xflow/__init__.py`
- Create: `xflow/__main__.py`
- Create: `xflow/cli.py`
- Create: `xflow/env.py`
- Test: `tests/python_core.py`

- [ ] **Step 1: Write failing preflight tests**

Create `tests/python_core.py` with:

```python
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xflow.env import RuntimeContext, detect_python_runtime


class RuntimeTests(unittest.TestCase):
    def test_detect_python_runtime_requires_modern_python(self):
        runtime = detect_python_runtime()
        self.assertGreaterEqual(runtime.version_info[:2], (3, 10))
        self.assertTrue(runtime.executable)

    def test_runtime_context_uses_explicit_repo_root(self):
        with TemporaryDirectory() as tmp:
            env = {"DEVCTL_REPO_ROOT": tmp, "DEVCTL_PRODUCT_LINE": "academic"}
            context = RuntimeContext.from_env(ROOT, env)
            self.assertEqual(context.repo_root, Path(tmp).resolve())
            self.assertEqual(context.tool_root, ROOT)
            self.assertEqual(context.product_line, "academic")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest tests.python_core -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'xflow'`.

- [ ] **Step 3: Add minimal Python package**

Create `xflow/__init__.py`:

```python
"""XFlow devctl Python core."""
```

Create `xflow/env.py`:

```python
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class PythonRuntime:
    executable: str
    version_info: tuple[int, int, int]


@dataclass(frozen=True)
class RuntimeContext:
    tool_root: Path
    repo_root: Path
    product_line: str

    @classmethod
    def from_env(cls, tool_root: Path, env: Mapping[str, str]) -> "RuntimeContext":
        repo = Path(env.get("DEVCTL_REPO_ROOT", Path.cwd())).resolve()
        product = env.get("DEVCTL_PRODUCT_LINE", "")
        return cls(tool_root=tool_root.resolve(), repo_root=repo, product_line=product)


def detect_python_runtime() -> PythonRuntime:
    return PythonRuntime(
        executable=sys.executable,
        version_info=(sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
    )
```

Create `xflow/__main__.py`:

```python
from .cli import main

raise SystemExit(main())
```

Create `xflow/cli.py`:

```python
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .env import RuntimeContext, detect_python_runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devctl")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("preflight")
    return parser


def run_preflight() -> int:
    runtime = detect_python_runtime()
    context = RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)
    print(f"python: {runtime.executable}")
    print(f"version: {runtime.version_info[0]}.{runtime.version_info[1]}.{runtime.version_info[2]}")
    print(f"tool_root: {context.tool_root}")
    print(f"repo_root: {context.repo_root}")
    print(f"product_line: {context.product_line or 'unset'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preflight":
        return run_preflight()
    parser.print_help()
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python -m unittest tests.python_core -v
```

Expected: PASS for both runtime tests.

- [ ] **Step 5: Verify preflight command directly**

Run:

```bash
DEVCTL_REPO_ROOT="$(pwd)" DEVCTL_PRODUCT_LINE=academic python -m xflow preflight
```

Expected output includes `version:`, `repo_root:`, and `product_line: academic`.

- [ ] **Step 6: Commit**

```bash
git add xflow tests/python_core.py
git commit -m "feat(python): add devctl core preflight"
```

## Task 2: Add Launcher Python Detection And Routing

**Files:**
- Modify: `devctl`
- Modify: `help.txt`
- Create: `tests/python-launcher.sh`

- [ ] **Step 1: Write failing launcher smoke test**

Create `tests/python-launcher.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

output="$(DEVCTL_REPO_ROOT="$ROOT" DEVCTL_PRODUCT_LINE=academic bash "$ROOT/devctl" preflight)"
echo "$output" | grep -F "product_line: academic" >/dev/null
echo "$output" | grep -F "version:" >/dev/null

echo "python launcher ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
bash tests/python-launcher.sh
```

Expected: FAIL with `unknown command: preflight` or equivalent current launcher error.

- [ ] **Step 3: Implement minimal Bash launcher route**

Modify `devctl` near the top, after exports:

```bash
find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      "$candidate" - <<'PY' >/dev/null 2>&1 && { echo "$candidate"; return 0; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    fi
  done
  return 1
}

run_python_core() {
  local py
  py="$(find_python)" || {
    cat >&2 <<'EOF'
[ERROR] Python 3.10+ is required for devctl Python core.
No installation was performed.
Recommended Windows command after human review:
  winget install Python.Python.3.12
EOF
    exit 1
  }
  exec "$py" -m xflow "$@"
}
```

In the `main()` case block, add before `git)`:

```bash
    preflight)
      shift
      run_python_core preflight "$@"
      ;;
```

- [ ] **Step 4: Update help**

In `help.txt`, add:

```text
Preflight:
  devctl preflight
      Validate Python 3.10+ runtime, repo root, tool root, and product-line context.
```

- [ ] **Step 5: Run launcher test to verify it passes**

Run:

```bash
bash tests/python-launcher.sh
```

Expected: prints `python launcher ok`.

- [ ] **Step 6: Run existing tests**

Run:

```bash
bash tests/academic-checks.sh
bash tests/issue-body-guard.sh
```

Expected: both pass. Existing shell commands still work.

- [ ] **Step 7: Commit**

```bash
git add devctl help.txt tests/python-launcher.sh
git commit -m "feat(devctl): route preflight through python core"
```

## Task 3: Port Academic Template Checks To Python

**Files:**
- Create: `xflow/paths.py`
- Create: `xflow/io.py`
- Create: `xflow/checks.py`
- Modify: `xflow/cli.py`
- Modify: `devctl`
- Test: `tests/python_core.py`

- [ ] **Step 1: Add failing Python check tests**

Append to `tests/python_core.py`:

```python
from xflow.checks import check_academic_issue, check_tdd_result


class CheckTests(unittest.TestCase):
    def test_academic_issue_rejects_missing_sections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text("# Academic Issue Draft\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Task Type:"):
                check_academic_issue(path)

    def test_academic_issue_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Academic Issue Draft\n\n"
                "Task Type: workflow-test\n"
                "Target Branch: academic\n"
                "Target Artifacts:\n- README.md\n\n"
                "## Background\nx\n\n"
                "## Goal\nx\n\n"
                "## Scope\nx\n\n"
                "## Acceptance Criteria\nx\n\n"
                "## Verification Plan\nx\n\n"
                "## Human Review Gate\nx\n",
                encoding="utf-8",
            )
            check_academic_issue(path)

    def test_tdd_result_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-1" / "tdd-result.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# TDD Result\n\n"
                "Issue: 1\n"
                "Branch: feature/1-test\n"
                "Verified At: 2026-06-13T00:00:00+08:00\n"
                "Executor: Codex\n\n"
                "## Verification Scope\nx\n\n"
                "## Commands\nx\n\n"
                "## Results\nx\n\n"
                "## Risks\nx\n\n"
                "## Human Review Entry\nx\n",
                encoding="utf-8",
            )
            check_tdd_result(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest tests.python_core -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'xflow.checks'`.

- [ ] **Step 3: Add path and IO helpers**

Create `xflow/io.py`:

```python
from __future__ import annotations

from pathlib import Path


def read_text_strict(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="strict")
```

Create `xflow/paths.py`:

```python
from __future__ import annotations

from pathlib import Path


def issue_dir(repo_root: Path, issue: str) -> Path:
    return repo_root / ".xflow" / f"issue-{issue}"


def default_issue_file(repo_root: Path, issue: str, filename: str) -> Path:
    return issue_dir(repo_root, issue) / filename
```

Create `xflow/checks.py`:

```python
from __future__ import annotations

from pathlib import Path

from .io import read_text_strict


ACADEMIC_ISSUE_REQUIRED = [
    "# Academic Issue Draft",
    "Task Type:",
    "Target Branch:",
    "Target Artifacts:",
    "## Background",
    "## Goal",
    "## Scope",
    "## Acceptance Criteria",
    "## Verification Plan",
    "## Human Review Gate",
]

TDD_RESULT_REQUIRED = [
    "# TDD Result",
    "Issue:",
    "Branch:",
    "Verified At:",
    "Executor:",
    "## Verification Scope",
    "## Commands",
    "## Results",
    "## Risks",
    "## Human Review Entry",
]


def require_template(path: Path, required: list[str]) -> None:
    if not path.is_file():
        raise ValueError(f"missing required file: {path}")
    text = read_text_strict(path)
    for needle in required:
        if needle not in text:
            raise ValueError(f"missing required text '{needle}' in {path}")


def check_academic_issue(path: Path) -> None:
    require_template(path, ACADEMIC_ISSUE_REQUIRED)


def check_tdd_result(path: Path) -> None:
    require_template(path, TDD_RESULT_REQUIRED)
```

- [ ] **Step 4: Add Python CLI check dispatch**

Modify `xflow/cli.py` by adding check subcommands:

```python
from .checks import check_academic_issue, check_tdd_result
from .paths import default_issue_file
```

Inside `build_parser()`:

```python
    check = sub.add_parser("check")
    check_sub = check.add_subparsers(dest="check_command")
    for name in ("academic-issue", "tdd-result"):
        item = check_sub.add_parser(name)
        item.add_argument("--issue", required=True)
        item.add_argument("--file")
```

Inside `main()` before help fallback:

```python
    if args.command == "check":
        context = RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)
        filename = "issue-draft.md" if args.check_command == "academic-issue" else "tdd-result.md"
        path = Path(args.file).resolve() if args.file else default_issue_file(context.repo_root, args.issue, filename)
        try:
            if args.check_command == "academic-issue":
                check_academic_issue(path)
            elif args.check_command == "tdd-result":
                check_tdd_result(path)
            else:
                parser.error(f"unknown check subcommand: {args.check_command}")
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        print(f"[INFO] {args.check_command} check passed: {path}")
        return 0
```

Also import `sys`.

- [ ] **Step 5: Route Python-native checks from launcher**

In `devctl`, under `check)` case, route these two checks first:

```bash
        academic-issue|tdd-result)
          subcmd="$1"
          shift
          run_python_core check "$subcmd" "$@"
          ;;
```

- [ ] **Step 6: Run tests**

Run:

```bash
python -m unittest tests.python_core -v
bash tests/academic-checks.sh
```

Expected: both pass.

- [ ] **Step 7: Commit**

```bash
git add xflow tests/python_core.py devctl
git commit -m "feat(python): port academic template checks"
```

## Task 4: Port Local Approval Gate To Python

**Files:**
- Create: `xflow/approval.py`
- Modify: `xflow/cli.py`
- Modify: `xflow/paths.py`
- Modify: `devctl`
- Test: `tests/python_core.py`

- [ ] **Step 1: Add failing approval tests**

Append to `tests/python_core.py`:

```python
from xflow.approval import require_remote_approval


class ApprovalTests(unittest.TestCase):
    def test_remote_approval_requires_approved_yes_action_and_hash(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue_dir = root / ".xflow" / "issue-draft"
            approval_dir = issue_dir / "approvals"
            approval_dir.mkdir(parents=True)
            artifact = issue_dir / "issue-draft.md"
            artifact.write_text("# Academic Issue Draft\n", encoding="utf-8")
            digest = __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
            (approval_dir / "local-review.md").write_text(
                "# Local Review Approval\n\n"
                "Issue: draft\n"
                "Reviewer: user\n"
                "Approved At: 2026-06-13T00:00:00+08:00\n"
                "Approved Action: issue-create\n"
                "Approved File: .xflow/issue-draft/issue-draft.md\n"
                f"Approved SHA256: {digest}\n\n"
                "## Decision\n"
                "Approved: yes\n",
                encoding="utf-8",
            )
            require_remote_approval(root, "issue-create", artifact, "draft")

    def test_remote_approval_rejects_wrong_action(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue_dir = root / ".xflow" / "issue-draft"
            approval_dir = issue_dir / "approvals"
            approval_dir.mkdir(parents=True)
            artifact = issue_dir / "issue-draft.md"
            artifact.write_text("body\n", encoding="utf-8")
            digest = __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
            (approval_dir / "local-review.md").write_text(
                "# Local Review Approval\n"
                "Approved Action: test-only\n"
                f"Approved SHA256: {digest}\n"
                "Approved: yes\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "action mismatch"):
                require_remote_approval(root, "issue-create", artifact, "draft")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest tests.python_core -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'xflow.approval'`.

- [ ] **Step 3: Implement approval module**

Create `xflow/approval.py`:

```python
from __future__ import annotations

import hashlib
from pathlib import Path

from .io import read_text_strict
from .paths import issue_dir


ALLOWED_UMBRELLA_ACTIONS = {"remote-write", "remote write", "all-remote-writes"}


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"missing approved file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_field(text: str, field: str) -> str:
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise ValueError(f"invalid academic approval: missing {field}")


def default_approval_file(repo_root: Path, issue: str) -> Path:
    return issue_dir(repo_root, issue) / "approvals" / "local-review.md"


def read_approval_text(repo_root: Path, issue: str, approved_file: Path) -> str:
    approval_file = default_approval_file(repo_root, issue)
    if not approval_file.is_file():
        raise ValueError(f"academic local approval required before remote write: missing {approval_file}")
    if not approved_file.is_file():
        raise ValueError(f"academic approved artifact missing: {approved_file}")

    text = read_text_strict(approval_file)
    if "# Local Review Approval" not in text:
        raise ValueError("invalid academic approval: missing title")
    if "Approved: yes" not in text:
        raise ValueError("invalid academic approval: not approved")
    parse_field(text, "Approved Action")
    expected = parse_field(text, "Approved SHA256")
    actual = sha256_file(approved_file)
    if expected != actual:
        raise ValueError(f"academic approval hash mismatch for {approved_file}")
    return text


def check_local_review_file(repo_root: Path, issue: str, approved_file: Path) -> None:
    read_approval_text(repo_root, issue, approved_file)


def require_remote_approval(repo_root: Path, action: str, approved_file: Path, issue: str) -> None:
    text = read_approval_text(repo_root, issue, approved_file)

    approved_action = parse_field(text, "Approved Action")
    if approved_action != action and approved_action not in ALLOWED_UMBRELLA_ACTIONS:
        raise ValueError(f"academic approval action mismatch: expected {action}, got {approved_action}")
```

- [ ] **Step 4: Add `check local-review` Python CLI**

Modify `xflow/cli.py` so `check local-review --issue N --file F` calls:

```python
from .approval import check_local_review_file, require_remote_approval
```

For local-review, do not enforce a remote action. This command validates the
approval file shape and hash binding only. Remote commands call
`require_remote_approval()` separately with their exact action.

```python
def check_local_review(context: RuntimeContext, issue: str, file_value: str) -> None:
    path = Path(file_value).resolve()
    check_local_review_file(context.repo_root, issue, path)
```

- [ ] **Step 5: Route remote write commands through Python gate**

In `xflow/cli.py`, add an `issue create` parser that only validates the gate and then exits with a distinct code when `DEVCTL_SKIP_PROVIDER_LOAD=1`:

```python
issue = sub.add_parser("issue")
issue_sub = issue.add_subparsers(dest="issue_command")
create = issue_sub.add_parser("create")
create.add_argument("title")
create.add_argument("--body", default="")
create.add_argument("--body-file")
create.add_argument("--labels", default="")
```

In `main()`:

```python
    if args.command == "issue" and args.issue_command == "create":
        context = RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)
        if not args.body_file:
            print("[ERROR] academic issue create requires --body-file", file=sys.stderr)
            return 1
        body_file = Path(args.body_file).resolve()
        try:
            require_remote_approval(context.repo_root, "issue-create", body_file, "draft")
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
            print("[INFO] issue-create gate passed; provider skipped")
            return 0
        print("[ERROR] provider not ported to Python yet", file=sys.stderr)
        return 1
```

In `devctl`, route `issue create` through Python when academic product line is active:

```bash
        create)
          shift
          if [[ "${DEVCTL_PRODUCT_LINE:-}" == "academic" || "${DEVCTL_ACADEMIC_ENFORCE:-}" == "1" ]]; then
            run_python_core issue create "$@"
          fi
          run_script "$OPS/issue/create.sh" "$@"
          ;;
```

- [ ] **Step 6: Run regression tests**

Run:

```bash
python -m unittest tests.python_core -v
bash tests/academic-checks.sh
```

Expected: Python tests pass. Existing academic shell tests pass or are adjusted only where Python has intentionally replaced behavior.

- [ ] **Step 7: Add paper-demo failure regression**

Add to `tests/python-launcher.sh`:

```bash
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/.xflow/issue-draft"
cat >"$tmpdir/.xflow/issue-draft/issue-draft.md" <<'EOF'
# Academic Issue Draft
Task Type: workflow-test
Target Branch: academic
Target Artifacts:
- README.md
## Background
x
## Goal
x
## Scope
x
## Acceptance Criteria
x
## Verification Plan
x
## Human Review Gate
x
EOF

if DEVCTL_REPO_ROOT="$tmpdir" DEVCTL_PRODUCT_LINE=academic DEVCTL_SKIP_PROVIDER_LOAD=1 \
  bash "$ROOT/devctl" issue create "must be blocked" --body-file "$tmpdir/.xflow/issue-draft/issue-draft.md" >/tmp/xflow-gate.out 2>&1
then
  cat /tmp/xflow-gate.out >&2
  echo "expected academic issue create to be blocked without approval" >&2
  exit 1
fi
grep -F "academic local approval required" /tmp/xflow-gate.out >/dev/null
```

- [ ] **Step 8: Run launcher regression**

Run:

```bash
bash tests/python-launcher.sh
```

Expected: prints `python launcher ok` and exits 0.

- [ ] **Step 9: Commit**

```bash
git add xflow devctl tests/python_core.py tests/python-launcher.sh
git commit -m "feat(python): enforce academic approval gate"
```

## Task 5: Port Remaining Academic Checks

**Files:**
- Modify: `xflow/checks.py`
- Modify: `xflow/cli.py`
- Test: `tests/python_core.py`

- [ ] **Step 1: Add failing tests for `claude-package` and `academic-mr`**

Add tests that use valid templates from `tests/academic-checks.sh` and call new functions:

```python
from xflow.checks import check_academic_mr, check_claude_package


class AdditionalCheckTests(unittest.TestCase):
    def test_claude_package_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude-task.md"
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "AcademicForge Skill: paper-polish-workflow-skill@unknown\n"
                "Input Files:\n- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            check_claude_package(path)

    def test_academic_mr_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mr-draft.md"
            path.write_text(
                "# MR Draft\n\n"
                "Issue: 1\n"
                "Target Branch: academic\n\n"
                "## Summary\nx\n\n"
                "## Evidence\nx\n\n"
                "## Remote Actions Requested\nx\n",
                encoding="utf-8",
            )
            check_academic_mr(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest tests.python_core -v
```

Expected: FAIL with import error for missing functions.

- [ ] **Step 3: Implement check functions**

Add required lists and functions to `xflow/checks.py`:

```python
CLAUDE_PACKAGE_REQUIRED = [
    "# Claude Task Package",
    "Issue:",
    "AcademicForge Skill:",
    "Input Files:",
    "Output File:",
    "## Objective",
    "## Constraints",
    "## Required Output Format",
    "## Human Review Requirement",
]

ACADEMIC_MR_REQUIRED = [
    "# MR Draft",
    "Issue:",
    "Target Branch:",
    "## Summary",
    "## Evidence",
    "## Remote Actions Requested",
]


def check_claude_package(path: Path) -> None:
    require_template(path, CLAUDE_PACKAGE_REQUIRED)


def check_academic_mr(path: Path) -> None:
    require_template(path, ACADEMIC_MR_REQUIRED)
```

- [ ] **Step 4: Add CLI dispatch**

Extend `check` subcommands in `xflow/cli.py` to include `claude-package` and `academic-mr`, defaulting to `claude-task.md` and `mr-draft.md`.

- [ ] **Step 5: Route launcher**

In `devctl`, route `claude-package`, `academic-mr`, and `local-review` through Python once they pass tests.

- [ ] **Step 6: Run tests**

Run:

```bash
python -m unittest tests.python_core -v
bash tests/academic-checks.sh
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add xflow devctl tests/python_core.py
git commit -m "feat(python): port academic check commands"
```

## Task 6: Update Skill Documentation For Python Runtime

**Files in `C:\Users\chenh\.codex\xflow\repos\xflow-skills`:**
- Modify: `references/academic-workflow.md`
- Modify: `references/academic-schema-contract.md`
- Modify: `SKILL.md`
- Test: `tests/academic-entrypoint.sh`

- [ ] **Step 1: Write documentation update**

Add this policy to `references/academic-workflow.md`:

```markdown
## Python Runtime Preflight

Academic devctl uses a Python 3.10+ core for UTF-8, path, template, and approval
gate handling. Before remote writes or local installer actions, AI assistants
must run `devctl preflight` and record the result in the TDD result sheet.

If Python 3.10+ is missing, devctl must fail closed. AI assistants must not
silently install Python. Installation requires a local human review approval
that names the installer command and the reason it is needed.
```

Add this to `references/academic-schema-contract.md`:

```markdown
## Runtime Contract

- Required runtime: Python 3.10 or newer.
- Initial dependency policy: Python standard library only.
- Missing Python behavior: fail closed and provide a reviewable installation
  recommendation.
- Pre-Issue draft location: `.xflow/issue-draft/`.
```

- [ ] **Step 2: Ensure `SKILL.md` references remain valid**

Check that `SKILL.md` keeps the `academic_references` block and no temporary development path is introduced.

- [ ] **Step 3: Run skill tests**

Run in `xflow-skills`:

```bash
bash tests/academic-entrypoint.sh
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add references/academic-workflow.md references/academic-schema-contract.md SKILL.md
git commit -m "docs(academic): require python preflight"
```

## Task 7: Paper-Demo Local Validation

**Files in `D:\04-code\paper-demo`:**
- No required commit unless the user approves paper-demo migration.
- Use local working tree only for validation.

- [ ] **Step 1: Update submodules locally**

Run:

```bash
git submodule sync --recursive
git -C _ops/devctl fetch origin academic
git -C _ops/devctl checkout -B academic origin/academic
git -C _ops/workflow fetch origin academic
git -C _ops/workflow checkout -B academic origin/academic
```

Expected: `_ops/devctl` points to the new Python migration commit and `_ops/workflow` points to the Python runtime docs commit.

- [ ] **Step 2: Run local preflight**

Run:

```bash
bash ./devctl preflight
```

Expected: prints Python version, repo root, tool root, and `product_line: academic`.

- [ ] **Step 3: Run local academic checks**

Run:

```bash
bash ./devctl check academic-issue --issue draft
bash ./devctl check tdd-result --issue 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\.xflow\issue-0\wrapper-smoke.ps1
```

Expected: all pass.

- [ ] **Step 4: Verify no-approval remote write block**

Run:

```bash
DEVCTL_SKIP_PROVIDER_LOAD=1 bash ./devctl issue create "blocked test" --body-file .xflow/issue-draft/issue-draft.md
```

Expected: FAIL before provider work with an academic local approval error.

- [ ] **Step 5: Record result**

Update `D:\04-code\paper-demo\.xflow\issue-0\tdd-result.md` with exact pass/fail commands.

- [ ] **Step 6: Stop before remote actions**

Do not push, close Issue #2, comment on Issue #2, or create an MR without explicit human approval.

## Task 8: Final Verification And Local Review Package

**Files:**
- Modify: `.xflow/issue-draft/issue-draft.md` or issue-specific planning files only if creating a formal issue draft.
- Do not push without human approval.

- [ ] **Step 1: Run full local verification in `xflow-devctl`**

Run:

```bash
python -m unittest tests.python_core -v
bash tests/python-launcher.sh
bash tests/academic-checks.sh
bash tests/issue-body-guard.sh
bash -n devctl lib/common.sh issue/create.sh issue/comment.sh issue/close.sh git/mr.sh
```

Expected: all pass.

- [ ] **Step 2: Run skill verification in `xflow-skills`**

Run:

```bash
bash tests/academic-entrypoint.sh
```

Expected: pass.

- [ ] **Step 3: Summarize commits**

Run:

```bash
git -C C:/Users/chenh/.codex/xflow/repos/xflow-devctl log --oneline origin/academic..HEAD
git -C C:/Users/chenh/.codex/xflow/repos/xflow-skills log --oneline origin/academic..HEAD
```

Expected: shows local commits awaiting human review.

- [ ] **Step 4: Prepare local review materials**

Create or update local review files under the relevant `.xflow/` issue directory with:

```markdown
# Local Review Approval

Issue: <issue-id-or-draft>
Reviewer: user
Approved At: <timestamp>
Approved Action: git-mr
Approved File: .xflow/<issue>/mr-draft.md
Approved SHA256: <sha256>

## Decision
Approved: yes
```

Only the human reviewer may approve this file for real remote write use.

- [ ] **Step 5: Stop and request human approval**

Report:

- commits created;
- tests run;
- any paper-demo residual risk;
- whether Issue #2 still requires a manual close/comment decision.

Do not push until the user explicitly approves the exact remote action.
