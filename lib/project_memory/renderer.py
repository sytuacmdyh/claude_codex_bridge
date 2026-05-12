from __future__ import annotations

from pathlib import Path

from .types import ProjectMemorySource


def render_memory_bundle(
    *,
    project_root: Path,
    agent_name: str,
    provider: str,
    sources: tuple[ProjectMemorySource, ...],
    workspace_path: Path | None = None,
) -> str:
    lines = [
        '# CCB Managed Agent Memory',
        '',
        '<!-- ccb-memory-bundle schema_version=1',
        'generated_by: ccb',
        'do_not_edit: true',
        f'agent: {agent_name}',
        f'provider: {provider}',
        f'project_root: {Path(project_root).expanduser().resolve()}',
    ]
    if workspace_path is not None:
        lines.append(f'workspace_path: {Path(workspace_path).expanduser().resolve()}')
    lines.extend(['-->', ''])

    for source in sources:
        if not source.exists or not source.content.strip():
            continue
        lines.extend(_render_source_section(source))

    return '\n'.join(lines).rstrip() + '\n'


def _render_source_section(source: ProjectMemorySource) -> list[str]:
    content = source.content.rstrip()
    lines = [
        f'## {source.title}',
        f'source: {source.path}',
    ]
    if source.warning:
        lines.append(f'warning: {source.warning}')
    lines.extend(['', content, ''])
    return lines


__all__ = ['render_memory_bundle']
