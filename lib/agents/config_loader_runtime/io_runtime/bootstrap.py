from __future__ import annotations

from pathlib import Path

from ..paths import global_config_path, project_config_path


def ensure_default_project_config(project_root: Path) -> Path:
    config_path = project_config_path(project_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return config_path


def ensure_bootstrap_project_config(project_root: Path) -> Path:
    config_path = project_config_path(project_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    global_path = global_config_path()
    if global_path.exists() and not config_path.exists():
        config_path.write_text(global_path.read_text(encoding='utf-8'), encoding='utf-8')
    return config_path


__all__ = ['ensure_bootstrap_project_config', 'ensure_default_project_config']
