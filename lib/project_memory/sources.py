from __future__ import annotations

from pathlib import Path

from agents.models import normalize_agent_name
from storage.paths import PathLayout

from .seed import project_memory_path
from .types import ProjectMemorySource

_PROVIDER_NATIVE_FILES = {
    'claude': 'CLAUDE.md',
    'codex': 'AGENTS.md',
    'opencode': 'AGENTS.md',
    'gemini': 'GEMINI.md',
}


def agent_private_memory_path(project_root_or_layout, agent_name: str) -> Path:
    layout = _layout(project_root_or_layout)
    return layout.agent_private_memory_path(agent_name)


def provider_native_memory_path(project_root_or_layout, provider: str) -> Path | None:
    layout = _layout(project_root_or_layout)
    filename = _PROVIDER_NATIVE_FILES.get(str(provider or '').strip().lower())
    if not filename:
        return None
    return layout.project_root / filename


def load_memory_sources(
    project_root_or_layout,
    *,
    agent_name: str,
    provider: str,
    extra_sources: tuple[ProjectMemorySource, ...] = (),
    include_missing: bool = True,
) -> tuple[ProjectMemorySource, ...]:
    layout = _layout(project_root_or_layout)
    sources: list[ProjectMemorySource] = []
    sources.extend(extra_sources)
    sources.append(
        _read_source(
            kind='ccb_shared',
            title='CCB Shared Project Memory',
            path=project_memory_path(layout),
            include_missing=include_missing,
        )
    )

    provider_path = provider_native_memory_path(layout, provider)
    if provider_path is not None:
        provider_source = _read_source(
            kind='provider_native_project',
            title='Provider-Native Project Memory',
            path=provider_path,
            include_missing=include_missing,
        )
        if provider_source is not None:
            sources.append(provider_source)

    sources.append(
        _read_source(
            kind='agent_private',
            title='Agent Private Memory',
            path=agent_private_memory_path(layout, agent_name),
            include_missing=include_missing,
        )
    )
    return tuple(source for source in sources if source is not None)


def _layout(project_root_or_layout) -> PathLayout:
    if isinstance(project_root_or_layout, PathLayout):
        return project_root_or_layout
    return PathLayout(Path(project_root_or_layout))


def read_memory_source(
    *,
    kind: str,
    title: str,
    path: Path,
    include_missing: bool = True,
) -> ProjectMemorySource | None:
    return _read_source(kind=kind, title=title, path=path, include_missing=include_missing)


def _read_source(*, kind: str, title: str, path: Path, include_missing: bool) -> ProjectMemorySource | None:
    source_path = Path(path)
    if not source_path.is_file():
        if not include_missing:
            return None
        return ProjectMemorySource(kind=kind, title=title, path=source_path, content='', exists=False)
    try:
        content = source_path.read_text(encoding='utf-8')
    except OSError as exc:
        return ProjectMemorySource(
            kind=kind,
            title=title,
            path=source_path,
            content='',
            exists=True,
            warning=f'failed_to_read_memory_source: {exc}',
        )
    return ProjectMemorySource(kind=kind, title=title, path=source_path, content=content, exists=True)


__all__ = [
    'agent_private_memory_path',
    'load_memory_sources',
    'provider_native_memory_path',
    'read_memory_source',
]
