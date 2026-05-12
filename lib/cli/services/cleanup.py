from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil

from cli.services.daemon import inspect_daemon
from provider_execution.state_store import ExecutionStateStore
from storage.locks import file_lock


_PENDING_JOB_STATUSES = {'accepted', 'queued', 'running'}
_SAFE_GEMINI_CACHE_RELS = (
    Path('.npm') / '_cacache',
    Path('.cache') / 'node-gyp',
    Path('.cache') / 'vscode-ripgrep',
)


@dataclass(frozen=True)
class CleanupAction:
    provider: str
    kind: str
    path: str
    bytes_removed: int
    reason: str


@dataclass(frozen=True)
class CleanupSkipped:
    provider: str
    path: str
    reason: str


@dataclass(frozen=True)
class CleanupSummary:
    project_root: str
    project_id: str
    status: str
    deleted_bytes: int
    deleted_count: int
    skipped_count: int
    actions: tuple[CleanupAction, ...] = ()
    skipped: tuple[CleanupSkipped, ...] = ()


def cleanup_project_storage(context, command) -> CleanupSummary:
    del command
    with file_lock(context.paths.ccbd_dir / 'startup.lock'):
        _require_stopped_backend(context)
        _require_no_pending_jobs(context)
        actions: list[CleanupAction] = []
        skipped: list[CleanupSkipped] = []
        _cleanup_claude_version_caches(context.paths, actions=actions, skipped=skipped)
        _cleanup_gemini_rebuildable_caches(context.paths, actions=actions, skipped=skipped)
        return CleanupSummary(
            project_root=str(context.project.project_root),
            project_id=context.project.project_id,
            status='ok',
            deleted_bytes=sum(item.bytes_removed for item in actions),
            deleted_count=len(actions),
            skipped_count=len(skipped),
            actions=tuple(actions),
            skipped=tuple(skipped),
        )


def _require_stopped_backend(context) -> None:
    _manager, _guard, inspection = inspect_daemon(context)
    phase = str(getattr(inspection, 'phase', '') or '').strip()
    desired_state = str(getattr(inspection, 'desired_state', '') or '').strip()
    if getattr(inspection, 'pid_alive', False) or getattr(inspection, 'socket_connectable', False):
        raise RuntimeError('ccb cleanup requires stopped ccbd; run `ccb kill` first')
    if phase not in {'', 'unmounted', 'failed'}:
        raise RuntimeError(f'ccb cleanup requires stopped ccbd; current phase={phase}')
    if desired_state and desired_state != 'stopped':
        raise RuntimeError(f'ccb cleanup requires stopped ccbd; desired_state={desired_state}')


def _require_no_pending_jobs(context) -> None:
    execution_summary = ExecutionStateStore(context.paths).summary()
    active_execution_count = int(execution_summary.get('active_execution_count') or 0)
    pending_items_count = int(execution_summary.get('pending_items_count') or 0)
    terminal_pending_count = int(execution_summary.get('terminal_pending_count') or 0)
    pending_job_count = _pending_job_count(context.paths)
    if active_execution_count or pending_items_count or terminal_pending_count or pending_job_count:
        raise RuntimeError(
            'ccb cleanup refused: pending ask jobs exist; wait for completion or run `ccb kill` after terminalization'
        )


def _pending_job_count(layout) -> int:
    roots = [layout.agents_dir, layout.ccbd_dir / 'targets']
    count = 0
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob('jobs.jsonl')):
            count += _pending_job_count_in_file(path)
    return count


def _pending_job_count_in_file(path: Path) -> int:
    latest_by_job: dict[str, str] = {}
    unreadable_or_malformed_count = 0
    try:
        handle = path.open('r', encoding='utf-8')
    except OSError:
        return 1
    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                unreadable_or_malformed_count += 1
                continue
            if not isinstance(record, dict):
                unreadable_or_malformed_count += 1
                continue
            job_id = str(record.get('job_id') or '').strip()
            if not job_id:
                continue
            latest_by_job[job_id] = str(record.get('status') or '').strip().lower()
    return (
        sum(1 for status in latest_by_job.values() if status in _PENDING_JOB_STATUSES)
        + unreadable_or_malformed_count
    )


def _cleanup_claude_version_caches(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if not agents_dir.exists():
        return
    for versions_dir in sorted(agents_dir.glob('*/provider-state/claude/home/.local/share/claude/versions')):
        _cleanup_one_claude_versions_dir(versions_dir, actions=actions, skipped=skipped)


def _cleanup_one_claude_versions_dir(
    versions_dir: Path,
    *,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    if versions_dir.is_symlink():
        skipped.append(
            CleanupSkipped(
                provider='claude',
                path=str(versions_dir),
                reason='versions_dir_is_symlink',
            )
        )
        return
    if not versions_dir.is_dir():
        return
    home = versions_dir.parents[3]
    current_dir = _current_claude_version_dir(home, versions_dir)
    if current_dir is None:
        skipped.append(
            CleanupSkipped(
                provider='claude',
                path=str(versions_dir),
                reason='current_version_symlink_unresolved',
            )
        )
        return
    version_dirs = [
        path
        for path in versions_dir.iterdir()
        if path.is_dir() and not path.is_symlink() and _is_within(path, versions_dir)
    ]
    rollback = _newest_version_dir(path for path in version_dirs if path != current_dir)
    keep = {current_dir}
    if rollback is not None:
        keep.add(rollback)
    for path in version_dirs:
        if path in keep:
            continue
        _remove_tree(
            path,
            root=versions_dir,
            provider='claude',
            kind='version_cache',
            reason='old_claude_version_cache',
            actions=actions,
            skipped=skipped,
        )


def _current_claude_version_dir(home: Path, versions_dir: Path) -> Path | None:
    link = home / '.local' / 'bin' / 'claude'
    try:
        target = link.resolve(strict=True)
    except Exception:
        return None
    if not _is_within(target, versions_dir):
        return None
    try:
        relative = target.relative_to(versions_dir.resolve(strict=False))
    except Exception:
        return None
    if not relative.parts:
        return None
    current = versions_dir / relative.parts[0]
    if not current.is_dir() or current.is_symlink():
        return None
    return current


def _newest_version_dir(paths) -> Path | None:
    candidates = list(paths)
    if not candidates:
        return None
    return max(candidates, key=lambda path: (_version_key(path.name), _safe_mtime(path), path.name))


def _version_key(value: str) -> tuple[tuple[int, object], ...]:
    parts: list[tuple[int, object]] = []
    for item in value.replace('-', '.').split('.'):
        if item.isdigit():
            parts.append((1, int(item)))
        else:
            parts.append((0, item))
    return tuple(parts)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cleanup_gemini_rebuildable_caches(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if not agents_dir.exists():
        return
    for home in sorted(agents_dir.glob('*/provider-state/gemini/home')):
        if not home.is_dir() or home.is_symlink():
            continue
        for relative in _SAFE_GEMINI_CACHE_RELS:
            path = home / relative
            if not path.exists():
                continue
            _remove_tree(
                path,
                root=home,
                provider='gemini',
                kind='tool_cache',
                reason='rebuildable_gemini_cache',
                actions=actions,
                skipped=skipped,
            )


def _remove_tree(
    path: Path,
    *,
    root: Path,
    provider: str,
    kind: str,
    reason: str,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    if path.is_symlink():
        skipped.append(CleanupSkipped(provider=provider, path=str(path), reason='symlink_not_removed'))
        return
    if not _is_within(path, root):
        skipped.append(CleanupSkipped(provider=provider, path=str(path), reason='path_out_of_bounds'))
        return
    size = _tree_size(path)
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return
    actions.append(
        CleanupAction(
            provider=provider,
            kind=kind,
            path=str(path),
            bytes_removed=size,
            reason=reason,
        )
    )


def _tree_size(path: Path) -> int:
    total = 0
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        return _lstat_size(path)
    for child in path.rglob('*'):
        total += _lstat_size(child)
    return total


def _lstat_size(path: Path) -> int:
    try:
        return int(path.lstat().st_size)
    except OSError:
        return 0


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


__all__ = ['CleanupAction', 'CleanupSkipped', 'CleanupSummary', 'cleanup_project_storage']
