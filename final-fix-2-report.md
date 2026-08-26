# Platform runtime final-fix-2 report

- Baseline: `d925924b40979b859fefc4da734044a61389d042` (clean before round 2).
- Final commit: the single round-2 fix commit at `HEAD` (verify with
  `git rev-parse HEAD`).
- Scope: signal delivery being swallowed by ordinary exception handlers only.
  No push, MR, deployment, provider write, issue, approval, or other real
  external side effect was performed.

## Root cause and fix

`_url_is_healthy()` catches ordinary `Exception` while invoking the health
probe. The internal signal exception therefore became a failed probe and the
parent remained in the health loop. `_SupervisorSignal` now inherits from
`BaseException`, so dependency, health, URL, browser, build, and other ordinary
exception handlers cannot consume it. `run_scenario` and `run_playground` keep
the dedicated signal catches, stable 143/130 return codes, cleanup, and signal
handler restoration.

The real subprocess regression waits for a health-probe marker before sending
each signal. It covers scenario/playground × SIGTERM/SIGINT and asserts parent
143/130, owned leader and descendant exit, PID metadata removal, handler
restoration, and log reopening from inside and outside the parent process.

## Verification

TDD evidence:

- RED: the synchronized health-probe test failed before the fix because the
  parent timed out without returning from the swallowed signal.
- GREEN: the same focused service suite passed after the `BaseException` fix.

Passed on both Python 3.9 and 3.12:

```text
python tests/cockpit-profile.py
python tests/cockpit-cli.py
python tests/cockpit-services.py
python tests/entrypoint-routing.py
python -m py_compile xflow/*.py tests/*.py
git diff --check
```

The service regression was run repeatedly on both interpreters. No fixture
process or signal-runner residue remained after the tests; the final handoff
also includes a clean-worktree verification after commit.

## Remaining concerns

- Windows/PowerShell execution remains unvalidated because that capability is
  unavailable in the environment; no PowerShell baseline file was changed.
- The signal regression intentionally uses a descendant that exits on group
  termination; existing tests continue to cover descendants that ignore the
  first termination signal and require force cleanup.
