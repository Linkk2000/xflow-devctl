from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import os
from pathlib import Path

import yaml

OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.contracts import diff_contracts, load_contract


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    (repo / "README.md").write_text("# Contract diff fixture\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize contract diff fixture", "-q")
    (repo / ".xflow").mkdir()
    (repo / ".xflow" / "xflow.json").write_text('{"contracts":{"root":"contracts"}}\n', encoding="utf-8")
    return repo


def copied_contract(repo: Path, fixture: str, name: str | None = None) -> Path:
    target = repo / "contracts" / (name or fixture)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture, target)
    return target


def write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")


def run_devctl(repo_root: Path, *args: str, expect: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env={**os.environ, "DEVCTL_REPO_ROOT": str(repo_root), "PYTHONPATH": str(OPS_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != expect:
        raise AssertionError(f"expected exit {expect}: {' '.join(args)}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


def test_required_evolution_bumps(repo: Path) -> None:
    base = load_contract(repo, copied_contract(repo, "valid.yaml"))
    patch = load_contract(repo, copied_contract(repo, "patch.yaml"))
    minor = load_contract(repo, copied_contract(repo, "minor.yaml"))
    major = load_contract(repo, copied_contract(repo, "major.yaml"))
    implementation_gap = load_contract(repo, copied_contract(repo, "implementation-gap-unchanged.yaml"))

    assert diff_contracts(base, patch).required_bump == "patch"
    assert diff_contracts(base, minor).required_bump == "minor"
    assert diff_contracts(base, major).required_bump == "major"
    assert diff_contracts(base, implementation_gap).required_bump == "none"
    assert diff_contracts(base, implementation_gap).actual_bump == "none"
    assert diff_contracts(base, implementation_gap).unchanged == tuple(sorted(base.objects_by_id))
    major_impacts = "\n".join(diff_contracts(base, major).review_impacts)
    assert "affected verification IDs: example.verify.case.operation-rejection, example.verify.case.operation-success" in major_impacts
    assert "affected projection IDs: example.projection.primary-implementation" in major_impacts


def test_rejects_stale_versions_and_unlinked_replacements(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)

    stale_path = copied_contract(repo, "major.yaml")
    stale_payload = yaml.safe_load(stale_path.read_text(encoding="utf-8"))
    stale_payload["semanticValueContracts"][1]["version"] = "0.1.0"
    write(stale_path, stale_payload)
    stale = load_contract(repo, stale_path)
    stale_diff = diff_contracts(base, stale)
    assert stale_diff.required_bump == "major"
    assert any("example.value.result" in impact for impact in stale_diff.review_impacts)
    result = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(stale_path), expect=1)
    assert "under-bumped objects:" in result.stdout, result.stdout + result.stderr
    assert "example.value.result" in result.stdout

    renamed_path = copied_contract(repo, "valid.yaml", "renamed.yaml")
    renamed_payload = yaml.safe_load(renamed_path.read_text(encoding="utf-8"))
    renamed_payload["version"] = "0.2.0"
    renamed_payload["futureCapabilitiesOutOfScope"][0]["id"] = "example.future.optional-extension-v2"
    write(renamed_path, renamed_payload)
    renamed = load_contract(repo, renamed_path)
    renamed_diff = diff_contracts(base, renamed)
    assert renamed_diff.required_bump == "human-review"
    assert "stable-ID replacement without valid supersedes" in "\n".join(renamed_diff.review_impacts)
    result = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(renamed_path), expect=1)
    assert "[ERROR]" in result.stdout


def test_accepts_historical_supersedes_and_prints_stable_impacts(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)
    successor_path = copied_contract(repo, "valid.yaml", "successor.yaml")
    successor_payload = yaml.safe_load(successor_path.read_text(encoding="utf-8"))
    successor_payload["version"] = "0.2.0"
    successor_payload["futureCapabilitiesOutOfScope"][0]["id"] = "example.future.optional-extension-v2"
    successor_payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = ["example.future.optional-extension"]
    write(successor_path, successor_payload)
    successor = load_contract(repo, successor_path)
    diff = diff_contracts(base, successor)
    assert diff.added == ("example.future.optional-extension-v2",)
    assert diff.removed == ("example.future.optional-extension",)
    assert not any("stable-ID replacement" in impact for impact in diff.review_impacts)
    result = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(successor_path), expect=0)
    repeated = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(successor_path), expect=0)
    assert result.stdout == repeated.stdout
    assert "added: example.future.optional-extension-v2" in result.stdout
    assert "affected verification IDs: none" in result.stdout
    assert "affected projection IDs: none" in result.stdout


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        test_required_evolution_bumps(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_rejects_stale_versions_and_unlinked_replacements(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_accepts_historical_supersedes_and_prints_stable_impacts(init_repo(Path(raw)))
    print("contract diff ok")


if __name__ == "__main__":
    main()
