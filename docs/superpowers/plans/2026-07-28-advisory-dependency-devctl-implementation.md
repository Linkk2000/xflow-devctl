# Advisory Dependency Issue devctl Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add portable mechanical checks for advisory dependency graphs and Chinese scoped Issue-linked commit messages, integrate dependency closure into resolution-report validation, and preserve developer judgment by never blocking ordinary development solely because a dependency is unfinished.

**Architecture:** Parse issue-local `dependencies.yaml` into a small validation module, expose it through the existing `devctl check` dispatcher, and reuse its normalized result when validating a resolution report. Add a separate commit-message validator used by both the CLI check and generated commit paths. Keep provider behavior unchanged and keep PyYAML import local so unrelated devctl commands can still show help when the dependency is not installed.

**Tech Stack:** Python 3.11+, PyYAML 6.x, `argparse`, `dataclasses`, `unittest`-style executable test scripts, Git.

## Global Constraints

- Work only in `D:\04-code\020-skill-dev\xflow-devctl`.
- Follow test-driven development: add one focused failing test group, run it, then implement the smallest behavior that passes.
- `check dependencies` validates structure and consistency. It must not prohibit local development, commit creation, test execution, or evidence collection merely because status is `discovered`, `active`, or `available`.
- Remote Issue creation, push, MR, merge, and comment behavior retains existing human gates.
- Local evidence must remain inside `.xflow/issues/issue-<id>/`; URLs and COS/OSS paths are not valid closure evidence.
- GitHub numeric and Gitee alphanumeric Issue IDs must be preserved exactly.

---

### Task 1: Introduce the YAML dependency parser with focused unit coverage

**Files:**
- Create: `requirements.txt`
- Create: `xflow/dependencies.py`
- Modify: `tests/python-core.py`

- [ ] **Step 1: Add dependency fixtures and failing tests**

In `tests/python-core.py`, create `.xflow/issues/issue-IK152D/dependencies.yaml` plus local integration evidence. Add cases for:

- Valid Gitee IDs `IK152D` and `IK17AW`.
- Valid GitHub numeric IDs `123` and `456`.
- Top-level `issue` not matching `--issue`.
- Invalid `type`, `status`, `blockingAssessment`, or `decision`.
- Empty `requiredFor` or `rationale`.
- `available` child/shared dependency without delivery commit/MR information.
- `available` external dependency without provider, available version, or verification entry.
- `integrated` without integration commit, `verifiedBy`, or local evidence.
- `superseded` without a reason and closure assessment.
- Integration evidence using `https://`, `oss://`, or `cos://`.
- Integration evidence escaping the current parent Issue directory.

Initially import this concrete API:

```python
from xflow.dependencies import DependencyCheckResult, check_dependencies

result = check_dependencies(repo, "IK152D")
assert result.path.name == "dependencies.yaml"
assert result.entries[0]["issue"] == "IK17AW"
assert result.warnings == ()
```

- [ ] **Step 2: Run the failing test**

Run: `python tests/python-core.py`

Expected: import failure for `xflow.dependencies`.

- [ ] **Step 3: Declare and load PyYAML safely**

Create `requirements.txt`:

```text
PyYAML>=6.0,<7.0
```

In `xflow/dependencies.py`, import YAML inside a helper:

```python
def load_yaml(path: Path) -> object:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "dependency checks require PyYAML; run: python -m pip install -r requirements.txt"
        ) from exc
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))
```

This keeps `devctl help` and unrelated commands available without PyYAML.

- [ ] **Step 4: Implement the normalized result and validation**

Use this public interface:

```python
@dataclass(frozen=True)
class DependencyCheckResult:
    path: Path
    entries: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


check_dependencies(
    repo_root: Path,
    issue: str,
    file_path: Path | None = None,
) -> DependencyCheckResult
```

Validate exact enums:

```python
DEPENDENCY_TYPES = {"child-feature", "shared-infrastructure", "external"}
DEPENDENCY_STATUSES = {"discovered", "active", "available", "integrated", "superseded"}
BLOCKING_ASSESSMENTS = {"none", "partial", "full"}
DEVELOPMENT_DECISIONS = {"continue", "pause-affected-scope", "wait", "use-temporary-adapter"}
CLOSURE_DECISIONS = {"integrated", "not-required", "superseded"}
```

Use existing path-containment and repository-local evidence helpers from `xflow/checks.py` where practical. Avoid a circular import by moving only generic path/evidence helpers to a neutral module if necessary; do not duplicate URL/COS/OSS detection regexes.

Return warnings for unfinished statuses, for example `dependency #IK17AW is active; developer decision remains continue`. Do not raise solely because an advisory dependency is unfinished.

Apply type-specific availability rules: `child-feature` and `shared-infrastructure` use `delivery.branch`, `delivery.commit`, and `delivery.mergeRequest`; `external` instead uses non-empty `provider`, `availableVersion`, and `verificationEntry`. Every type uses the parent's `integration` block only after the parent has actually consumed and re-verified it.

- [ ] **Step 5: Run tests**

Run: `python tests/python-core.py`

Expected: all parser cases pass.

- [ ] **Step 6: Commit the parser**

```powershell
git add requirements.txt xflow/dependencies.py tests/python-core.py
git commit -m "feat(check): 增加建议性依赖清单校验"
```

---

### Task 2: Expose `devctl check dependencies`

**Files:**
- Modify: `xflow/cli.py`
- Modify: `help.txt`
- Modify: `README.md`
- Modify: `tests/entrypoint-routing.py`
- Modify: `tests/python-core.py`

- [ ] **Step 1: Add failing CLI routing tests**

Assert these commands:

```powershell
python -m xflow check dependencies --issue IK152D
python -m xflow check dependencies --issue IK152D --file .xflow/issues/issue-IK152D/dependencies.yaml
```

The valid fixture exits 0 and prints the checked path. The fixture with an active dependency also prints `[WARN]` but still exits 0. Invalid structure exits non-zero.

- [ ] **Step 2: Run the routing tests and confirm failure**

Run: `python tests/entrypoint-routing.py`

Expected: argparse rejects `dependencies`.

- [ ] **Step 3: Add parser and dispatcher entries**

In `build_parser()` add:

```python
dependencies = check_sub.add_parser("dependencies")
dependencies.add_argument("--issue", required=True)
dependencies.add_argument("--file", type=Path)
```

In `run_check()`, call `check_dependencies`, print every warning as `[WARN] ...`, then print the existing successful check line. Warnings do not change the zero exit status.

- [ ] **Step 4: Update help and README**

Document:

- The default path `.xflow/issues/issue-IK152D/dependencies.yaml`.
- `active` and `available` are warnings, not development gates.
- `integrated` requires fresh parent-side evidence.
- Remote dependency Issue creation still requires human approval.
- Examples for one numeric and one alphanumeric Issue ID.

- [ ] **Step 5: Run tests**

Run: `python tests/python-core.py`

Run: `python tests/entrypoint-routing.py`

Expected: both exit 0.

- [ ] **Step 6: Commit the CLI surface**

```powershell
git add xflow/cli.py help.txt README.md tests/entrypoint-routing.py tests/python-core.py
git commit -m "feat(cli): 暴露依赖清单检查命令"
```

---

### Task 3: Make resolution reports account for dependency closure

**Files:**
- Modify: `xflow/dependencies.py`
- Modify: `xflow/checks.py`
- Modify: `tests/python-core.py`
- Modify: `README.md`
- Modify: `help.txt`

- [ ] **Step 1: Add failing closure matrix tests**

Extend resolution-report fixtures with these outcomes:

| Report conclusion | Dependency state | Closure assessment | Expected |
|---|---|---|---|
| `resolved` | `integrated` | `affectsClosure: true`, `decision: integrated` | pass |
| `resolved` | `available` | `affectsClosure: true` | fail |
| `resolved` | `active` | `affectsClosure: false`, `decision: not-required`, non-empty rationale | pass |
| `resolved` | `active` | missing closure assessment | fail |
| `resolved` | `superseded` | `decision: superseded`, non-empty rationale | pass |
| `reduced` | `active` | impact recorded | pass |
| `blocked` | `active` | impact recorded | pass |

Also assert that a dependency's own resolution report is not accepted as parent integration evidence.

- [ ] **Step 2: Run the failing tests**

Run: `python tests/python-core.py`

Expected: current `check_resolution_report` ignores `dependencies.yaml`, so at least the invalid resolved cases pass unexpectedly and the test fails.

- [ ] **Step 3: Add a closure helper**

Implement:

```python
check_dependency_closure(
    result: DependencyCheckResult,
    conclusion: str,
) -> tuple[str, ...]
```

Rules:

- If `dependencies.yaml` is absent, keep current behavior.
- Every dependency needs `closureAssessment` when conclusion is `resolved`.
- `affectsClosure: true` supports `resolved` only for `integrated`, or `superseded` with closure decision `superseded` and a reason.
- `affectsClosure: false` requires closure decision `not-required` and a non-empty rationale.
- `reduced` and `blocked` may retain active dependencies, but structurally invalid closure data still fails.
- Do not inspect remote provider state; validate only repository-owned artifacts.

- [ ] **Step 4: Integrate with `check_resolution_report`**

After parsing `Closure Conclusion`, load the default dependency file when it exists, call `check_dependencies`, and then call `check_dependency_closure`. Raise one actionable `ValueError` containing dependency Issue IDs for closure violations.

- [ ] **Step 5: Update user-facing documentation**

Explain that dependency checks are advisory during work but become evidence consistency checks when a report claims `resolved`.

- [ ] **Step 6: Run tests**

Run: `python tests/python-core.py`

Expected: all closure matrix cases pass.

- [ ] **Step 7: Commit the closure integration**

```powershell
git add xflow/dependencies.py xflow/checks.py tests/python-core.py README.md help.txt
git commit -m "feat(report): 校验主 Issue 的依赖闭环"
```

---

### Task 4: Implement the portable commit-message policy

**Files:**
- Create: `xflow/commit_message.py`
- Modify: `xflow/cli.py`
- Modify: `tests/python-core.py`
- Modify: `tests/entrypoint-routing.py`

- [ ] **Step 1: Add failing validator tests**

Test this public API:

```python
from xflow.commit_message import check_commit_message

check_commit_message(
    "feat(canvas): 修复稳定端点定位[#IK152D]\n\n"
    "- 调整统一端点计算\n"
    "- 覆盖 C-004 并记录测试证据\n",
    branch_issue="IK152D",
)
```

Cover:

- Gitee alphanumeric and GitHub numeric IDs.
- Missing scope, missing Issue tag, English-only subject, one-line body, and non-Chinese body fail.
- At least two non-empty Chinese-dominant body lines pass.
- Non-`merge` subjects with two Issue IDs fail.
- `merge(canvas): 集成统一容器事务能力[#IK152D][#IK17AW]` with two IDs passes.
- The first ID must match `--issue` when that argument is supplied.
- AI trailers, absolute Windows paths, and provider metadata fail.

Use this interface:

```python
check_commit_message(
    message: str,
    branch_issue: str | None = None,
) -> tuple[str, ...]
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python tests/python-core.py`

Expected: import failure for `xflow.commit_message`.

- [ ] **Step 3: Implement the validator**

Parse the first line with a compiled regex that captures `type`, non-empty `scope`, Chinese-dominant summary, and one or two contiguous `[#ID]` suffixes. Accept exactly `feat|fix|refactor|perf|test|docs|build|ci|chore|revert|style|merge`. Preserve Issue IDs exactly; accept `[A-Za-z0-9._-]+` after `#`.

Require a blank separator and at least two non-empty Chinese-dominant body lines. Reject AI client trailers including `Co-authored-by: Cursor`, `Co-authored-by: Claude`, `Co-authored-by: Gemini`, `Generated-by:`, and `OpenAI-Codex`; also reject drive-letter absolute paths.

- [ ] **Step 4: Add the CLI check**

Expose:

```powershell
python -m xflow check commit-msg --file .xflow/local/commit-message.txt --issue IK152D
```

The CLI reads UTF-8 with optional BOM, validates, prints associated Issue IDs, and exits non-zero with the validator message on failure.

- [ ] **Step 5: Update generated commit messages**

Change the generator interface to preserve the positional summary while enforcing a complete message:

```python
summarize_commit_message(
    repo_root: Path,
    full_message: str | None,
    summary_override: str | None,
) -> str
```

`-m/--message` is a complete multi-line message and must pass `check_commit_message` unchanged. The positional `summary` is only the Chinese core summary; the generator combines it with the inferred type/scope, current branch Issue tag, and two generated Chinese body lines. With neither override, infer the summary from changed paths as today.

Replace the old body-only association:

```text
type(scope): 中文摘要

关联 issue: #IK152D
```

to:

```text
type(scope): 中文核心摘要[#IK152D]

- 中文说明修改范围
- 中文说明验证结果和证据
```

Obtain the current branch Issue from branch metadata and fail with an actionable message when no Issue identity is available. Validate the complete result before invoking Git. This keeps `devctl git commit-msg 修复稳定端点定位` usable while preventing that positional summary from bypassing the required title/body structure.

- [ ] **Step 6: Make PR state-backfill commits compliant**

Change:

```python
commit_and_push_pr_backfill(
    repo_root: Path,
    branch: str,
    paths: list[Path],
    pr_number: str,
    issue: str,
) -> bool
```

Generate this shape:

```text
chore(xflow): 回填合并请求状态[#IK152D]

- 记录合并请求编号与远端链接
- 同步当前任务状态文件
```

Pass the already known Issue from the MR creation flow and run `check_commit_message` before committing.

- [ ] **Step 7: Run tests**

Run: `python tests/python-core.py`

Run: `python tests/entrypoint-routing.py`

Expected: validator, generator, CLI, and PR backfill tests all pass.

- [ ] **Step 8: Commit the commit policy implementation**

```powershell
git add xflow/commit_message.py xflow/cli.py tests/python-core.py tests/entrypoint-routing.py
git commit -m "feat(git): 校验中文多行 Issue 关联提交"
```

---

### Task 5: Align help, README, and command discoverability

**Files:**
- Modify: `README.md`
- Modify: `help.txt`
- Modify: `tests/python-core.py`
- Modify: `tests/entrypoint-routing.py`

- [ ] **Step 1: Add failing documentation anchors**

Assert that both help surfaces contain:

```text
devctl check dependencies --issue IK152D
devctl check commit-msg --file .xflow/local/commit-message.txt --issue IK152D
type(scope): 中文核心摘要[#Issue编号]
active dependencies warn but do not block local development
```

Also assert `devctl --help`, `devctl check --help`, and PowerShell `devctl.ps1 check --help` list both commands.

- [ ] **Step 2: Update the documentation**

Include copy-pasteable examples for:

- Main feature commit.
- Child-feature commit that links its parent in the body.
- Shared-infrastructure commit that lists known consumers in the body.
- Two-Issue integration commit.
- Dependency validation with warnings.
- Resolution-report validation after integration.

Make clear that `check commit-msg` is a real command now, not only a prose recommendation.

- [ ] **Step 3: Run routing and core tests**

Run: `python tests/python-core.py`

Run: `python tests/entrypoint-routing.py`

Expected: both exit 0.

- [ ] **Step 4: Commit documentation alignment**

```powershell
git add README.md help.txt tests/python-core.py tests/entrypoint-routing.py
git commit -m "docs(devctl): 补齐依赖与提交检查示例"
```

---

### Task 6: Full verification and compatibility audit

**Files:**
- Verify: all files changed above

- [ ] **Step 1: Install the declared test dependency**

Run: `python -m pip install -r requirements.txt`

Expected: PyYAML 6.x is available. If already installed, pip reports it as satisfied.

- [ ] **Step 2: Run the full suite**

Run: `python tests/python-core.py`

Expected: exit code 0.

Run: `python tests/entrypoint-routing.py`

Expected: exit code 0.

Run: `git diff --check HEAD~5..HEAD`

Expected: no whitespace errors.

- [ ] **Step 3: Verify help without relying on shell wrappers**

Run: `python -m xflow --help`

Run: `python -m xflow check --help`

Expected: both dependency and commit-message checks are discoverable; no removed app lifecycle commands reappear.

- [ ] **Step 4: Exercise the advisory boundary manually**

Create a temporary fixture with an `active` dependency and `decision: continue`, then run `python -m xflow check dependencies --issue IK152D`.

Expected: `[WARN]` is printed and exit code remains 0.

Create a `resolved` report over the same unresolved `affectsClosure: true` dependency.

Expected: `check resolution-report` exits non-zero and names `IK17AW` as the unresolved closure dependency.

- [ ] **Step 5: Inspect repository state**

Run: `git status --short`

Expected: clean except for the intentionally uncommitted implementation-plan document, if the executor chose not to commit plans.

- [ ] **Step 6: Report residual boundaries**

State explicitly in the completion report:

- devctl validates structure, traceability, and evidence paths, not business truth.
- Human reviewers still decide whether a dependency should exist, whether it blocks, and whether evidence is sufficient.
- No remote Issue, push, MR, merge, or comment is performed by these checks.
