from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import shlex
from pathlib import Path

from agents.models import AgentSpec, normalize_agent_name
from cli.context import CliContext
from cli.models import ParsedStartCommand
from provider_core.caller_env import (
    caller_context_env,
    export_env_clause,
    join_env_prefix,
    provider_user_session_env,
)
from provider_core.contracts import ProviderRuntimeLauncher
from provider_core.runtime_shared import provider_start_parts
from provider_profiles import load_resolved_provider_profile
from project_memory import materialize_runtime_memory_bundle
from project_memory.hashing import sha256_text
from storage.atomic import atomic_write_text
from workspace.models import WorkspacePlan


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return ProviderRuntimeLauncher(
        provider='opencode',
        launch_mode='simple_tmux',
        prepare_launch_context=prepare_launch_context,
        build_start_cmd=build_start_cmd,
        build_session_payload=build_session_payload,
    )


def prepare_launch_context(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    del runtime_dir
    payload = dict(prepared_state)
    payload['agent_name'] = spec.name
    payload['project_root'] = str(context.project.project_root)
    payload['workspace_path'] = str(prepared_state.get('run_cwd') or plan.workspace_path)
    payload['agent_events_path'] = str(context.paths.agent_events_path(spec.name))
    payload['opencode_config_path'] = str(context.paths.agent_provider_state_dir(spec.name, 'opencode') / 'opencode.json')
    return payload


def build_start_cmd(
    command: ParsedStartCommand,
    spec: AgentSpec,
    runtime_dir,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
) -> str:
    runtime_dir = Path(runtime_dir)
    launch_context = prepared_state or {}
    project_root = _path_or_none(launch_context.get('project_root'))
    if project_root is None:
        raise RuntimeError('OpenCode launch requires prepare_launch_context before build_start_cmd')
    profile = load_resolved_provider_profile(runtime_dir)
    memory_env = _opencode_memory_env(_path_or_none(launch_context.get('opencode_config_path')), profile)
    cmd_parts = provider_start_parts('opencode')
    if command.restore:
        cmd_parts.append('--continue')
    cmd_parts.extend(spec.startup_args)
    cmd = ' '.join(shlex.quote(str(part)) for part in cmd_parts)
    env_prefix = join_env_prefix(
        export_env_clause(provider_user_session_env()),
        export_env_clause(memory_env),
        export_env_clause(
            caller_context_env(actor=spec.name, runtime_dir=runtime_dir, launch_session_id=launch_session_id)
        ),
    )
    if env_prefix:
        return f'{env_prefix}; {cmd}'
    return cmd


def build_session_payload(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir,
    run_cwd,
    pane_id: str,
    pane_title_marker: str,
    start_cmd: str,
    launch_session_id: str,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    del prepared_state
    return {
        'ccb_session_id': launch_session_id,
        'agent_name': spec.name,
        'ccb_project_id': context.project.project_id,
        'runtime_dir': str(runtime_dir),
        'completion_artifact_dir': str(runtime_dir / 'completion'),
        'terminal': 'tmux',
        'tmux_session': pane_id,
        'pane_id': pane_id,
        'pane_title_marker': pane_title_marker,
        'workspace_path': str(plan.workspace_path),
        'work_dir': str(run_cwd),
        'start_dir': str(context.project.project_root),
        'start_cmd': start_cmd,
    }


def materialize_opencode_memory_config(
    *,
    project_root: Path,
    agent_name: str,
    workspace_path: Path | None,
    config_path: Path | None,
    profile,
    event_path: Path | None,
    marker_path: Path,
) -> 'OpenCodeMemoryConfigResult':
    if config_path is None:
        result = _memory_projection_result(
            status='failed',
            reason='missing_config_path',
            path=Path(''),
        )
        _record_memory_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
        return OpenCodeMemoryConfigResult(env={})
    if not _inherits_memory(profile):
        _remove_file(config_path)
        result = _memory_projection_result(
            status='skipped',
            reason='inherit_memory_disabled',
            path=Path(''),
            config_path=config_path,
        )
        _record_memory_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
        return OpenCodeMemoryConfigResult(env={})

    materialization = materialize_runtime_memory_bundle(
        project_root,
        agent_name=agent_name,
        provider='opencode',
        workspace_path=workspace_path,
    )
    if not materialization.sha256 or not materialization.path:
        result = _memory_projection_result(
            status='failed',
            reason='bundle_write_failed',
            path=Path(materialization.path or ''),
            config_path=config_path,
            source_count=len(materialization.sources),
            warnings=materialization.warnings,
        )
        _record_memory_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
        return OpenCodeMemoryConfigResult(env={})

    bridge = _bridge_opencode_memory_bundle(
        project_root=project_root,
        agent_name=agent_name,
        source_bundle_path=materialization.path,
    )
    if not bridge.path:
        warnings = (*materialization.warnings, *bridge.warnings)
        result = _memory_projection_result(
            status='failed',
            reason='bridge_write_failed',
            path=Path(''),
            config_path=config_path,
            sha256=materialization.sha256,
            source_count=len(materialization.sources),
            warnings=warnings,
        )
        _record_memory_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
        return OpenCodeMemoryConfigResult(env={})

    rendered_config = _render_opencode_config(
        project_root=Path(project_root).expanduser(),
        memory_instruction=bridge.instruction,
    )
    config_unchanged = _text_file_sha256(config_path) == rendered_config.sha256
    if not config_unchanged:
        try:
            atomic_write_text(config_path, rendered_config.text)
        except OSError as exc:
            warnings = (*materialization.warnings, *bridge.warnings, *rendered_config.warnings)
            result = _memory_projection_result(
                status='failed',
                reason=type(exc).__name__,
                path=bridge.path,
                config_path=config_path,
                sha256=materialization.sha256,
                config_sha256=rendered_config.sha256,
                source_count=len(materialization.sources),
                warnings=warnings,
                error_detail=str(exc),
                bundle_path=materialization.path,
                project_config_path=rendered_config.project_config_path,
                project_config_sha256=rendered_config.project_config_sha256,
                config_merge_status=rendered_config.merge_status,
                config_merge_reason=rendered_config.merge_reason,
            )
            _record_memory_projection_event(
                result,
                event_path=event_path,
                marker_path=marker_path,
                agent_name=agent_name,
            )
            _record_opencode_config_merge_failed_event(
                result,
                event_path=event_path,
                marker_path=marker_path,
                agent_name=agent_name,
            )
            return OpenCodeMemoryConfigResult(env={})

    status = 'skipped' if materialization.unchanged and bridge.unchanged and config_unchanged else 'ok'
    reason = 'unchanged' if status == 'skipped' else 'written'
    warnings = (*materialization.warnings, *bridge.warnings, *rendered_config.warnings)
    result = _memory_projection_result(
        status=status,
        reason=reason,
        path=bridge.path,
        config_path=config_path,
        sha256=materialization.sha256,
        config_sha256=rendered_config.sha256,
        source_count=len(materialization.sources),
        warnings=warnings,
        bundle_path=materialization.path,
        project_config_path=rendered_config.project_config_path,
        project_config_sha256=rendered_config.project_config_sha256,
        config_merge_status=rendered_config.merge_status,
        config_merge_reason=rendered_config.merge_reason,
    )
    _record_memory_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
    _record_opencode_config_merge_failed_event(
        result,
        event_path=event_path,
        marker_path=marker_path,
        agent_name=agent_name,
    )
    return OpenCodeMemoryConfigResult(env={'OPENCODE_CONFIG': str(config_path)})


@dataclass(frozen=True)
class OpenCodeMemoryConfigResult:
    env: dict[str, str]


@dataclass(frozen=True)
class _OpenCodeMemoryBridge:
    path: Path
    instruction: str
    unchanged: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RenderedOpenCodeConfig:
    text: str
    sha256: str
    warnings: tuple[str, ...]
    merge_status: str
    merge_reason: str
    project_config_path: Path
    project_config_sha256: str


def _opencode_memory_env(config_path: Path | None, profile) -> dict[str, str]:
    if config_path is None or not _inherits_memory(profile):
        return {}
    if not Path(config_path).is_file():
        return {}
    return {'OPENCODE_CONFIG': str(config_path)}


def _bridge_opencode_memory_bundle(
    *,
    project_root: Path,
    agent_name: str,
    source_bundle_path: Path,
) -> _OpenCodeMemoryBridge:
    root = Path(project_root).expanduser()
    normalized_agent = normalize_agent_name(agent_name)
    bridge_path = root / '.ccb' / 'runtime' / 'memory' / f'{normalized_agent}.md'
    instruction = f'.ccb/runtime/memory/{normalized_agent}.md'
    source_path = Path(source_bundle_path).expanduser()
    try:
        if _same_path(source_path, bridge_path):
            return _OpenCodeMemoryBridge(path=bridge_path, instruction=instruction, unchanged=True)
        text = source_path.read_text(encoding='utf-8')
        digest = sha256_text(text)
        if _text_file_sha256(bridge_path) == digest:
            return _OpenCodeMemoryBridge(path=bridge_path, instruction=instruction, unchanged=True)
        atomic_write_text(bridge_path, text)
        return _OpenCodeMemoryBridge(path=bridge_path, instruction=instruction, unchanged=False)
    except Exception as exc:
        return _OpenCodeMemoryBridge(
            path=Path(''),
            instruction=instruction,
            unchanged=False,
            warnings=(f'failed_to_write_opencode_memory_bridge: {exc}',),
        )


def _render_opencode_config(*, project_root: Path, memory_instruction: str) -> _RenderedOpenCodeConfig:
    project_config_path = Path(project_root).expanduser() / 'opencode.json'
    project_config_sha = _text_file_sha256(project_config_path)
    payload: dict[str, object] = {}
    warnings: list[str] = []
    merge_status = 'missing'
    merge_reason = 'project_config_missing'
    if project_config_path.is_file():
        try:
            raw = json.loads(project_config_path.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                payload = _clone_json_object(raw)
                merge_status = 'ok'
                merge_reason = 'merged_project_opencode_json'
            else:
                merge_status = 'failed'
                merge_reason = 'project_config_not_object'
                warnings.append('opencode_config_merge_failed: project_config_not_object')
        except Exception as exc:
            merge_status = 'failed'
            merge_reason = type(exc).__name__
            warnings.append(f'opencode_config_merge_failed: {type(exc).__name__}: {exc}')
    payload.setdefault('$schema', 'https://opencode.ai/config.json')
    payload['instructions'] = _merge_instruction_entries(payload.get('instructions'), memory_instruction)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    return _RenderedOpenCodeConfig(
        text=text,
        sha256=sha256_text(text),
        warnings=tuple(warnings),
        merge_status=merge_status,
        merge_reason=merge_reason,
        project_config_path=project_config_path,
        project_config_sha256=project_config_sha,
    )


def _clone_json_object(payload: dict[object, object]) -> dict[str, object]:
    return {str(key): _clone_json_value(value) for key, value in payload.items()}


def _clone_json_value(value: object) -> object:
    if isinstance(value, dict):
        return _clone_json_object(value)
    if isinstance(value, list):
        return [_clone_json_value(item) for item in value]
    return value


def _merge_instruction_entries(current: object, memory_instruction: str) -> list[str]:
    entries: list[str] = []
    if isinstance(current, str):
        entries.append(current)
    elif isinstance(current, list):
        for item in current:
            if isinstance(item, str):
                entries.append(item)
    merged: list[str] = []
    seen: set[str] = set()
    for entry in (*entries, memory_instruction):
        stripped = str(entry or '').strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        merged.append(stripped)
    return merged


def _memory_projection_result(
    *,
    status: str,
    reason: str,
    path: Path,
    sha256: str = '',
    source_count: int = 0,
    warnings: tuple[str, ...] | list[str] = (),
    error_detail: str = '',
    bundle_path: Path | None = None,
    config_path: Path | None = None,
    config_sha256: str = '',
    project_config_path: Path | None = None,
    project_config_sha256: str = '',
    config_merge_status: str = '',
    config_merge_reason: str = '',
) -> dict[str, object]:
    result = {
        'status': status,
        'reason': reason,
        'path': str(path),
        'sha256': sha256,
        'config_path': str(config_path or ''),
        'config_sha256': str(config_sha256 or ''),
        'source_count': source_count,
        'warnings': tuple(str(item) for item in warnings if str(item)),
        'error_detail': str(error_detail or ''),
        'project_config_path': str(project_config_path or ''),
        'project_config_sha256': str(project_config_sha256 or ''),
        'config_merge_status': str(config_merge_status or ''),
        'config_merge_reason': str(config_merge_reason or ''),
    }
    if bundle_path is not None:
        result['bundle_path'] = str(bundle_path)
    return result


def _record_memory_projection_event(
    result: dict[str, object],
    *,
    event_path: Path | None,
    marker_path: Path,
    agent_name: str,
) -> None:
    if event_path is None:
        return
    status = str(result.get('status') or 'unknown')
    reason = str(result.get('reason') or '')
    signature = {
        'status': status,
        'reason': reason,
        'path': str(result.get('path') or ''),
        'config_path': str(result.get('config_path') or ''),
        'bundle_path': str(result.get('bundle_path') or ''),
        'sha256': str(result.get('sha256') or ''),
        'config_sha256': str(result.get('config_sha256') or ''),
        'warnings': list(result.get('warnings') or ()),
        'config_merge_status': str(result.get('config_merge_status') or ''),
        'config_merge_reason': str(result.get('config_merge_reason') or ''),
    }
    marker = Path(marker_path)
    if _same_memory_projection_signature(marker, signature):
        return
    event = {
        'record_type': 'agent_event',
        'event_type': f'opencode_memory_projection_{status}',
        'provider': 'opencode',
        'agent_name': agent_name,
        'status': status,
        'reason': reason,
        'projection_path': signature['path'],
        'config_path': signature['config_path'],
        'bundle_path': signature['bundle_path'],
        'sha256': signature['sha256'],
        'bundle_sha256': signature['sha256'],
        'config_sha256': signature['config_sha256'],
        'source_count': int(result.get('source_count') or 0),
        'warnings': signature['warnings'],
        'error_detail': str(result.get('error_detail') or ''),
        'project_config_path': str(result.get('project_config_path') or ''),
        'project_config_sha256': str(result.get('project_config_sha256') or ''),
        'config_merge_status': signature['config_merge_status'],
        'config_merge_reason': signature['config_merge_reason'],
        'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    try:
        target = Path(event_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(signature, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError:
        return


def _record_opencode_config_merge_failed_event(
    result: dict[str, object],
    *,
    event_path: Path | None,
    marker_path: Path,
    agent_name: str,
) -> None:
    if event_path is None or result.get('config_merge_status') != 'failed':
        return
    signature = {
        'status': 'failed',
        'reason': str(result.get('config_merge_reason') or ''),
        'project_config_path': str(result.get('project_config_path') or ''),
        'project_config_sha256': str(result.get('project_config_sha256') or ''),
    }
    marker = Path(marker_path).with_name('opencode-config-merge.json')
    if _same_memory_projection_signature(marker, signature):
        return
    event = {
        'record_type': 'agent_event',
        'event_type': 'opencode_config_merge_failed',
        'provider': 'opencode',
        'agent_name': agent_name,
        'status': 'failed',
        'reason': signature['reason'],
        'project_config_path': signature['project_config_path'],
        'project_config_sha256': signature['project_config_sha256'],
        'warnings': list(result.get('warnings') or ()),
        'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    try:
        target = Path(event_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(signature, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError:
        return


def _same_memory_projection_signature(path: Path, payload: dict[str, object]) -> bool:
    try:
        existing = json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return False
    if not isinstance(existing, dict):
        return False
    if existing == payload:
        return True
    if payload.get('status') == 'skipped' and payload.get('reason') == 'unchanged':
        return (
            bool(payload.get('sha256'))
            and existing.get('path') == payload.get('path')
            and existing.get('config_path') == payload.get('config_path')
            and existing.get('bundle_path') == payload.get('bundle_path')
            and existing.get('sha256') == payload.get('sha256')
            and existing.get('config_sha256') == payload.get('config_sha256')
            and existing.get('warnings') == payload.get('warnings')
            and existing.get('config_merge_status') == payload.get('config_merge_status')
            and existing.get('config_merge_reason') == payload.get('config_merge_reason')
        )
    if payload.get('status') == 'skipped':
        return (
            existing.get('reason') == payload.get('reason')
            and existing.get('path') == payload.get('path')
            and existing.get('config_path') == payload.get('config_path')
            and existing.get('bundle_path') == payload.get('bundle_path')
            and existing.get('sha256') == payload.get('sha256')
            and existing.get('config_sha256') == payload.get('config_sha256')
            and existing.get('warnings') == payload.get('warnings')
        )
    return False


def _inherits_memory(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_memory', True))


def _path_or_none(value: object) -> Path | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except Exception:
        return None


def _text_file_sha256(path: Path) -> str:
    try:
        return sha256_text(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return ''


def _remove_file(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        return


def _same_path(left: Path, right: Path) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return Path(left).expanduser() == Path(right).expanduser()


__all__ = ['build_runtime_launcher', 'build_start_cmd', 'materialize_opencode_memory_config']
