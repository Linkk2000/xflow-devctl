# Python Core Migration Design

## Background

The academic product line needs stricter local gates, better cross-platform
behavior, and reliable handling of UTF-8 Markdown, issue bodies, MR bodies, TDD
result sheets, local approval files, and Claude task packages. The current
implementation is split across Bash and PowerShell wrappers. That split caused
two concrete failures during the paper-demo test:

- PowerShell environment variables did not propagate into WSL Bash, so an
  expected local approval gate did not run.
- Bash tried to execute a Windows Claude shim and produced encoding/runtime
  noise even though AcademicForge was already registered in the Windows Claude
  config.

The migration goal is to move compliance-sensitive behavior into one Python
core while keeping the platform launchers very small.

## Decision

Adopt a hybrid architecture:

- Keep `devctl` and `devctl.ps1` as thin launchers.
- Add a Python standard-library-only core under `xflow/`.
- Require Python 3.10 or newer for the Python core.
- Do not silently install Python.
- If Python is missing, fail closed and emit a human-reviewable installation
  recommendation.

This avoids duplicating business logic across Bash and PowerShell while keeping
bootstrap simple enough to audit.

## Non-Goals

- Do not rewrite every command at once.
- Do not add third-party Python dependencies in the first migration.
- Do not create a virtual environment automatically.
- Do not run package managers, `winget`, `brew`, `apt`, or installers without
  explicit human approval.
- Do not change remote-write policy. Issue, comment, close, push, and MR actions
  still require local human approval before any remote operation.

## Architecture

### Launchers

`devctl` and `devctl.ps1` remain the only user-facing entrypoints. Their
responsibilities are limited to:

- find the repository root;
- set repository defaults such as `DEVCTL_PRODUCT_LINE=academic` when applicable;
- locate Python;
- invoke the Python core with all original arguments;
- fail closed with a clear diagnostic if Python is unavailable.

They must not contain template validation, approval semantics, provider API
logic, JSON parsing, or Markdown processing.

### Python Core

The Python core should live in a small package, initially:

```text
xflow/
  __main__.py
  cli.py
  env.py
  paths.py
  checks.py
  approval.py
  providers/
    github.py
    gitee.py
```

Responsibilities:

- parse command arguments with `argparse`;
- read and write UTF-8 files with explicit encoding;
- resolve paths with `pathlib`;
- compute SHA256 with `hashlib`;
- parse local approval files with deterministic line-based rules;
- validate academic templates;
- enforce approval gates before remote writes;
- call Git through `subprocess` where needed;
- call provider APIs with `urllib.request` and JSON from the standard library.

### Compatibility Layer

Existing command shapes should continue to work:

- `devctl issue create <title> [--body B] [--body-file F] [--labels a,b]`
- `devctl issue comment <number> [--body B] [--body-file F]`
- `devctl issue close <number>`
- `devctl git mr --title T --body-file F --issue N`
- `devctl check academic-issue --issue N [--file F]`
- `devctl check tdd-result --issue N [--file F]`
- `devctl check claude-package --issue N [--file F]`
- `devctl check academic-mr --issue N [--file F]`
- `devctl check local-review --issue N --file F`

During migration, each command can be ported one group at a time. Commands not
yet ported may be delegated to the existing shell implementation, but approval
gate commands must be ported first to remove the duplicated shell behavior.

## Python Detection

Launcher detection order:

Windows PowerShell:

1. `py -3`
2. `python`
3. `python3`

Bash:

1. `python3`
2. `python`
3. Windows Python only when safely invokable from the current shell

The launcher verifies:

- executable exists;
- version is Python 3.10 or newer;
- it can run `import pathlib, argparse, hashlib, json, urllib.request`.

If the check fails, the launcher exits before running any remote command.

## Missing Python Behavior

When Python is missing or too old, `devctl` must:

- print the detected platform;
- print the required Python version;
- print the command it would recommend, such as `winget install Python.Python.3.12`;
- write no remote data;
- avoid automatic installation;
- instruct the user to create a local approval if they want the assistant to
  perform installation.

The `SKILL.md` workflow should say that Python installation is a local
environment change requiring human review.

## Encoding Rules

The Python core always uses:

- `encoding="utf-8"` when reading or writing workflow files;
- `errors="strict"` for compliance files;
- normalized `\n` newlines for generated Markdown;
- explicit byte hashing for approval-bound artifacts.

PowerShell and Bash launchers should not parse Markdown content. Multi-line
issue and MR bodies must continue to use `--body-file`.

## Approval Gate Semantics

Approval checks remain fail-closed.

For each remote-write action, the Python core validates:

- approval file exists;
- approved artifact exists;
- `Approved: yes`;
- `Approved Action:` matches the requested action or an allowed umbrella action;
- `Approved SHA256:` equals the current bytes of the approved artifact;
- the command's effective body file is the approved artifact when applicable.

The Python implementation should fix the current draft-path ambiguity by making
pre-Issue creation use `.xflow/issue-draft/` consistently.

## Skill Repository Changes

`xflow-skills` should be updated after the core design is accepted:

- document Python 3.10+ as a required runtime for devctl;
- state that Python installation is never silent;
- add an academic preflight step before any remote write;
- clarify that `.xflow/issue-draft/` is used before a remote Issue number exists;
- keep the AI-facing workflow language independent of shell implementation.

## Testing Strategy

Tests should be added before porting behavior.

Minimum test groups:

- launcher detects missing Python and fails closed;
- launcher invokes Python core on Windows and Bash;
- UTF-8 Markdown issue body round-trips without mojibake;
- `check academic-issue` accepts valid template and rejects missing sections;
- `check tdd-result` accepts valid template and rejects missing sections;
- local approval passes only when file hash, action, and `Approved: yes` match;
- issue creation is blocked before provider calls without approval;
- issue creation with approval calls a fake provider, not the network;
- `.xflow/issue-draft/` is used before remote Issue creation.

Provider tests must mock network calls through local fake functions or local
fixtures. No test may create remote Issues, comments, pushes, or MRs.

## Migration Phases

### Phase 1: Preflight and Core Skeleton

- Add Python package skeleton.
- Add launcher Python detection.
- Add `devctl preflight` and `devctl check` commands in Python.
- Keep remote provider commands on existing shell path.

### Phase 2: Approval Gates

- Port approval gate logic to Python.
- Route `issue create`, `issue comment`, `issue close`, and `git mr` through the
  Python gate before any legacy or provider call.
- Add regression test for the paper-demo failure where env variables did not
  propagate from PowerShell into WSL Bash.

### Phase 3: Provider Calls

- Port GitHub/Gitee issue and MR creation to Python standard library HTTP.
- Keep `--body-file` as the required path for multi-line Markdown.
- Add fake-provider tests.

### Phase 4: Skill Documentation and Paper-Demo Validation

- Update `xflow-skills` academic references.
- Re-run paper-demo local tests.
- Do not close or comment on remote Issue #2 without explicit human approval.

## Open Constraints

- The first implementation should remain standard-library-only.
- Any future dependency must be justified in a separate design update.
- The migration must preserve the two product lines: `main` for general code
  workflows and `academic` for academic workflows.
- Local human review remains mandatory before all remote writes and local
  installer actions.
