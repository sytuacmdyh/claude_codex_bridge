from __future__ import annotations

from agents.config_loader import load_project_config
from ccbd.socket_client import CcbdClient
from provider_core.catalog import build_default_provider_catalog
from provider_execution.registry import build_default_execution_registry

from .daemon import ping_local_state
from .daemon_runtime.policy import CONTROL_PLANE_RPC_TIMEOUT_S
from .doctor_runtime import agent_summaries, ccbd_summary, doctor_stores, installation_summary, requirements_summary


def doctor_summary(context) -> dict:
    config = load_project_config(context.project.project_root).config
    stores = doctor_stores(context)
    catalog = build_default_provider_catalog()
    execution_registry = build_default_execution_registry()
    local = ping_local_state(context)
    errors: list[str] = []
    remote_ccbd, remote_error = _load_remote_ccbd_summary(context, local=local)
    if remote_error is not None:
        errors.append(f'remote_ccbd_probe:{remote_error}')
    agents = agent_summaries(
        context,
        config=config,
        stores=stores,
        catalog=catalog,
        execution_registry=execution_registry,
        errors=errors,
    )
    return {
        'project': str(context.project.project_root),
        'project_id': context.project.project_id,
        'installation': installation_summary(),
        'requirements': requirements_summary(),
        'ccbd': ccbd_summary(local=local, stores=stores, errors=errors, remote=remote_ccbd),
        'agents': agents,
    }


def _load_remote_ccbd_summary(context, *, local) -> tuple[dict | None, str | None]:
    if local.mount_state == 'unmounted':
        return None, None
    if not local.socket_connectable:
        return None, None
    try:
        payload = CcbdClient(context.paths.ccbd_socket_path, timeout_s=CONTROL_PLANE_RPC_TIMEOUT_S).ping('ccbd')
    except Exception as exc:
        return None, str(exc)
    return (payload if isinstance(payload, dict) else None), None
