# Task-Scoped Unattended devctl Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a fail-closed task-scoped unattended state that can replace ordinary local human-review gates for supported XFlow remote writes without granting destructive Git or secret-management capabilities.

**Architecture:** Isolate state parsing and validation in `xflow/unattended.py`, expose enable/status/disable through the existing argparse CLI, and centralize gate selection so remote commands use either a valid unattended state or the existing approval file. Store only non-secret state under `.xflow/local`, bind it to repository/worktree/Issue fingerprints, and invalidate it on task cleanup or mismatch.

**Tech Stack:** Python 3.11 standard library, argparse, JSON, hashlib, pathlib, existing executable test scripts, Git.

## Global Constraints

- Exact confirmation value: `XFLOW_HUMAN_UNATTENDED_ALL`.
- State path: `.xflow/local/unattended.json`.
- Never persist the confirmation value or any token/secret.
- Invalid or mismatched state is fail-closed and falls back to existing human review.
- `--no-local-review` without active matching state must fail.
- Unattended mode skips only human approval files; all other checks continue.
- Do not add force push, history rewrite, destructive deletion, or secret/permission mutation commands.

---

### Task 1: State model and CLI lifecycle

**Files:**
- Create: `xflow/unattended.py`
- Modify: `xflow/cli.py`
- Modify: `tests/python-core.py`
- Modify: `tests/entrypoint-routing.py`

**Interfaces:**
- Produces: `UnattendedState`, `enable(repo_root, issue, confirmation)`, `load(repo_root)`, `require_active(repo_root, issue)`, `disable(repo_root)`, and `migrate_issue(repo_root, old_issue, new_issue)`.
- State file: UTF-8 JSON at `.xflow/local/unattended.json` with `version`, `mode`, `repository`, `worktree`, `issue`, and `enabledAt`.

- [ ] **Step 1: Write failing state tests**

Test exact confirmation success; lowercase, whitespace-altered, incomplete, and extra-character confirmation failure; state contains no safety word; status active/inactive/invalid; idempotent disable; repository/worktree/Issue mismatch failure; UTF-8 BOM handling; and atomic migration from `draft` to `42`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python tests/python-core.py`

Expected: FAIL importing `xflow.unattended`.

- [ ] **Step 3: Implement the focused state module**

Use a frozen dataclass, SHA-256 fingerprints derived from canonical Git common-dir plus current worktree root, atomic temporary-file replacement, UTC ISO timestamps, strict JSON type checks, and actionable `ValueError` messages. `require_active` must compare normalized Issue IDs and never repair invalid state.

- [ ] **Step 4: Add CLI commands**

Add `unattended enable --issue ISSUE --confirm VALUE`, `unattended status`, and `unattended disable`. `status` exits 0 for active/inactive, prints invalid state as `[WARN]`, and does not expose sensitive environment values.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `python tests/python-core.py`

Run: `python tests/entrypoint-routing.py`

Expected: both exit 0 for Task 1 cases.

- [ ] **Step 6: Commit**

```powershell
git add xflow/unattended.py xflow/cli.py tests/python-core.py tests/entrypoint-routing.py
git commit -m "feat(gate): 增加任务级无人值守状态"
```

---

### Task 2: Central remote-gate integration and compatibility

**Files:**
- Modify: `xflow/approval.py`
- Modify: `xflow/cli.py`
- Modify: `xflow/unattended.py`
- Modify: `tests/python-core.py`

**Interfaces:**
- Consumes: `require_active(repo_root: Path, issue: str) -> UnattendedState` from Task 1.
- Produces: `approval.require_remote_or_unattended(repo_root, action, file_path, issue, attachments=None, request_unattended=False) -> str`, returning `local-review` or `unattended`.

- [ ] **Step 1: Write failing gate-matrix tests**

Cover Issue create/comment/close, branch push, MR creation, normal PR merge, and state backfill. Each action passes with matching active state and no approved `local-review.md`; without state it retains the existing approval failure. Assert `[UNATTENDED] Human approval gate bypassed for current task IK152D.` is emitted.

- [ ] **Step 2: Add `--no-local-review` regression tests**

Assert the flag alone fails, active state plus the flag passes, attachments still run attachment validation, and a mismatched task state fails. Assert no provider request occurs before a gate failure.

- [ ] **Step 3: Run tests and verify RED**

Run: `python tests/python-core.py`

Expected: old code bypasses Issue review from the flag alone and other remote actions still require approval.

- [ ] **Step 4: Centralize gate selection**

Implement `require_remote_or_unattended`. It first validates all action inputs already required by callers, then accepts a matching unattended state or calls existing `require_remote`. A requested unattended bypass without matching state raises `ValueError("--no-local-review requires active task-scoped unattended mode")`.

- [ ] **Step 5: Route supported remote actions**

Replace direct approval calls in Issue create/comment/close, push, MR creation, normal merge, and remote state backfill paths. Resolve the action Issue from explicit argument, branch metadata, or current task in that order, and require it to match the state. Do not introduce unattended handling into unsupported destructive operations.

- [ ] **Step 6: Migrate draft only after confirmed Issue creation**

After the provider returns a definite Issue ID, atomically migrate `draft` state to that ID. If the provider result is missing/ambiguous or the request errors, leave state as `draft` and retain existing remote read-after-error behavior; do not retry creation mechanically.

- [ ] **Step 7: Run tests and verify GREEN**

Run: `python tests/python-core.py`

Expected: all gate-matrix and compatibility cases pass.

- [ ] **Step 8: Commit**

```powershell
git add xflow/approval.py xflow/cli.py xflow/unattended.py tests/python-core.py
git commit -m "feat(remote): 接入统一无人值守人工门禁"
```

---

### Task 3: Automatic invalidation, help, and full verification

**Files:**
- Modify: `xflow/cli.py`
- Modify: `README.md`
- Modify: `help.txt`
- Modify: `tests/python-core.py`
- Modify: `tests/entrypoint-routing.py`

**Interfaces:**
- Consumes: `disable(repo_root: Path) -> bool` from Task 1.
- Produces: discoverable, copy-safe unattended command documentation and cleanup behavior.

- [ ] **Step 1: Write failing cleanup and help tests**

Assert `git done` disables state, current-task Issue mismatch is inactive/fail-closed, all three commands appear in module and PowerShell help, `--no-local-review` is not shown as independently authorized, and high-risk exclusions are visible.

- [ ] **Step 2: Run tests and verify RED**

Run: `python tests/python-core.py`

Run: `python tests/entrypoint-routing.py`

Expected: cleanup/help assertions fail.

- [ ] **Step 3: Implement cleanup and documentation**

Call `disable` from successful `git done` cleanup. Update README/help with enable/status/disable, scope, invalidation, mechanical-check preservation, `[UNATTENDED]` meaning, and the explicit high-risk exclusion list. Do not include a natural-language approval example for `--no-local-review`.

- [ ] **Step 4: Run full verification**

Run: `python tests/python-core.py`

Run: `python tests/entrypoint-routing.py`

Run: `git diff --check`

Expected: both test scripts exit 0 and no whitespace errors are reported.

- [ ] **Step 5: Commit**

```powershell
git add xflow/cli.py README.md help.txt tests/python-core.py tests/entrypoint-routing.py
git commit -m "docs(unattended): 补齐生命周期与安全边界"
```

