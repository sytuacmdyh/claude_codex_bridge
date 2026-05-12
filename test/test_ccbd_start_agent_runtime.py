from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agents.models import AgentRuntime, AgentState, RuntimeBindingSource
from ccbd.start_runtime.agent_runtime import start_agent_runtime
from cli.services.provider_binding import AgentBinding
from cli.services.runtime_launch import RuntimeLaunchResult
from project.ids import compute_project_id
from project.resolver import ProjectContext
from storage.paths import PathLayout


class _RuntimeService:
    def __init__(self) -> None:
        self.attach_calls: list[dict[str, object]] = []
        self.mount_attach_calls: list[dict[str, object]] = []
        self.restore_calls: list[str] = []
        self._registry = _Registry()

    def attach(self, **kwargs):
        self.attach_calls.append(kwargs)
        binding_source = SimpleNamespace(value=kwargs['binding_source'])
        return SimpleNamespace(
            agent_name=kwargs['agent_name'],
            runtime_ref=kwargs['runtime_ref'],
            session_ref=kwargs['session_ref'],
            lifecycle_state=kwargs['lifecycle_state'],
            desired_state=None,
            reconcile_state=None,
            binding_source=binding_source,
            provider='codex',
            terminal_backend=kwargs['terminal_backend'],
            tmux_socket_name=kwargs['tmux_socket_name'],
            tmux_socket_path=kwargs['tmux_socket_path'],
            pane_id=kwargs['pane_id'],
            active_pane_id=kwargs['active_pane_id'],
            pane_state=kwargs['pane_state'],
            runtime_pid=kwargs['runtime_pid'],
            runtime_root=kwargs['runtime_root'],
            runtime_generation=kwargs.get('binding_generation', 1),
            daemon_generation=7,
            started_at='2026-04-21T00:00:00Z',
            last_seen_at='2026-04-21T00:00:01Z',
        )

    def attach_mount_attempt_authority(self, **kwargs):
        self.mount_attach_calls.append(kwargs)
        return self.attach(**{key: value for key, value in kwargs.items() if key != 'attempt_id'}), True

    def restore(self, agent_name: str):
        self.restore_calls.append(agent_name)


class _Registry:
    def __init__(self, runtime: AgentRuntime | None = None) -> None:
        self._runtime = runtime

    def get(self, agent_name: str):
        assert agent_name == 'agent1'
        return self._runtime


def _binding(**overrides) -> AgentBinding:
    values = {
        'runtime_ref': 'tmux:%5',
        'session_ref': 'session-5',
        'provider': 'codex',
        'runtime_root': '/tmp/runtime',
        'runtime_pid': 55,
        'session_file': '/tmp/session.json',
        'session_id': 'session-5',
        'tmux_socket_name': 'sock-a',
        'tmux_socket_path': '/tmp/ccb.sock',
        'terminal': 'tmux',
        'pane_id': '%5',
        'active_pane_id': '%5',
        'pane_title_marker': 'agent1',
        'pane_state': 'alive',
    }
    values.update(overrides)
    return AgentBinding(**values)


def _runtime(**overrides) -> AgentRuntime:
    values = {
        'agent_name': 'agent1',
        'state': AgentState.STARTING,
        'pid': None,
        'started_at': '2026-04-21T00:00:00Z',
        'last_seen_at': '2026-04-21T00:00:01Z',
        'runtime_ref': 'tmux:%5',
        'session_ref': 'session-5',
        'workspace_path': '/tmp/ws',
        'project_id': 'proj-1',
        'backend_type': 'pane-backed',
        'queue_depth': 0,
        'socket_path': None,
        'health': 'starting',
        'provider': 'codex',
        'runtime_root': '/tmp/runtime',
        'runtime_pid': 55,
        'terminal_backend': 'tmux',
        'pane_id': '%5',
        'active_pane_id': '%5',
        'pane_title_marker': 'agent1',
        'pane_state': 'alive',
        'binding_generation': 2,
        'daemon_generation': 7,
        'runtime_generation': 2,
        'managed_by': 'ccbd',
        'binding_source': RuntimeBindingSource.PROVIDER_SESSION,
        'reconcile_state': 'starting',
        'mount_attempt_id': 'mount-123',
    }
    values.update(overrides)
    return AgentRuntime(**values)


def test_start_agent_runtime_degrades_unresolved_stale_binding() -> None:
    runtime_service = _RuntimeService()

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=None,
        stale_binding=True,
        assigned_pane_id='%9',
        style_index=0,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=2,
        ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(launched=False, binding=None),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: None,
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert execution.agent_result.action == 'degraded'
    assert execution.agent_result.health == 'degraded'
    assert execution.agent_result.failure_reason == 'stale_binding_unresolved'
    assert execution.actions_taken == ('degraded_stale_binding:agent1',)
    assert runtime_service.restore_calls == []


def test_start_agent_runtime_reuses_binding_and_restores_when_requested() -> None:
    runtime_service = _RuntimeService()
    binding = _binding()

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=binding,
        raw_binding=binding,
        stale_binding=False,
        assigned_pane_id=None,
        style_index=1,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=3,
        ensure_agent_runtime_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not relaunch')),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%5',
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert execution.agent_result.action == 'attached'
    assert execution.actions_taken == (
        'relabel_runtime_pane:agent1:%5',
        'reuse_binding:agent1',
        'restore_runtime:agent1',
    )
    assert execution.runtime_pane_id == '%5'
    assert execution.project_socket_active_pane_id == '%5'
    assert runtime_service.restore_calls == ['agent1']


def test_start_agent_runtime_relaunches_and_tracks_project_socket_pane() -> None:
    runtime_service = _RuntimeService()
    launched_binding = _binding(runtime_ref='tmux:%7', session_ref='session-7', pane_id='%7', active_pane_id='%7')

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=_binding(runtime_ref='tmux:%3'),
        stale_binding=True,
        assigned_pane_id='%7',
        style_index=2,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=4,
        ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(launched=True, binding=launched_binding),
        launch_binding_hint_fn=lambda **kwargs: 'hint',
        relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert execution.agent_result.action == 'relaunched'
    assert execution.actions_taken == (
        'relabel_runtime_pane:agent1:%7',
        'relaunch_runtime:agent1',
    )
    assert execution.runtime_pane_id == '%7'
    assert execution.project_socket_active_pane_id == '%7'
    assert runtime_service.attach_calls[-1]['runtime_ref'] == 'tmux:%7'


def test_start_agent_runtime_uses_runtime_service_for_helper_ownership(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    config_dir = project_root / '.ccb'
    config_dir.mkdir(parents=True)
    context = SimpleNamespace(
        paths=PathLayout(project_root),
        project=ProjectContext(
            cwd=project_root,
            project_root=project_root,
            config_dir=config_dir,
            project_id=compute_project_id(project_root),
            source='test',
        ),
    )
    runtime_service = _RuntimeService()
    launched_binding = _binding(
        runtime_ref='tmux:%7',
        session_ref='session-7',
        pane_id='%7',
        active_pane_id='%7',
        runtime_root=str(tmp_path / 'runtime'),
    )
    runtime_dir = Path(launched_binding.runtime_root)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / 'bridge.pid').write_text('5511\n', encoding='utf-8')

    start_agent_runtime(
        context=context,
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=None,
        stale_binding=False,
        assigned_pane_id='%7',
        style_index=0,
        project_id=context.project.project_id,
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=1,
        ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(launched=True, binding=launched_binding),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert runtime_service.attach_calls


def test_start_agent_runtime_uses_mount_attempt_scoped_attach_during_supervision_starting() -> None:
    runtime_service = _RuntimeService()
    runtime_service._registry = _Registry(_runtime())
    binding = _binding(runtime_ref='tmux:%7', session_ref='session-7', pane_id='%7', active_pane_id='%7')

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=binding,
        raw_binding=binding,
        stale_binding=False,
        assigned_pane_id=None,
        style_index=1,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=3,
        ensure_agent_runtime_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not relaunch')),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert execution.agent_result.action == 'attached'
    assert len(runtime_service.mount_attach_calls) == 1
    assert runtime_service.mount_attach_calls[0]['attempt_id'] == 'mount-123'
    assert runtime_service.attach_calls == [
        {
            'agent_name': 'agent1',
            'workspace_path': '/tmp/ws',
            'backend_type': 'pane-backed',
            'runtime_ref': 'tmux:%7',
            'session_ref': 'session-7',
            'health': 'healthy',
            'provider': 'codex',
            'runtime_root': '/tmp/runtime',
            'runtime_pid': 55,
            'terminal_backend': 'tmux',
            'pane_id': '%7',
            'active_pane_id': '%7',
            'pane_title_marker': 'agent1',
            'pane_state': 'alive',
            'tmux_socket_name': 'sock-a',
            'tmux_socket_path': '/tmp/ccb.sock',
            'session_file': '/tmp/session.json',
            'session_id': 'session-5',
            'slot_key': 'agent1',
            'window_id': None,
            'workspace_epoch': None,
            'lifecycle_state': execution.agent_result.lifecycle_state,
            'managed_by': 'ccbd',
            'binding_source': 'provider-session',
        }
    ]
