# Main Core Backport Design

## Background

`xflow-devctl` uses two long-lived product branches:

- `main` is the general software-development workflow line.
- `academic` is the academic research and writing workflow line.

The `academic` branch has validated several capabilities that are not
academic-specific: Python-based command handling, local human approval before
remote writes, GitHub API operations, safer PowerShell/native-command behavior,
and submodule hygiene checks. These should be brought back to `main` as a
general workflow core without importing academic-only semantics.

The goal is not to merge `academic` into `main`. The goal is to extract a
domain-neutral core that `main` owns and that `academic` can continue to build
on as a profile.

## Product-Line Boundary

`main` should provide a general `dev` profile for code-development work.
`academic` should remain a specialized profile for paper repositories,
AcademicForge, Claude academic task packages, and academic review templates.

The main branch must not mention academic paper branches, AcademicForge, paper
directory layouts, or academic-only template names in its default command
surface. It may support profile-specific extensions through environment
variables or subcommands.

## Capabilities To Backport

The following capabilities should be ported from `academic` to `main`:

- Python 3.10+ core package for path handling, UTF-8-safe file IO, command
  parsing, provider calls, and checks.
- `devctl preflight` to report Python runtime, repo root, tool root, and active
  product/profile.
- GitHub provider operations implemented in Python:
  issue create/list/show/comment/close and PR create/get.
- Body-file-first remote write behavior. Complex Issue, comment, and PR/MR
  text must use `--body-file`.
- Local human approval before remote writes.
- Submodule hygiene checks for project-local tool submodules.
- PowerShell/native command safety support, especially exit-code-based Git
  handling and avoiding `2>&1 | Out-String`.

The following capabilities must stay out of main's default behavior:

- AcademicForge installation or registration logic.
- Academic Claude task package execution.
- Academic issue/MR templates.
- Paper repository directory conventions such as `manuscript/` and
  `references/`.
- Rules that treat `academic` as a paper workflow product line.

## Local Approval Model

`main` should rename the approval concept from "academic local approval" to
"local remote-write approval".

Remote writes include:

- creating a remote Issue;
- posting an Issue comment;
- closing a remote Issue;
- pushing a task branch as part of PR/MR creation;
- creating a PR/MR;
- changing remote metadata;
- performing installer-like global configuration writes.

The general rule is:

```text
AI output -> local verification/TDD -> local human review -> remote write
```

The approval record should be a Markdown file under the task directory, for
example:

```text
.xflow/issues/issue-<id>/approvals/local-review.md
```

This file is the active approval record. AI clients must not invent alternate
active approval file names such as `local-review-mr.md` or
`local-review-issue.md`. The active gate reads one current approval file so
that remote-write checks are deterministic.

Historical approvals are still important and should be preserved. When a new
approval supersedes an old one, the previous active approval should be copied
or moved to:

```text
.xflow/issues/issue-<id>/approvals/history/local-review-<action>-<timestamp>.md
```

Historical files are audit evidence only. They must not satisfy the active
remote-write gate unless explicitly copied back to `approvals/local-review.md`
and the reviewed file hash still matches.

The core approval check should verify:

- approval title;
- issue/task identifier;
- reviewer;
- approval timestamp;
- approved action;
- approved file;
- approved SHA256;
- decision `Approved: yes`;
- the current hash of the approved file matches the recorded hash.

The umbrella action `remote-write` may exist for exceptional broad approvals,
but profile templates should prefer specific actions such as `issue-create`,
`issue-comment`, `issue-close`, and `git-mr`.

## MR Review File Boundary

The current academic implementation has two separate checks that must remain
separate in the generalized main design:

1. MR draft shape check.
2. Local review approval check.

The MR draft shape check validates the MR body file itself. In the academic
branch this is:

```text
.xflow/issues/issue-<id>/mr-draft.md
```

For the general main profile, the default should remain:

```text
.xflow/issues/issue-<id>/mr-draft.md
```

This check should ensure that the MR draft contains required sections such as
summary, verification evidence, local review reference, and requested remote
actions. It should not itself prove that the review file exists or that the
hash matches.

The local review approval check validates:

```text
.xflow/issues/issue-<id>/approvals/local-review.md
```

For `devctl git mr`, the approved file should be the MR draft file unless an
explicit reviewed file is passed. The approval action should be `git-mr` or a
recognized umbrella remote-write action. The hash in `local-review.md` must
match the exact MR draft file that will be sent to the remote provider.

This separation avoids confusing "the MR body has the right structure" with
"the human approved this exact body for remote publication".

If multiple MR review rounds happen, each superseded approval should be
archived under `approvals/history/`, while the latest effective approval remains
`approvals/local-review.md`. The current MR command should keep checking only
the active approval file by default.

## PR Publication And Sealing Boundary

The `git-mr` approval should cover the whole PR publication sequence, not only
the single API request that creates the PR. Once the human approves the
reviewed `mr-draft.md` for `Approved Action: git-mr`, the command may:

- push the current task branch;
- create the remote PR/MR;
- write the returned PR/MR number and URL to local metadata or task artifacts;
- create one follow-up metadata commit when that commit only records the PR/MR
  number, URL, and creation evidence;
- push that metadata commit to the same task branch before review starts.

These steps are part of the same approved publication action and must not
require a second approval file. This prevents an approval loop where the audit
record itself forces a new approval and another PR.

After the PR/MR is merged, the original task is sealed. The workflow must not
require a new commit or new PR only to check off a local task-board item, record
the merge, or rewrite the local checklist. Post-merge local notes may be kept
outside the merged branch, but they are not part of the original PR completion
criteria.

If the remote Issue was not automatically closed by `Closes #<id>`, closing it
is a separate maintenance remote write. It may use `devctl issue close <id>`
with a new `Approved Action: issue-close`, but it must not reopen the original
PR task or require another PR just to record that optional cleanup.

## Tool Source And Submodule Relationship

The main branch should document and enforce three layers:

1. Global source repositories:
   - `~/.codex/xflow/repos/xflow-devctl`
   - `~/.codex/xflow/repos/xflow-skills`
2. Project-local pinned tool submodules:
   - `.xflow/ops/devctl`
   - `.xflow/ops/workflow`
3. Project-root AI guardrail files:
   - `AGENTS.md`
   - `.cursorrules`
   - `CLAUDE.md`
   - `GEMINI.md`
   - `SKILL.md` when applicable

Runtime commands should use the project-local `devctl` wrapper and pinned
submodules. Global repositories are for bootstrap and reviewed updates only.
AI guardrail files are prompts and routing hints, not executable truth.

Every AI guardrail must repeat the same approval-file rule: never skip local
human approval, never invent an active approval file name, and archive previous
approvals under `approvals/history/` when a new approval replaces them.

Submodule updates should follow:

```text
fetch -> pin reviewed SHA -> test -> human review -> commit
```

Silent tracking of latest branch heads must be rejected.

## Multi-Platform AI Support

Main should be platform-neutral. Codex, Cursor, Claude, and Gemini should all
read repository-local guardrail files and invoke repository-local `devctl`.

The devctl core should not depend on any one AI product. It should expose
checks and commands that any upper AI can call.

## Migration Strategy

Implementation should proceed in small, test-first slices:

1. Add Python runtime preflight and launcher routing.
2. Add general local approval module and tests.
3. Port GitHub provider operations to Python.
4. Add body-file enforcement for remote-write commands.
5. Add submodule hygiene checks.
6. Add PowerShell/native command helper support.
7. Update help text and tests.

Each slice should keep the current `main` command surface working unless a
behavior change is explicitly documented and tested.

## Verification

The backport is acceptable only when:

- Python unit tests cover the new core behavior.
- Existing shell tests still pass or have documented replacement coverage.
- GitHub provider tests use local fake HTTP servers instead of real tokens.
- Approval tests prove hash mismatch, action mismatch, and missing approval
  failures.
- Submodule hygiene tests cover tracked modifications and generated byproducts.
- `git diff --check` passes.
