from __future__ import annotations

from pathlib import Path

from project.discovery import CCB_DIRNAME, global_ccb_dir

from .common import CONFIG_FILENAME


def project_config_path(project_root: Path) -> Path:
    return Path(project_root).expanduser().resolve() / CCB_DIRNAME / CONFIG_FILENAME


def global_config_path() -> Path:
    return global_ccb_dir() / CONFIG_FILENAME


__all__ = ['global_config_path', 'project_config_path']
