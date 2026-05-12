#!/usr/bin/env python3
"""Read-only release surface checker for the CCB GitHub project."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


EXPECTED_ASSETS = {
    "ccb-linux-x86_64.tar.gz",
    "ccb-macos-universal.tar.gz",
    "SHA256SUMS",
}
CHECKSUMMED_ASSETS = EXPECTED_ASSETS - {"SHA256SUMS"}
REQUIRED_TAG_WORKFLOWS = {"Release Artifacts"}
BRANCH_VALIDATION_WORKFLOWS = {
    "Tests",
    "CCBD Real Platform Smoke",
    "Cross-Platform Compatibility Test",
}
DEV_STRICT_PHASES = {"dev", "published"}
DEV_ALWAYS_REQUIRED_WORKFLOWS = {
    "Tests",
    "CCBD Real Platform Smoke",
}
DEV_DEFAULT_BRANCH_WORKFLOWS = {
    "Cross-Platform Compatibility Test",
}
DEV_RELEASE_TRIGGER_PATHS = {
    "VERSION",
    "ccb",
}
DEV_HOMEPAGE_PATHS = {
    "README.md",
    "README_zh.md",
}


def run(cmd: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = stderr or f"command timed out after {timeout}s"
        return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=detail)


def repo_root(start: Path) -> Path:
    proc = run(["git", "rev-parse", "--show-toplevel"], start)
    if proc.returncode == 0:
        return Path(proc.stdout.strip())
    return start.resolve()


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def fail(issues: list[str], message: str, *, fix: str | None = None) -> None:
    if fix:
        issues.append(f"FAIL: {message}\n      fix: {fix}")
    else:
        issues.append(f"FAIL: {message}")


def warn(warnings: list[str], message: str) -> None:
    warnings.append(f"WARN: {message}")


def infer_repo(root: Path) -> str:
    proc = run(["git", "remote", "get-url", "origin"], root)
    if proc.returncode != 0:
        return "SeemSeam/claude_codex_bridge"
    url = proc.stdout.strip()
    match = re.search(r"github.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return "SeemSeam/claude_codex_bridge"


def markdown_section(body: str, heading: str) -> str | None:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}[^\n]*\n(?P<body>.*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(body)
    if not match:
        return None
    return match.group("body").strip()


def readme_release_block(body: str, version: str) -> str | None:
    pattern = re.compile(
        rf"(?ms)<summary><b>{re.escape(version)}</b>.*?</summary>(?P<body>.*?)(?=</details>)"
    )
    match = pattern.search(body)
    if not match:
        return None
    return match.group("body").strip()


def has_substantive_release_text(text: str | None) -> bool:
    if not text:
        return False
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("<!--", "-->", "<details", "</details>", "<summary", "</summary>")):
            continue
        cleaned_lines.append(stripped)
    return any(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", line) for line in cleaned_lines)


def semver_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(version or "").strip())
    if not match:
        return (-1, -1, -1)
    return tuple(int(part) for part in match.groups())


def release_note_versions(body: str) -> list[str]:
    return re.findall(r"<summary><b>(v\d+\.\d+\.\d+)</b>", body)


def install_section(body: str, heading: str) -> str:
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)")
    match = pattern.search(body)
    return match.group("body") if match else body


def git_output(root: Path, cmd: list[str]) -> str | None:
    proc = run(["git", *cmd], root)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def check_local_git_state(root: Path, phase: str, issues: list[str], warnings: list[str]) -> None:
    status = git_output(root, ["status", "--porcelain"])
    if status:
        message = "Worktree has uncommitted changes"
        fix = "commit or intentionally discard local changes before reporting a final dev or release result"
        if phase in DEV_STRICT_PHASES:
            fail(issues, message, fix=fix)
        else:
            warn(warnings, f"{message}; {fix}")

    branch = git_output(root, ["branch", "--show-current"])
    if not branch:
        warn(warnings, "Detached HEAD; branch push/merge state cannot be checked")
        return

    upstream = git_output(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not upstream:
        message = f"Current branch {branch} has no upstream"
        fix = f"push it with upstream tracking: git push -u origin {branch}"
        if phase in DEV_STRICT_PHASES:
            fail(issues, message, fix=fix)
        else:
            warn(warnings, f"{message}; {fix}")
        return

    local = git_output(root, ["rev-parse", "HEAD"])
    remote = git_output(root, ["rev-parse", "@{u}"])
    merge_base = git_output(root, ["merge-base", "HEAD", "@{u}"])
    if not local or not remote or not merge_base or local == remote:
        return

    if merge_base == remote:
        message = f"Current branch {branch} has unpushed commits relative to {upstream}"
        fix = f"push the branch before continuing: git push"
        if phase in DEV_STRICT_PHASES:
            fail(issues, message, fix=fix)
        else:
            warn(warnings, f"{message}; {fix}")
    elif merge_base == local:
        warn(warnings, f"Current branch {branch} is behind {upstream}; pull/rebase before release work if this is unexpected")
    else:
        message = f"Current branch {branch} has diverged from {upstream}"
        fix = "reconcile the branch with its upstream before publishing"
        if phase in DEV_STRICT_PHASES:
            fail(issues, message, fix=fix)
        else:
            warn(warnings, f"{message}; {fix}")


def dev_changed_paths(root: Path) -> list[str]:
    paths: set[str] = set()
    for cmd in (
        ["diff", "--name-only"],
        ["diff", "--cached", "--name-only"],
    ):
        output = git_output(root, cmd)
        if output:
            paths.update(item.strip() for item in output.splitlines() if item.strip())

    upstream = git_output(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream:
        output = git_output(root, ["diff", "--name-only", f"{upstream}...HEAD"])
        if output:
            paths.update(item.strip() for item in output.splitlines() if item.strip())
    return sorted(paths)


def classify_dev_path(path: str) -> str:
    if path.startswith("dev_tools/"):
        return "dev_tools"
    if path.startswith("test/") or path.startswith(".github/workflows/"):
        return "verification"
    if path.startswith("docs/"):
        return "docs"
    if path in DEV_HOMEPAGE_PATHS:
        return "homepage"
    if path == "CHANGELOG.md":
        return "release_notes"
    if path in DEV_RELEASE_TRIGGER_PATHS or path.startswith("lib/") or path.startswith("bin/") or path.startswith("scripts/"):
        return "runtime_package"
    return "other"


def check_dev_change_set(root: Path, warnings: list[str]) -> None:
    paths = dev_changed_paths(root)
    if not paths:
        warn(warnings, "No local/branch delta found relative to upstream; dev check is validating git/GitHub state only")
        return
    categories: dict[str, int] = {}
    for path in paths:
        categories[classify_dev_path(path)] = categories.get(classify_dev_path(path), 0) + 1
    summary = ", ".join(f"{name}={count}" for name, count in sorted(categories.items()))
    warn(warnings, f"Development change classification: {summary}")
    if "runtime_package" in categories:
        warn(warnings, "Runtime/package files changed; decide whether this should become a versioned release before calling the work complete")
    if "homepage" in categories:
        warn(warnings, "Homepage README files changed; push/merge to the default branch before expecting GitHub's homepage to update")
    if "release_notes" in categories:
        warn(warnings, "CHANGELOG changed; if this is a public package change, use prepare/published release phases")
    if set(categories).issubset({"dev_tools", "verification", "docs"}):
        warn(warnings, "Change set appears development-only; a release tag is usually not needed")


def check_git_tag(root: Path, version: str, phase: str, issues: list[str], warnings: list[str]) -> None:
    local_commit = git_output(root, ["rev-list", "-n", "1", version])
    if phase == "prepare":
        if local_commit:
            warn(warnings, f"Local tag {version} already exists at {local_commit}; confirm this is intentional before publishing")
        return

    if not local_commit:
        fail(
            issues,
            f"Local git tag {version} does not exist",
            fix=f"create the tag on the intended release commit: git tag {version} && git push origin {version}",
        )
        return

    remote = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{version}^{{}}"], root)
    remote_sha = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.strip() else ""
    if not remote_sha:
        remote = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{version}"], root)
        remote_sha = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.strip() else ""

    if not remote_sha:
        fail(
            issues,
            f"Remote git tag {version} is missing on origin",
            fix=f"push the tag: git push origin {version}",
        )
        return

    if remote_sha != local_commit:
        fail(
            issues,
            f"Remote tag {version} points to {remote_sha}, but local tag resolves to {local_commit}",
            fix="stop and inspect the tag mismatch; do not force-push release tags without maintainer approval",
        )


def check_local_files(root: Path, version: str, repo: str, issues: list[str], warnings: list[str]) -> None:
    bare_version = version.removeprefix("v")
    files = {
        "VERSION": read(root / "VERSION"),
        "ccb": read(root / "ccb"),
        "CHANGELOG.md": read(root / "CHANGELOG.md"),
        "README.md": read(root / "README.md"),
        "README_zh.md": read(root / "README_zh.md"),
    }

    if files["VERSION"].strip() != bare_version:
        fail(issues, f"VERSION is {files['VERSION'].strip()!r}, expected {bare_version!r}", fix=f"write exactly {bare_version} to VERSION")
    if f'VERSION = "{bare_version}"' not in files["ccb"]:
        fail(issues, f"ccb does not contain VERSION = {bare_version!r}", fix=f'update ccb to VERSION = "{bare_version}"')

    changelog_section = markdown_section(files["CHANGELOG.md"], version)
    if changelog_section is None:
        fail(issues, f"CHANGELOG.md has no {version} section", fix=f"add a non-empty ## {version} (...) section near the top of CHANGELOG.md")
    elif not has_substantive_release_text(changelog_section):
        fail(issues, f"CHANGELOG.md {version} section is empty", fix="add concrete user-facing release bullets before publishing")

    for readme_name in ("README.md", "README_zh.md"):
        body = files[readme_name]
        versions = release_note_versions(body)
        if versions:
            if versions[0] != version:
                fail(
                    issues,
                    f"{readme_name} first release notes entry is {versions[0]}, expected {version}",
                    fix=f"move the {version} release notes entry above older versions",
                )
            sorted_versions = sorted(versions, key=semver_tuple, reverse=True)
            if versions != sorted_versions:
                warn(warnings, f"{readme_name} release notes are not in descending semver order")
        if f"version-{bare_version}-orange.svg" not in body:
            fail(issues, f"{readme_name} version badge does not show {bare_version}", fix=f"update the top badge to version-{bare_version}-orange.svg")
        if f"<summary><b>{version}</b>" not in body:
            fail(issues, f"{readme_name} release notes do not include {version}", fix=f"add a non-empty {version} entry to Release Notes / 新版本记录")
        elif not has_substantive_release_text(readme_release_block(body, version)):
            fail(issues, f"{readme_name} release notes entry for {version} is empty", fix="add concrete release bullets under the details block")
        if ".ccb/ccb_memory.md" not in body:
            fail(issues, f"{readme_name} does not mention .ccb/ccb_memory.md", fix="state that .ccb/ccb_memory.md is the project-wide shared memory document")

        badge_versions = sorted(set(re.findall(r"version-([0-9]+\.[0-9]+\.[0-9]+)-orange\.svg", body)))
        stale_badges = [item for item in badge_versions if item != bare_version]
        if stale_badges:
            fail(issues, f"{readme_name} has stale version badges: {', '.join(stale_badges)}", fix=f"replace stale current badges with {bare_version}")

    owner, name = repo.split("/", 1)
    expected_clone = f"https://github.com/{owner}/{name}.git"
    readme_install_headings = {
        "README.md": "How to Install",
        "README_zh.md": "如何安装",
    }
    for readme_name, heading in readme_install_headings.items():
        body = files[readme_name]
        install_body = install_section(body, heading)
        clone_urls = sorted(set(re.findall(r"git\s+clone\s+(https://github\.com/[^\s`]+\.git)", install_body)))
        wrong_urls = [url for url in clone_urls if url != expected_clone]
        if wrong_urls:
            fail(issues, f"{readme_name} has clone URL(s) not matching {expected_clone}: {', '.join(wrong_urls)}", fix=f"replace README clone URLs with {expected_clone}")

    if "CCB.md" in files["README.md"] or "CCB.md" in files["README_zh.md"]:
        fail(issues, "README mentions current CCB.md support; current design must only use .ccb/ccb_memory.md", fix="remove current-feature references to CCB.md; keep only .ccb/ccb_memory.md")

    warn(warnings, "Manually inspect README What's New / 最新亮点 for stale prose; this cannot be proven by version regex alone")


def check_readme_surface(
    *,
    body: str,
    readme_name: str,
    version: str,
    repo: str,
    source: str,
    issues: list[str],
    warnings: list[str],
) -> None:
    bare_version = version.removeprefix("v")
    versions = release_note_versions(body)
    if versions:
        if versions[0] != version:
            fail(
                issues,
                f"{source} {readme_name} first release notes entry is {versions[0]}, expected {version}",
                fix="merge/push the release documentation changes to the default branch",
            )
        sorted_versions = sorted(versions, key=semver_tuple, reverse=True)
        if versions != sorted_versions:
            warn(warnings, f"{source} {readme_name} release notes are not in descending semver order")
    else:
        fail(
            issues,
            f"{source} {readme_name} has no release notes version entries",
            fix="merge/push a README with current release notes to the default branch",
        )

    if f"version-{bare_version}-orange.svg" not in body:
        fail(
            issues,
            f"{source} {readme_name} version badge does not show {bare_version}",
            fix="merge/push the release README badge update to the default branch",
        )
    if f"<summary><b>{version}</b>" not in body:
        fail(
            issues,
            f"{source} {readme_name} release notes do not include {version}",
            fix="merge/push release notes for the current version to the default branch",
        )
    elif not has_substantive_release_text(readme_release_block(body, version)):
        fail(
            issues,
            f"{source} {readme_name} release notes entry for {version} is empty",
            fix="add concrete release bullets before calling the homepage updated",
        )
    if ".ccb/ccb_memory.md" not in body:
        fail(
            issues,
            f"{source} {readme_name} does not mention .ccb/ccb_memory.md",
            fix="keep the shared memory wording in the default-branch README",
        )

    owner, name = repo.split("/", 1)
    expected_clone = f"https://github.com/{owner}/{name}.git"
    install_heading = "如何安装" if readme_name == "README_zh.md" else "How to Install"
    install_body = install_section(body, install_heading)
    clone_urls = sorted(set(re.findall(r"git\s+clone\s+(https://github\.com/[^\s`]+\.git)", install_body)))
    wrong_urls = [url for url in clone_urls if url != expected_clone]
    if wrong_urls:
        fail(
            issues,
            f"{source} {readme_name} has clone URL(s) not matching {expected_clone}: {', '.join(wrong_urls)}",
            fix=f"replace default-branch README install clone URLs with {expected_clone}",
        )

    if "CCB.md" in body:
        fail(
            issues,
            f"{source} {readme_name} mentions current CCB.md support",
            fix="default-branch README should describe only .ccb/ccb_memory.md as current shared memory",
        )


def gh_api_text(root: Path, path: str) -> str | None:
    proc = run(["gh", "api", path, "--jq", ".content"], root)
    if proc.returncode != 0:
        return None
    try:
        return base64.b64decode(proc.stdout.encode("utf-8"), validate=False).decode("utf-8", errors="replace")
    except Exception:
        return None


def check_remote_homepage(
    *,
    root: Path,
    version: str,
    repo: str,
    default_branch: str,
    issues: list[str],
    warnings: list[str],
) -> None:
    if not default_branch:
        warn(warnings, "Could not determine GitHub default branch; homepage README was not checked")
        return
    for readme_name in ("README.md", "README_zh.md"):
        body = gh_api_text(root, f"repos/{repo}/contents/{readme_name}?ref={default_branch}")
        if body is None:
            fail(
                issues,
                f"Could not read {readme_name} from GitHub default branch {default_branch}",
                fix="confirm gh auth/repo access and that the default branch contains the README",
            )
            continue
        check_readme_surface(
            body=body,
            readme_name=readme_name,
            version=version,
            repo=repo,
            source=f"GitHub default branch {default_branch}",
            issues=issues,
            warnings=warnings,
        )


def check_sha256sums(root: Path, version: str, repo: str, issues: list[str], warnings: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="ccb-release-check-") as tmp:
        tmp_path = Path(tmp)
        download = run(
            [
                "gh",
                "release",
                "download",
                version,
                "--repo",
                repo,
                "--pattern",
                "SHA256SUMS",
                "--dir",
                str(tmp_path),
            ],
            root,
        )
        if download.returncode != 0:
            fail(
                issues,
                "Could not download SHA256SUMS from the GitHub release",
                fix="rerun Release Artifacts or re-upload SHA256SUMS, then rerun the published check",
            )
            return
        sums_path = tmp_path / "SHA256SUMS"
        payload = read(sums_path)
        found: dict[str, str] = {}
        for line in payload.splitlines():
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            digest, name = parts
            if re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                found[Path(name).name] = digest.lower()
        missing = sorted(CHECKSUMMED_ASSETS - set(found))
        if missing:
            fail(
                issues,
                f"SHA256SUMS is missing checksum entry/entries for: {', '.join(missing)}",
                fix="rerun Release Artifacts so SHA256SUMS is regenerated from the uploaded tarballs",
            )
        extra = sorted(set(found) - CHECKSUMMED_ASSETS)
        if extra:
            warn(warnings, f"SHA256SUMS contains unexpected extra asset checksum(s): {', '.join(extra)}")


def check_default_branch_contains_release(
    *,
    root: Path,
    version: str,
    repo: str,
    default_branch: str,
    issues: list[str],
    warnings: list[str],
) -> None:
    if not default_branch:
        warn(warnings, "Could not determine GitHub default branch; default-branch containment was not checked")
        return
    compare = run(
        [
            "gh",
            "api",
            f"repos/{repo}/compare/{version}...{default_branch}",
            "--jq",
            ".status",
        ],
        root,
    )
    if compare.returncode != 0:
        fail(
            issues,
            f"Could not compare release tag {version} with default branch {default_branch}",
            fix="confirm the tag exists on GitHub, then merge the release commit into the default branch if needed",
        )
        return
    status = compare.stdout.strip()
    if status not in {"identical", "ahead"}:
        fail(
            issues,
            f"GitHub default branch {default_branch} does not contain release tag {version} (compare status: {status or 'unknown'})",
            fix=f"merge the release commit/tag into {default_branch} and push; GitHub homepage README only renders from the default branch",
        )


def gh_auth_is_ready(root: Path, issues: list[str]) -> bool:
    auth = run(["gh", "auth", "status", "--hostname", "github.com"], root)
    if auth.returncode != 0:
        fail(
            issues,
            "GitHub CLI is not authenticated",
            fix="run gh auth login, then rerun the GitHub state check",
        )
        return False
    return True


def repo_default_branch(root: Path, repo: str, warnings: list[str]) -> str:
    repo_view = run(["gh", "repo", "view", repo, "--json", "defaultBranchRef"], root)
    if repo_view.returncode != 0:
        warn(warnings, f"Could not read GitHub default branch: {repo_view.stderr.strip()}")
        return ""
    try:
        payload = json.loads(repo_view.stdout)
    except json.JSONDecodeError as exc:
        warn(warnings, f"Could not parse gh repo JSON: {exc}")
        return ""
    return (payload.get("defaultBranchRef") or {}).get("name") or ""


def read_github_runs(root: Path, repo: str, limit: int = 50) -> list[dict[str, object]] | None:
    runs = run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            str(limit),
            "--json",
            "name,status,conclusion,headBranch,event,databaseId,url,headSha",
        ],
        root,
    )
    if runs.returncode != 0:
        return None
    try:
        return json.loads(runs.stdout)
    except json.JSONDecodeError:
        return None


def required_dev_workflows(branch: str, default_branch: str) -> set[str]:
    required = set(DEV_ALWAYS_REQUIRED_WORKFLOWS)
    if branch in {default_branch, "main", "dev"}:
        required.update(DEV_DEFAULT_BRANCH_WORKFLOWS)
    return required


def check_dev_branch_workflows(
    *,
    root: Path,
    repo: str,
    wait_seconds: int,
    poll_interval: int,
    issues: list[str],
    warnings: list[str],
) -> None:
    if not gh_auth_is_ready(root, issues):
        return

    branch = git_output(root, ["branch", "--show-current"]) or ""
    head = git_output(root, ["rev-parse", "HEAD"]) or ""
    upstream_head = git_output(root, ["rev-parse", "@{u}"]) or ""
    if not branch or not head:
        warn(warnings, "Could not determine current branch/head; GitHub workflow state was not checked")
        return
    if upstream_head and head != upstream_head:
        warn(warnings, "Current HEAD is not pushed to upstream; GitHub workflow state for this commit cannot be complete yet")
        return

    default_branch = repo_default_branch(root, repo, warnings)
    required = required_dev_workflows(branch, default_branch)
    if "Cross-Platform Compatibility Test" not in required:
        warn(warnings, f"Cross-Platform Compatibility Test is not required for branch {branch!r}; it only runs on main/dev, PRs, or manual dispatch")

    deadline = time.monotonic() + max(wait_seconds, 0)
    latest_by_name: dict[str, dict[str, object]] = {}
    while True:
        run_payload = read_github_runs(root, repo)
        if run_payload is None:
            fail(
                issues,
                "Could not read GitHub Actions runs for dev workflow check",
                fix="retry after GitHub/API connectivity recovers; final dev verification requires workflow status",
            )
            return
        latest_by_name = {}
        for item in run_payload:
            if item.get("headSha") != head:
                continue
            name = str(item.get("name") or "")
            if name in required and name not in latest_by_name:
                latest_by_name[name] = item

        all_done = True
        for workflow_name in required:
            item = latest_by_name.get(workflow_name)
            if not item or item.get("status") != "completed":
                all_done = False
                break
        if all_done or wait_seconds <= 0 or time.monotonic() >= deadline:
            break
        time.sleep(max(poll_interval, 1))

    for workflow_name in sorted(required):
        item = latest_by_name.get(workflow_name)
        if not item:
            fail(
                issues,
                f"No GitHub Actions run found for current commit {head[:12]}: {workflow_name}",
                fix="push the branch and wait for GitHub Actions, or confirm this workflow is intentionally not triggered",
            )
            continue
        if item.get("status") != "completed" or item.get("conclusion") != "success":
            fail(
                issues,
                f"GitHub Actions {workflow_name} for current commit is {item.get('status')}/{item.get('conclusion')}: {item.get('url')}",
                fix=f"wait for the run to complete or fix/rerun it; use --wait-seconds to let the checker wait automatically",
            )


def check_dev_state(
    *,
    root: Path,
    repo: str,
    wait_seconds: int,
    poll_interval: int,
    issues: list[str],
    warnings: list[str],
) -> None:
    check_dev_change_set(root, warnings)
    check_dev_branch_workflows(
        root=root,
        repo=repo,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        issues=issues,
        warnings=warnings,
    )


def check_github(root: Path, version: str, repo: str, issues: list[str], warnings: list[str]) -> None:
    if not gh_auth_is_ready(root, issues):
        return

    release = run(["gh", "release", "view", version, "--repo", repo, "--json", "tagName,url,assets,isDraft"], root)
    if release.returncode != 0:
        fail(
            issues,
            f"GitHub release {version} not found for {repo}: {release.stderr.strip() or release.stdout.strip()}",
            fix=f"create the release page first: gh release create {version} --repo {repo} --title {version} --notes-file <notes-file>",
        )
        return

    try:
        payload = json.loads(release.stdout)
    except json.JSONDecodeError as exc:
        fail(issues, f"Could not parse gh release JSON: {exc}")
        return

    if payload.get("tagName") != version:
        fail(issues, f"GitHub release tag is {payload.get('tagName')!r}, expected {version!r}")
    if payload.get("isDraft"):
        fail(issues, f"GitHub release {version} is still a draft", fix="publish the draft after assets and notes are ready")

    asset_names = {asset.get("name") for asset in payload.get("assets", [])}
    missing = sorted(EXPECTED_ASSETS - asset_names)
    if missing:
        fail(
            issues,
            f"GitHub release missing asset(s): {', '.join(missing)}",
            fix=f"rerun Release Artifacts for {version}, then verify assets again",
        )
    elif "SHA256SUMS" in asset_names:
        check_sha256sums(root, version, repo, issues, warnings)

    default_branch = ""
    repo_view = run(["gh", "repo", "view", repo, "--json", "description,repositoryTopics,latestRelease,url,defaultBranchRef"], root)
    if repo_view.returncode != 0:
        warn(warnings, f"Could not read GitHub repo metadata: {repo_view.stderr.strip()}")
    else:
        try:
            repo_payload = json.loads(repo_view.stdout)
        except json.JSONDecodeError as exc:
            warn(warnings, f"Could not parse gh repo JSON: {exc}")
        else:
            latest = (repo_payload.get("latestRelease") or {}).get("tagName")
            default_branch = (repo_payload.get("defaultBranchRef") or {}).get("name") or ""
            if latest != version:
                fail(issues, f"GitHub latest release is {latest!r}, expected {version!r}", fix="publish the GitHub release and ensure it is not draft/prerelease unless intended")
            description = repo_payload.get("description") or ""
            if "Claude, Codex & Gemini" in description and "OpenCode" not in description:
                warn(warnings, "GitHub description may be stale: it mentions Claude/Codex/Gemini but not newer supported providers")

    check_default_branch_contains_release(
        root=root,
        version=version,
        repo=repo,
        default_branch=default_branch,
        issues=issues,
        warnings=warnings,
    )

    check_remote_homepage(
        root=root,
        version=version,
        repo=repo,
        default_branch=default_branch,
        issues=issues,
        warnings=warnings,
    )

    runs = run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            "20",
            "--json",
            "name,status,conclusion,headBranch,event,databaseId,url,headSha",
        ],
        root,
    )
    if runs.returncode != 0:
        fail(
            issues,
            f"Could not read GitHub Actions runs: {runs.stderr.strip()}",
            fix="retry after GitHub/API connectivity recovers; final release verification requires workflow status",
        )
        return
    try:
        run_payload = json.loads(runs.stdout)
    except json.JSONDecodeError as exc:
        fail(issues, f"Could not parse gh run JSON: {exc}", fix="rerun the published check; GitHub Actions state could not be verified")
        return

    tag_commit = git_output(root, ["rev-list", "-n", "1", version]) or ""

    for workflow_name in sorted(REQUIRED_TAG_WORKFLOWS):
        candidates = [
            item
            for item in run_payload
            if item.get("name") == workflow_name and _release_artifacts_run_matches(item, version=version, tag_commit=tag_commit)
        ]
        successes = [
            item
            for item in candidates
            if item.get("status") == "completed" and item.get("conclusion") == "success"
        ]
        if successes:
            accepted = successes[0]
            if accepted.get("event") == "workflow_dispatch" and tag_commit and accepted.get("headSha") != tag_commit:
                warn(
                    warnings,
                    f"{workflow_name} was accepted from workflow_dispatch but its headSha {accepted.get('headSha')} does not match tag {tag_commit}; confirm it used input tag={version}",
                )
            continue
        if candidates:
            latest = candidates[0]
            fail(
                issues,
                f"GitHub Actions {workflow_name} for {version} is {latest.get('status')}/{latest.get('conclusion')}: {latest.get('url')}",
                fix="open the run, fix the root cause, rerun the failed workflow, and do not call the release complete while red",
            )
            continue
        fail(
            issues,
            f"Missing release workflow run for {version}: {workflow_name}",
            fix=f"push tag {version} or manually dispatch {workflow_name} with input tag={version}",
        )

    _check_branch_validation_runs(run_payload, tag_commit=tag_commit, warnings=warnings)


def _release_artifacts_run_matches(item: dict[str, object], *, version: str, tag_commit: str) -> bool:
    if item.get("headBranch") == version:
        return True
    if tag_commit and item.get("headSha") == tag_commit:
        return True
    return item.get("event") == "workflow_dispatch"


def _check_branch_validation_runs(run_payload: list[dict[str, object]], *, tag_commit: str, warnings: list[str]) -> None:
    if not tag_commit:
        return
    found: set[str] = set()
    for workflow_name in sorted(BRANCH_VALIDATION_WORKFLOWS):
        candidates = [
            item
            for item in run_payload
            if item.get("name") == workflow_name
            and item.get("headSha") == tag_commit
            and item.get("headBranch") not in {"", None}
            and not str(item.get("headBranch")).startswith("v")
        ]
        if not candidates:
            continue
        found.add(workflow_name)
        latest = candidates[0]
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            warn(
                warnings,
                f"Branch validation workflow {workflow_name} for release commit is {latest.get('status')}/{latest.get('conclusion')}: {latest.get('url')}",
            )
    missing = sorted(BRANCH_VALIDATION_WORKFLOWS - found)
    if missing:
        warn(warnings, f"No recent branch validation run found for release commit for: {', '.join(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check CCB release-facing local and GitHub state.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--repo", default=None, help="GitHub repo, e.g. SeemSeam/claude_codex_bridge")
    parser.add_argument("--version", default=None, help="Release version, with or without leading v")
    parser.add_argument("--phase", choices=("dev", "prepare", "published"), default="prepare")
    parser.add_argument("--wait-seconds", type=int, default=0, help="wait this many seconds for dev GitHub workflows")
    parser.add_argument("--poll-interval", type=int, default=30, help="poll interval in seconds when waiting for workflows")
    args = parser.parse_args()

    root = repo_root(args.repo_root)
    repo = args.repo or infer_repo(root)
    raw_version = args.version or read(root / "VERSION").strip()
    version = raw_version if raw_version.startswith("v") else f"v{raw_version}"

    issues: list[str] = []
    warnings: list[str] = []

    check_local_git_state(root, args.phase, issues, warnings)
    if args.phase == "dev":
        check_dev_state(
            root=root,
            repo=repo,
            wait_seconds=args.wait_seconds,
            poll_interval=args.poll_interval,
            issues=issues,
            warnings=warnings,
        )
    else:
        check_local_files(root, version, repo, issues, warnings)
        check_git_tag(root, version, args.phase, issues, warnings)

    if args.phase == "published":
        check_github(root, version, repo, issues, warnings)

    print(f"CCB release check: {version} ({args.phase})")
    print(f"repo root: {root}")
    print(f"github repo: {repo}")

    if warnings:
        print("\nWarnings:")
        for item in warnings:
            print(f"- {item}")

    if issues:
        print("\nIssues:")
        for item in issues:
            print(f"- {item}")
        return 1

    print("\nOK: no blocking release-surface drift found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
