# Platform runtime final-fix report

- Baseline: `5b8a427e8b5f2e38b29245e89da83bcdd430045b` (clean before work).
- Final commit: the single final-fix commit at `HEAD` (verify with
  `git rev-parse HEAD`).
- Scope: final review Important items 1–7 and the requested low-risk strictness
  minors only. No `devctl.ps1` baseline changes, push, MR, deployment, provider
  write, issue, approval, or other real external side effect was performed.

## Delivered

- `run_scenario` and `run_playground` install guarded temporary SIGINT/SIGTERM
  handlers, restore prior handlers, return 130/143, and clean owned process
  groups, PID records, and log streams. Real subprocess tests cover both paths.
- Explicit profile/root CLI and environment selections are authoritative and
  fail before side effects when missing; cwd discovery is only the unspecified
  fallback.
- Strict v1 profiles require a deduplicated legal-sibling `repositories`
  allowlist and retain it in `CockpitProfile`; `--repo` never infers names from
  command strings. `defaultPlayground` is validated and drives omitted
  playground targets without a runtime product default.
- Root and generated POSIX wrappers use the canonical Git top-level from the
  current directory when `DEVCTL_REPO_ROOT` is unset, with a non-Git cwd
  fallback, while preserving argument order and the byte-level wrapper shape.
- `devctl help` and no arguments return help without profile/provider loading.
- The plan-2 profile example now matches the strict loader shape and has a
  contract test that extracts and loads the documented YAML.
- Health/open URL duplicates and blank/duplicate/global-conflicting playground
  aliases are rejected; README/help document process, log, PID, rerun, and
  signal-cleanup contracts.

## Verification

Passed:

```text
python tests/cockpit-profile.py
python tests/cockpit-cli.py
python tests/cockpit-services.py
python tests/entrypoint-routing.py
python3.9 tests/cockpit-profile.py
python3.9 tests/cockpit-cli.py
python3.9 tests/cockpit-services.py
python3.9 tests/cockpit-commands.py
python3.9 tests/posix-launcher.py
python3.9 tests/entrypoint-routing.py
PYTHON39=python3.9 PYTHON_CURRENT=python3.12 BASE_REF=origin/main sh tests/run-platform-runtime.sh
git diff --check
```

The dual-version platform runner passed, including syntax/pycompile and review
gate checks. Its PowerShell lane was skipped because that capability was not
available in the environment (77 tests skipped). Static scans found no
`shell=True`, runtime `flowable` default, or forbidden local path in the
reviewed runtime/entrypoint scope. No process residue remained after the real
subprocess tests.

## Remaining concerns

- PowerShell behavior was not executable-validated here; the requested baseline
  `devctl.ps1` was left untouched.
- PID metadata is deliberately retained when tree termination cannot be
  confirmed, so a later invocation fails closed rather than adopting unknown
  processes.
- Successful runtime commands continue to leave their owned services running
  by the existing contract; this change does not invent a standalone stop
  command.
