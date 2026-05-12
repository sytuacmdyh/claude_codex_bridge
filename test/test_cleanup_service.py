from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os

import pytest

from cli.services import cleanup as cleanup_service
from cli.services.cleanup import cleanup_project_storage
from project.ids import compute_project_id
from storage.paths import PathLayout


def _write(path: Path, text: str = 'x') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _context(project_root: Path):
    layout = PathLayout(project_root)
    return SimpleNamespace(
        paths=layout,
        project=SimpleNamespace(project_root=project_root, project_id=compute_project_id(project_root)),
    )


def _stopped_inspection():
    return SimpleNamespace(
        phase='unmounted',
        desired_state='stopped',
        pid_alive=False,
        socket_connectable=False,
    )


def test_cleanup_prunes_old_claude_versions_and_gemini_caches(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)
    claude_home = layout.agent_provider_state_dir('agent1', 'claude') / 'home'
    versions = claude_home / '.local' / 'share' / 'claude' / 'versions'
    _write(versions / '2.1.132' / 'claude', 'old')
    _write(versions / '2.1.133' / 'claude', 'rollback')
    _write(versions / '2.1.137' / 'claude', 'current')
    (claude_home / '.local' / 'bin').mkdir(parents=True, exist_ok=True)
    os.symlink('../share/claude/versions/2.1.137/claude', claude_home / '.local' / 'bin' / 'claude')
    gemini_home = layout.agent_provider_state_dir('agent2', 'gemini') / 'home'
    _write(gemini_home / '.npm' / '_cacache' / 'blob', 'cache')
    _write(gemini_home / '.cache' / 'node-gyp' / 'state', 'cache')
    _write(gemini_home / '.gemini' / 'tmp' / 'session.json', '{}')
    monkeypatch.setattr(cleanup_service, 'inspect_daemon', lambda context: (None, None, _stopped_inspection()))

    summary = cleanup_project_storage(_context(project_root), SimpleNamespace())

    assert summary.status == 'ok'
    assert summary.deleted_count == 3
    assert not (versions / '2.1.132').exists()
    assert (versions / '2.1.133').exists()
    assert (versions / '2.1.137').exists()
    assert not (gemini_home / '.npm' / '_cacache').exists()
    assert not (gemini_home / '.cache' / 'node-gyp').exists()
    assert (gemini_home / '.gemini' / 'tmp' / 'session.json').exists()


def test_cleanup_refuses_when_pending_jobs_exist(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)
    _write(
        layout.agent_jobs_path('agent1'),
        '{"job_id":"job_1","status":"accepted"}\n',
    )
    claude_home = layout.agent_provider_state_dir('agent1', 'claude') / 'home'
    versions = claude_home / '.local' / 'share' / 'claude' / 'versions'
    _write(versions / '2.1.132' / 'claude', 'old')
    monkeypatch.setattr(cleanup_service, 'inspect_daemon', lambda context: (None, None, _stopped_inspection()))

    with pytest.raises(RuntimeError, match='pending ask jobs exist'):
        cleanup_project_storage(_context(project_root), SimpleNamespace())

    assert (versions / '2.1.132' / 'claude').exists()


def test_cleanup_refuses_when_jobs_jsonl_is_malformed(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)
    _write(
        layout.agent_jobs_path('agent1'),
        '{"job_id":"job_1","status":"succeeded"}\n{"job_id":',
    )
    claude_home = layout.agent_provider_state_dir('agent1', 'claude') / 'home'
    versions = claude_home / '.local' / 'share' / 'claude' / 'versions'
    _write(versions / '2.1.132' / 'claude', 'old')
    monkeypatch.setattr(cleanup_service, 'inspect_daemon', lambda context: (None, None, _stopped_inspection()))

    with pytest.raises(RuntimeError, match='pending ask jobs exist'):
        cleanup_project_storage(_context(project_root), SimpleNamespace())

    assert (versions / '2.1.132' / 'claude').exists()


def test_cleanup_refuses_when_ccbd_is_active(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    active = SimpleNamespace(
        phase='mounted',
        desired_state='running',
        pid_alive=True,
        socket_connectable=True,
    )
    monkeypatch.setattr(cleanup_service, 'inspect_daemon', lambda context: (None, None, active))

    with pytest.raises(RuntimeError, match='requires stopped ccbd'):
        cleanup_project_storage(_context(project_root), SimpleNamespace())


def test_cleanup_reports_symlinked_claude_versions_dir(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)
    claude_home = layout.agent_provider_state_dir('agent1', 'claude') / 'home'
    real_versions = tmp_path / 'shared-versions'
    _write(real_versions / '2.1.137' / 'claude', 'current')
    versions = claude_home / '.local' / 'share' / 'claude' / 'versions'
    versions.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real_versions, versions)
    monkeypatch.setattr(cleanup_service, 'inspect_daemon', lambda context: (None, None, _stopped_inspection()))

    summary = cleanup_project_storage(_context(project_root), SimpleNamespace())

    assert summary.deleted_count == 0
    assert summary.skipped_count == 1
    assert summary.skipped[0].reason == 'versions_dir_is_symlink'


def test_cleanup_skips_gemini_cache_behind_out_of_bounds_symlink(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)
    gemini_home = layout.agent_provider_state_dir('agent1', 'gemini') / 'home'
    outside_npm = tmp_path / 'outside-npm'
    _write(outside_npm / '_cacache' / 'blob', 'cache')
    gemini_home.mkdir(parents=True, exist_ok=True)
    os.symlink(outside_npm, gemini_home / '.npm')
    monkeypatch.setattr(cleanup_service, 'inspect_daemon', lambda context: (None, None, _stopped_inspection()))

    summary = cleanup_project_storage(_context(project_root), SimpleNamespace())

    assert summary.deleted_count == 0
    assert summary.skipped_count == 1
    assert summary.skipped[0].reason == 'path_out_of_bounds'
    assert (outside_npm / '_cacache' / 'blob').exists()
