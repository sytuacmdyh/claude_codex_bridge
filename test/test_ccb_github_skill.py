from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "dev_tools" / "skills" / "ccb-github" / "scripts" / "check_release_state.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("ccb_github_release_checker", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo_with_remote(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "ccb@example.invalid")
    _git(repo, "config", "user.name", "CCB Test")
    (repo / "file.txt").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "initial")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo


def test_check_local_git_state_warns_before_publish_but_fails_after_publish(tmp_path: Path) -> None:
    checker = _load_checker()
    repo = _init_repo_with_remote(tmp_path)
    (repo / "file.txt").write_text("updated\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "local-only")

    issues: list[str] = []
    warnings: list[str] = []
    checker.check_local_git_state(repo, "prepare", issues, warnings)
    assert not issues
    assert any("unpushed commits" in item for item in warnings)

    issues = []
    warnings = []
    checker.check_local_git_state(repo, "published", issues, warnings)
    assert any("unpushed commits" in item for item in issues)

    issues = []
    warnings = []
    checker.check_local_git_state(repo, "dev", issues, warnings)
    assert any("unpushed commits" in item for item in issues)


def test_check_local_git_state_fails_dirty_worktree_after_publish(tmp_path: Path) -> None:
    checker = _load_checker()
    repo = _init_repo_with_remote(tmp_path)
    (repo / "file.txt").write_text("dirty\n", encoding="utf-8")

    issues: list[str] = []
    warnings: list[str] = []
    checker.check_local_git_state(repo, "published", issues, warnings)

    assert any("Worktree has uncommitted changes" in item for item in issues)

    issues = []
    warnings = []
    checker.check_local_git_state(repo, "dev", issues, warnings)
    assert any("Worktree has uncommitted changes" in item for item in issues)


def test_default_branch_compare_rejects_release_tag_missing_from_main(monkeypatch, tmp_path: Path) -> None:
    checker = _load_checker()

    def fake_run(cmd: list[str], cwd: Path):
        assert cmd[:2] == ["gh", "api"]
        assert cmd[2] == "repos/SeemSeam/claude_codex_bridge/compare/v9.9.9...main"
        return subprocess.CompletedProcess(cmd, 0, stdout="diverged\n", stderr="")

    monkeypatch.setattr(checker, "run", fake_run)
    issues: list[str] = []
    warnings: list[str] = []

    checker.check_default_branch_contains_release(
        root=tmp_path,
        version="v9.9.9",
        repo="SeemSeam/claude_codex_bridge",
        default_branch="main",
        issues=issues,
        warnings=warnings,
    )

    assert any("does not contain release tag v9.9.9" in item for item in issues)


def test_default_branch_compare_accepts_ahead_or_identical(monkeypatch, tmp_path: Path) -> None:
    checker = _load_checker()

    def fake_run(cmd: list[str], cwd: Path):
        return subprocess.CompletedProcess(cmd, 0, stdout="ahead\n", stderr="")

    monkeypatch.setattr(checker, "run", fake_run)
    issues: list[str] = []
    warnings: list[str] = []

    checker.check_default_branch_contains_release(
        root=tmp_path,
        version="v9.9.9",
        repo="SeemSeam/claude_codex_bridge",
        default_branch="main",
        issues=issues,
        warnings=warnings,
    )

    assert not issues


def test_dev_change_classification_flags_release_and_dev_only_paths() -> None:
    checker = _load_checker()

    assert checker.classify_dev_path("dev_tools/skills/ccb-github/SKILL.md") == "dev_tools"
    assert checker.classify_dev_path("test/test_ccb_github_skill.py") == "verification"
    assert checker.classify_dev_path(".github/workflows/test.yml") == "verification"
    assert checker.classify_dev_path("docs/plan.md") == "docs"
    assert checker.classify_dev_path("README.md") == "homepage"
    assert checker.classify_dev_path("CHANGELOG.md") == "release_notes"
    assert checker.classify_dev_path("lib/runtime.py") == "runtime_package"
    assert checker.classify_dev_path("ccb") == "runtime_package"


def test_required_dev_workflows_depend_on_branch() -> None:
    checker = _load_checker()

    assert checker.required_dev_workflows("main", "main") == {
        "Tests",
        "CCBD Real Platform Smoke",
        "Cross-Platform Compatibility Test",
    }
    assert checker.required_dev_workflows("feature/x", "main") == {
        "Tests",
        "CCBD Real Platform Smoke",
    }
