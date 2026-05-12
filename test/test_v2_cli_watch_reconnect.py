from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from io import StringIO

import pytest

from ccbd.api_models import DeliveryScope, JobEvent, JobRecord, JobStatus, MessageEnvelope
from ccbd.socket_client import CcbdClientError
from cli.context import CliContext, CliContextBuilder
from cli.models import ParsedAckCommand, ParsedCancelCommand, ParsedInboxCommand, ParsedPendCommand, ParsedQueueCommand, ParsedResubmitCommand, ParsedRetryCommand, ParsedTraceCommand, ParsedWatchCommand
from cli.services import ack as ack_service
from cli.services import cancel as cancel_service
from cli.services.daemon import CcbdServiceError
from cli.services import inbox as inbox_service
from cli.services import pend as pend_service
from cli.services import queue as queue_service
from cli.services import resubmit as resubmit_service
from cli.services import retry as retry_service
from cli.services import trace as trace_service
from cli.services import watch as watch_service
from cli.phase2_runtime.handlers_mailbox import handle_watch
from cli.render import render_observer_notice
from cli.services.ask_runtime.watch import watch_ask_job as watch_ask_job_impl
from cli.render import render_watch_batch, write_lines
from completion.models import CompletionConfidence, CompletionDecision, CompletionFamily, CompletionState, CompletionStatus
from jobs.store import JobEventStore, JobStore
from ccbd.services.snapshot_writer import SnapshotWriter
from storage.paths import PathLayout


class _FlakyWatchClient:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def watch(self, target: str, *, cursor: int = 0) -> dict:
        del target
        self.calls.append(cursor)
        raise CcbdClientError('socket closed during generation switch')


class _StableWatchClient:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def watch(self, target: str, *, cursor: int = 0) -> dict:
        del target
        self.calls.append(cursor)
        return {
            'job_id': 'job_demo',
            'agent_name': 'codex',
            'cursor': cursor,
            'generation': 2,
            'terminal': True,
            'status': 'completed',
            'reply': 'done',
            'events': [],
        }


class _StreamingWatchClient:
    def __init__(self, *responses: dict) -> None:
        self._responses = list(responses)
        self.calls: list[int] = []

    def watch(self, target: str, *, cursor: int = 0) -> dict:
        del target
        self.calls.append(cursor)
        if not self._responses:
            raise AssertionError('no more watch responses configured')
        return dict(self._responses.pop(0))


class _StreamingThenFlakyWatchClient:
    def __init__(self, response: dict) -> None:
        self._response = dict(response)
        self._used = False
        self.calls: list[int] = []

    def watch(self, target: str, *, cursor: int = 0) -> dict:
        del target
        self.calls.append(cursor)
        if not self._used:
            self._used = True
            return dict(self._response)
        raise CcbdClientError('socket closed during generation switch')


class _FlakyPendClient:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, job_id: str) -> dict:
        del job_id
        self.calls += 1
        raise CcbdClientError('stale socket')


class _StablePendClient:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, job_id: str) -> dict:
        del job_id
        self.calls += 1
        return {
            'job_id': 'job_demo',
            'agent_name': 'codex',
            'status': 'completed',
            'reply': 'done',
            'completion_reason': 'task_complete',
            'completion_confidence': 'exact',
            'updated_at': '2026-03-18T00:00:10Z',
            'generation': 2,
        }


class _MailboxPendClient:
    def request(self, op: str, payload: dict) -> dict:
        assert op == 'get'
        assert payload == {'agent_name': 'claude'}
        return {
            'job_id': 'job_demo',
            'agent_name': 'claude',
            'status': 'running',
            'reply': '',
            'completion_reason': None,
            'completion_confidence': None,
            'updated_at': '2026-03-18T00:00:05Z',
            'generation': 2,
        }

    def mailbox_head(self, agent_name: str) -> dict:
        assert agent_name == 'claude'
        return {
            'target': 'claude',
            'head': {
                'reply_id': 'rep_1',
                'source_actor': 'codex',
                'reply_terminal_status': 'incomplete',
                'reply_notice': True,
                'reply_notice_kind': 'heartbeat',
                'reply_finished_at': '2026-03-18T00:10:00Z',
                'reply_last_progress_at': '2026-03-18T00:00:00Z',
                'reply_heartbeat_silence_seconds': 600.0,
                'job_id': 'job_demo',
                'reply': 'task still running',
            },
        }

    def inbox(self, agent_name: str) -> dict:
        raise AssertionError('pend should use mailbox_head before inbox fallback')


class _MailboxPendFallbackClient:
    def request(self, op: str, payload: dict) -> dict:
        assert op == 'get'
        assert payload == {'agent_name': 'claude'}
        raise CcbdClientError('get unavailable')

    def inbox(self, agent_name: str, *, detail=None) -> dict:
        assert agent_name == 'claude'
        assert detail is False
        return {
            'target': 'claude',
            'summary_status': 'ok',
            'head': {
                'reply_id': 'rep_2',
                'source_actor': 'codex',
                'reply_terminal_status': 'completed',
                'reply_notice': False,
                'reply_notice_kind': None,
                'reply_finished_at': '2026-03-18T00:20:00Z',
                'reply_last_progress_at': None,
                'reply_heartbeat_silence_seconds': None,
                'job_id': 'job_demo',
                'reply': 'done from inbox fallback',
            },
        }


class _MailboxPendSummaryMissingClient:
    def request(self, op: str, payload: dict) -> dict:
        assert op == 'get'
        assert payload == {'agent_name': 'claude'}
        return {
            'job_id': 'job_demo',
            'agent_name': 'claude',
            'status': 'running',
            'reply': '',
            'completion_reason': None,
            'completion_confidence': None,
            'updated_at': '2026-03-18T00:00:05Z',
            'generation': 2,
        }

    def mailbox_head(self, agent_name: str) -> dict:
        assert agent_name == 'claude'
        return {
            'target': 'claude',
            'summary_status': 'missing',
            'summary_error': None,
            'head': None,
        }


def _context(project_root: Path) -> CliContext:
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    command = ParsedWatchCommand(project=None, target='job_demo')
    return CliContextBuilder().build(command, cwd=project_root, bootstrap_if_missing=False)


def _persist_terminal_job(project_root: Path, *, job_id: str = 'job_demo') -> None:
    layout = PathLayout(project_root)
    request = MessageEnvelope(
        project_id='proj_demo',
        to_agent='codex',
        from_actor='user',
        body='hello',
        task_id=None,
        reply_to=None,
        message_type='ask',
        delivery_scope=DeliveryScope.SINGLE,
    )
    decision = CompletionDecision(
        terminal=True,
        status=CompletionStatus.COMPLETED,
        reason='session_reply_stable',
        confidence=CompletionConfidence.OBSERVED,
        reply='persisted reply',
        anchor_seen=True,
        reply_started=True,
        reply_stable=True,
        provider_turn_ref=None,
        source_cursor=None,
        finished_at='2026-03-18T00:00:02Z',
        diagnostics={},
    )
    JobStore(layout).append(
        JobRecord(
            job_id=job_id,
            submission_id='sub_demo',
            agent_name='codex',
            provider='codex',
            request=request,
            status=JobStatus.COMPLETED,
            terminal_decision=decision.to_record(),
            cancel_requested_at=None,
            created_at='2026-03-18T00:00:00Z',
            updated_at='2026-03-18T00:00:02Z',
            workspace_path=str(project_root),
        )
    )
    JobEventStore(layout).append(
        JobEvent(
            event_id='evt1',
            job_id=job_id,
            agent_name='codex',
            type='job_completed',
            payload={'status': 'completed'},
            timestamp='2026-03-18T00:00:02Z',
        )
    )
    SnapshotWriter(layout).write_completion(
        job_id=job_id,
        agent_name='codex',
        profile_family=CompletionFamily.ANCHORED_SESSION_STABILITY,
        state=CompletionState(
            anchor_seen=True,
            reply_started=True,
            reply_stable=True,
            terminal=True,
        ),
        decision=decision,
        updated_at='2026-03-18T00:00:02Z',
    )


def test_watch_target_reconnects_after_socket_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    project_root.mkdir()
    context = _context(project_root)
    flaky = _FlakyWatchClient()
    stable = _StableWatchClient()
    handles = iter(
        [
            SimpleNamespace(client=flaky),
            SimpleNamespace(client=stable),
        ]
    )
    seen: list[bool] = []

    def _connect(context, allow_restart_stale):
        seen.append(allow_restart_stale)
        return next(handles)

    monkeypatch.setattr(watch_service, 'connect_mounted_daemon', _connect)
    monkeypatch.setenv('CCB_WATCH_TIMEOUT_S', '1')
    monkeypatch.setenv('CCB_WATCH_POLL_INTERVAL_S', '0')

    batches = list(watch_service.watch_target(context, ParsedWatchCommand(project=None, target='job_demo')))
    assert len(batches) == 1
    assert batches[0].terminal is True
    assert batches[0].generation == 2
    assert flaky.calls == [0]
    assert stable.calls == [0]
    assert seen == [False, False]


def test_watch_target_preserves_cursor_across_reconnect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-cursor'
    project_root.mkdir()
    context = _context(project_root)
    first = _StreamingThenFlakyWatchClient(
        {
            'job_id': 'job_demo',
            'agent_name': 'codex',
            'cursor': 2,
            'generation': 1,
            'terminal': False,
            'status': 'running',
            'reply': 'partial',
            'events': [
                {'event_id': 'evt1', 'job_id': 'job_demo', 'agent_name': 'codex', 'type': 'job_started', 'timestamp': '2026-03-18T00:00:01Z'},
            ],
        }
    )
    second = _StreamingWatchClient(
        {
            'job_id': 'job_demo',
            'agent_name': 'codex',
            'cursor': 4,
            'generation': 2,
            'terminal': True,
            'status': 'completed',
            'reply': 'final',
            'events': [
                {'event_id': 'evt2', 'job_id': 'job_demo', 'agent_name': 'codex', 'type': 'completion_terminal', 'timestamp': '2026-03-18T00:00:02Z'},
                {'event_id': 'evt3', 'job_id': 'job_demo', 'agent_name': 'codex', 'type': 'job_completed', 'timestamp': '2026-03-18T00:00:02Z'},
            ],
        }
    )
    handles = iter(
        [
            SimpleNamespace(client=first),
            SimpleNamespace(client=second),
        ]
    )
    seen: list[bool] = []

    def _connect(context, allow_restart_stale):
        seen.append(allow_restart_stale)
        return next(handles)

    monkeypatch.setattr(watch_service, 'connect_mounted_daemon', _connect)
    monkeypatch.setenv('CCB_WATCH_TIMEOUT_S', '1')
    monkeypatch.setenv('CCB_WATCH_POLL_INTERVAL_S', '0')

    batches = list(watch_service.watch_target(context, ParsedWatchCommand(project=None, target='job_demo')))
    assert len(batches) == 2
    assert [batch.cursor for batch in batches] == [2, 4]
    assert [batch.generation for batch in batches] == [1, 2]
    assert [batch.terminal for batch in batches] == [False, True]
    assert first.calls == [0, 2]
    assert second.calls == [2]
    assert seen == [False, False]


def test_watch_target_retries_when_reconnect_attempt_temporarily_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / 'repo-watch-reconnect-step-fail'
    project_root.mkdir()
    context = _context(project_root)
    flaky = _FlakyWatchClient()
    stable = _StableWatchClient()
    connects = {'count': 0}
    seen: list[bool] = []

    def _connect(context, allow_restart_stale):
        del context
        seen.append(allow_restart_stale)
        connects['count'] += 1
        if connects['count'] == 1:
            return SimpleNamespace(client=flaky)
        if connects['count'] == 2:
            raise CcbdServiceError('daemon restarting')
        return SimpleNamespace(client=stable)

    monkeypatch.setattr(watch_service, 'connect_mounted_daemon', _connect)
    monkeypatch.setenv('CCB_WATCH_TIMEOUT_S', '1')
    monkeypatch.setenv('CCB_WATCH_POLL_INTERVAL_S', '0')

    batches = list(watch_service.watch_target(context, ParsedWatchCommand(project=None, target='job_demo')))

    assert len(batches) == 1
    assert batches[0].terminal is True
    assert batches[0].reply == 'done'
    assert flaky.calls == [0]
    assert stable.calls == [0]
    assert seen == [False, False, False]


def test_watch_target_falls_back_to_persisted_terminal_job_when_daemon_stays_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / 'repo-watch-fallback'
    project_root.mkdir()
    context = _context(project_root)
    _persist_terminal_job(project_root)

    seen: list[bool] = []

    def _connect(context, allow_restart_stale):
        del context
        seen.append(allow_restart_stale)
        raise CcbdServiceError('daemon restarting')

    monkeypatch.setattr(watch_service, 'connect_mounted_daemon', _connect)
    monkeypatch.setenv('CCB_WATCH_TIMEOUT_S', '1')
    monkeypatch.setenv('CCB_WATCH_POLL_INTERVAL_S', '0')

    batches = list(watch_service.watch_target(context, ParsedWatchCommand(project=None, target='job_demo')))

    assert len(batches) == 1
    assert batches[0].terminal is True
    assert batches[0].status == 'completed'
    assert batches[0].reply == 'persisted reply'
    assert [event['event_id'] for event in batches[0].events] == ['evt1']
    assert seen == [False]


def test_watch_target_initial_connect_error_still_raises_without_persisted_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / 'repo-watch-no-fallback'
    project_root.mkdir()
    context = _context(project_root)

    seen: list[bool] = []

    def _connect(context, allow_restart_stale):
        del context
        seen.append(allow_restart_stale)
        raise CcbdServiceError('project ccbd is unmounted; run `ccb` first')

    monkeypatch.setattr(watch_service, 'connect_mounted_daemon', _connect)

    with pytest.raises(CcbdServiceError, match='project ccbd is unmounted'):
        list(watch_service.watch_target(context, ParsedWatchCommand(project=None, target='job_demo')))
    assert seen == [False]


def test_ack_reply_uses_non_mutating_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-ack'
    project_root.mkdir()
    context = _context(project_root)
    seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        del context, request_fn
        seen.append(allow_restart_stale)
        return {'ok': True}

    monkeypatch.setattr(ack_service, 'invoke_mounted_daemon', _invoke)

    payload = ack_service.ack_reply(context, ParsedAckCommand(project=None, agent_name='claude', inbound_event_id='iev_123'))

    assert payload == {'ok': True}
    assert seen == [False]


def test_cancel_job_uses_non_mutating_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-cancel'
    project_root.mkdir()
    context = _context(project_root)
    seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        del context, request_fn
        seen.append(allow_restart_stale)
        return {'ok': True}

    monkeypatch.setattr(cancel_service, 'invoke_mounted_daemon', _invoke)

    payload = cancel_service.cancel_job(context, ParsedCancelCommand(project=None, job_id='job_123'))

    assert payload == {'ok': True}
    assert seen == [False]


def test_retry_attempt_uses_non_mutating_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-retry'
    project_root.mkdir()
    context = _context(project_root)
    seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        del context, request_fn
        seen.append(allow_restart_stale)
        return {
            'target': 'att_123',
            'message_id': 'msg_123',
            'original_attempt_id': 'att_122',
            'attempt_id': 'att_123',
            'job_id': 'job_123',
            'agent_name': 'claude',
            'status': 'queued',
        }

    monkeypatch.setattr(retry_service, 'invoke_mounted_daemon', _invoke)

    payload = retry_service.retry_attempt(context, ParsedRetryCommand(project=None, target='att_123'))

    assert payload.target == 'att_123'
    assert payload.message_id == 'msg_123'
    assert seen == [False]


def test_resubmit_message_uses_non_mutating_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-resubmit'
    project_root.mkdir()
    context = _context(project_root)
    seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        del context, request_fn
        seen.append(allow_restart_stale)
        return {
            'original_message_id': 'msg_old',
            'message_id': 'msg_new',
            'submission_id': 'sub_new',
            'jobs': ({'job_id': 'job_123', 'agent_name': 'claude'},),
        }

    monkeypatch.setattr(resubmit_service, 'invoke_mounted_daemon', _invoke)

    payload = resubmit_service.resubmit_message(context, ParsedResubmitCommand(project=None, message_id='msg_old'))

    assert payload.original_message_id == 'msg_old'
    assert payload.message_id == 'msg_new'
    assert seen == [False]


def test_watch_ask_job_falls_back_to_persisted_terminal_job_when_daemon_stays_unreachable(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / 'repo-ask-watch-fallback'
    project_root.mkdir()
    context = _context(project_root)
    _persist_terminal_job(project_root)
    out = StringIO()

    def _connect(context, allow_restart_stale):
        del context, allow_restart_stale
        raise CcbdServiceError('daemon restarting')

    batch = watch_ask_job_impl(
        context,
        'job_demo',
        out,
        timeout=1.0,
        emit_output=True,
        connect_mounted_daemon_fn=_connect,
        reconnect_error_classes=(CcbdClientError, CcbdServiceError),
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _seconds: None,
        poll_interval_seconds_fn=lambda: 0.0,
        timeout_seconds_fn=lambda: 1.0,
        render_watch_batch_fn=render_watch_batch,
        write_lines_fn=write_lines,
    )

    assert batch.terminal is True
    assert batch.status == 'completed'
    assert batch.reply == 'persisted reply'
    assert 'watch_status: terminal' in out.getvalue()
    assert 'reply: persisted reply' in out.getvalue()


def test_handle_watch_emits_non_terminal_observer_preamble_before_stream_batches() -> None:
    out = StringIO()

    batch = SimpleNamespace(
        events=(
            {
                'event_id': 'evt-1',
                'job_id': 'job_demo',
                'agent_name': 'codex',
                'type': 'job_started',
                'timestamp': '2026-03-18T00:00:00Z',
            },
        ),
        terminal=False,
        job_id='job_demo',
        agent_name='codex',
        target_name='codex',
        status='running',
        reply='',
    )
    writes: list[tuple[str, ...]] = []
    services = SimpleNamespace(
        render_observer_notice=render_observer_notice,
        watch_target=lambda context, command: [batch],
        render_watch_batch=render_watch_batch,
        write_lines=lambda out, lines: writes.append(tuple(lines)),
    )

    rc = handle_watch(None, None, out, services)

    assert rc == 0
    assert writes[0] == (
        'observer_view: watch',
        'observer_authority: supplementary_snapshot',
        'observer_terminal: false',
        'observer_notice: weak observer surface; non-terminal state may change; prefer ccb ask --wait / ccb ask wait <job_id>',
    )
    assert writes[1] == ('event: evt-1 job_demo codex job_started 2026-03-18T00:00:00Z',)


def test_pend_target_reconnects_after_socket_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    project_root.mkdir()
    context = _context(project_root)
    flaky = _FlakyPendClient()
    stable = _StablePendClient()
    handles = iter(
        [
            SimpleNamespace(client=flaky),
            SimpleNamespace(client=stable),
        ]
    )
    seen: list[bool] = []

    def _connect(context, allow_restart_stale):
        seen.append(allow_restart_stale)
        return next(handles)

    monkeypatch.setattr(pend_service, 'connect_mounted_daemon', _connect)

    payload = pend_service.pend_target(context, ParsedPendCommand(project=None, target='job_demo'))
    assert payload['status'] == 'completed'
    assert payload['generation'] == 2
    assert flaky.calls == 1
    assert stable.calls == 1
    assert seen == [False, False]


def test_pend_target_merges_mailbox_head_reply_for_agent_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-pend-mailbox'
    project_root.mkdir()
    context = _context(project_root)

    seen: list[bool] = []

    def _connect(context, allow_restart_stale):
        seen.append(allow_restart_stale)
        return SimpleNamespace(client=_MailboxPendClient())

    monkeypatch.setattr(pend_service, 'connect_mounted_daemon', _connect)

    payload = pend_service.pend_target(context, ParsedPendCommand(project=None, target='claude'))

    assert payload['status'] == 'running'
    assert payload['mailbox_summary_status'] is None
    assert payload['mailbox_reply_ready'] is True
    assert payload['mailbox_reply_id'] == 'rep_1'
    assert payload['mailbox_reply_notice'] is True
    assert payload['mailbox_reply_notice_kind'] == 'heartbeat'
    assert payload['mailbox_reply_job_id'] == 'job_demo'
    assert seen == [False]


def test_pend_target_uses_summary_only_inbox_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-pend-mailbox-fallback'
    project_root.mkdir()
    context = _context(project_root)

    def _connect(context, allow_restart_stale):
        del context, allow_restart_stale
        return SimpleNamespace(client=_MailboxPendFallbackClient())

    monkeypatch.setattr(pend_service, 'connect_mounted_daemon', _connect)

    payload = pend_service.pend_target(context, ParsedPendCommand(project=None, target='claude'))

    assert payload['status'] == 'mailbox_reply'
    assert payload['mailbox_reply_ready'] is True
    assert payload['mailbox_reply_id'] == 'rep_2'
    assert payload['mailbox_reply'] == 'done from inbox fallback'


def test_pend_target_surfaces_mailbox_summary_missing_without_fake_reply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-pend-summary-missing'
    project_root.mkdir()
    context = _context(project_root)

    def _connect(context, allow_restart_stale):
        del context, allow_restart_stale
        return SimpleNamespace(client=_MailboxPendSummaryMissingClient())

    monkeypatch.setattr(pend_service, 'connect_mounted_daemon', _connect)

    payload = pend_service.pend_target(context, ParsedPendCommand(project=None, target='claude'))

    assert payload['status'] == 'running'
    assert payload['mailbox_summary_status'] == 'missing'
    assert payload.get('mailbox_reply_ready') is None


def test_queue_target_uses_non_mutating_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-queue'
    project_root.mkdir()
    context = _context(project_root)
    seen: list[bool] = []
    detail_seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        seen.append(allow_restart_stale)
        return request_fn(
            SimpleNamespace(
                queue=lambda target, *, detail=None: detail_seen.append(bool(detail)) or {
                    'target': target,
                    'agent_count': 0,
                    'queued_agent_count': 0,
                    'total_queue_depth': 0,
                    'agents': [],
                }
            )
        )

    monkeypatch.setattr(queue_service, 'invoke_mounted_daemon', _invoke)

    payload = queue_service.queue_target(context, ParsedQueueCommand(project=None, target='all'))

    assert payload['target'] == 'all'
    assert seen == [False]
    assert detail_seen == [False]


def test_trace_target_uses_non_mutating_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-trace'
    project_root.mkdir()
    context = _context(project_root)
    seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        seen.append(allow_restart_stale)
        return request_fn(SimpleNamespace(trace=lambda target: {'target': target, 'resolved_kind': 'job'}))

    monkeypatch.setattr(trace_service, 'invoke_mounted_daemon', _invoke)

    payload = trace_service.trace_target(context, ParsedTraceCommand(project=None, target='job_demo'))

    assert payload['target'] == 'job_demo'
    assert seen == [False]


def test_inbox_target_uses_non_mutating_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-inbox'
    project_root.mkdir()
    context = _context(project_root)
    seen: list[bool] = []
    detail_seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        seen.append(allow_restart_stale)
        return request_fn(
            SimpleNamespace(
                inbox=lambda agent_name, *, detail=None: detail_seen.append(bool(detail)) or {
                    'agent': {'agent_name': agent_name}
                }
            )
        )

    monkeypatch.setattr(inbox_service, 'invoke_mounted_daemon', _invoke)

    payload = inbox_service.inbox_target(context, ParsedInboxCommand(project=None, agent_name='claude'))

    assert payload['agent']['agent_name'] == 'claude'
    assert seen == [False]
    assert detail_seen == [False]


def test_queue_target_passes_detail_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-queue-detail'
    project_root.mkdir()
    context = _context(project_root)
    detail_seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        del context, allow_restart_stale
        return request_fn(
            SimpleNamespace(
                queue=lambda target, *, detail=None: detail_seen.append(bool(detail)) or {'target': target, 'agent': {}}
            )
        )

    monkeypatch.setattr(queue_service, 'invoke_mounted_daemon', _invoke)

    queue_service.queue_target(context, ParsedQueueCommand(project=None, target='claude', detail=True))

    assert detail_seen == [True]


def test_inbox_target_passes_detail_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-inbox-detail'
    project_root.mkdir()
    context = _context(project_root)
    detail_seen: list[bool] = []

    def _invoke(context, *, allow_restart_stale, request_fn):
        del context, allow_restart_stale
        return request_fn(
            SimpleNamespace(
                inbox=lambda agent_name, *, detail=None: detail_seen.append(bool(detail)) or {'agent': {'agent_name': agent_name}}
            )
        )

    monkeypatch.setattr(inbox_service, 'invoke_mounted_daemon', _invoke)

    inbox_service.inbox_target(context, ParsedInboxCommand(project=None, agent_name='claude', detail=True))

    assert detail_seen == [True]
