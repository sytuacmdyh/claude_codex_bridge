from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ccbd.api_models import DeliveryScope, JobRecord, JobStatus, MessageEnvelope, TargetKind
from completion.models import CompletionConfidence, CompletionDecision, CompletionStatus
from message_bureau import AttemptStore, MessageBureauFacade
from message_bureau.control import MessageBureauControlService
from message_bureau.control_queue_runtime.views_runtime.agent import agent_queue
from mailbox_kernel import InboundEventStatus, InboundEventStore, InboundEventType, MailboxStore
from storage.paths import PathLayout


def _job(job_id: str, *, agent_name: str) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        submission_id=None,
        agent_name=agent_name,
        provider='codex',
        provider_instance=None,
        provider_options=None,
        workspace_path=str(Path('/tmp') / agent_name),
        target_kind=TargetKind.AGENT,
        target_name=agent_name,
        request=MessageEnvelope(
            project_id='proj-1',
            to_agent=agent_name,
            from_actor='user',
            body='hello',
            task_id=None,
            reply_to=None,
            message_type='ask',
            delivery_scope=DeliveryScope.SINGLE,
        ),
        status=JobStatus.ACCEPTED,
        terminal_decision=None,
        cancel_requested_at=None,
        created_at='2026-05-08T00:00:00Z',
        updated_at='2026-05-08T00:00:00Z',
    )


def test_record_submission_does_not_refresh_mailbox(tmp_path: Path, monkeypatch) -> None:
    layout = PathLayout(tmp_path / 'repo')
    control = MessageBureauControlService(
        layout,
        SimpleNamespace(agents={'agent1': {}}, cmd_enabled=True),
        clock=lambda: '2026-05-08T00:00:00Z',
    )
    bureau = MessageBureauFacade(
        layout,
        config=SimpleNamespace(agents={'agent1': {}}, cmd_enabled=True),
        clock=lambda: '2026-05-08T00:00:00Z',
    )

    message_id = bureau.record_submission(
        MessageEnvelope(
            project_id='proj-1',
            to_agent='agent1',
            from_actor='user',
            body='hello',
            task_id=None,
            reply_to=None,
            message_type='ask',
            delivery_scope=DeliveryScope.SINGLE,
        ),
        (_job('job_1', agent_name='agent1'),),
        submission_id=None,
        accepted_at='2026-05-08T00:00:00Z',
    )

    assert message_id is not None
    attempts = AttemptStore(layout).list_message(message_id)
    assert len(attempts) == 1
    events = InboundEventStore(layout).list_agent('agent1')
    assert len(events) == 1
    assert events[0].event_type is InboundEventType.TASK_REQUEST
    assert events[0].status is InboundEventStatus.QUEUED
    mailbox = MailboxStore(layout).load('agent1')
    assert mailbox is not None
    assert mailbox.queue_depth == 1
    assert mailbox.pending_reply_count == 0
    assert mailbox.mailbox_state.value == 'blocked'
    queue = agent_queue(control, 'agent1')
    assert queue['queue_depth'] == 1
    assert queue['mailbox_state'] == 'blocked'


def test_record_retry_attempt_does_not_refresh_mailbox(tmp_path: Path, monkeypatch) -> None:
    layout = PathLayout(tmp_path / 'repo')
    control = MessageBureauControlService(
        layout,
        SimpleNamespace(agents={'agent1': {}}, cmd_enabled=True),
        clock=lambda: '2026-05-08T00:00:00Z',
    )
    bureau = MessageBureauFacade(
        layout,
        config=SimpleNamespace(agents={'agent1': {}}, cmd_enabled=True),
        clock=lambda: '2026-05-08T00:00:00Z',
    )

    original_message_id = bureau.record_submission(
        MessageEnvelope(
            project_id='proj-1',
            to_agent='agent1',
            from_actor='user',
            body='hello',
            task_id=None,
            reply_to=None,
            message_type='ask',
            delivery_scope=DeliveryScope.SINGLE,
        ),
        (_job('job_1', agent_name='agent1'),),
        submission_id=None,
        accepted_at='2026-05-08T00:00:00Z',
    )
    assert original_message_id is not None

    attempt_id = bureau.record_retry_attempt(
        original_message_id,
        _job('job_2', agent_name='agent1'),
        accepted_at='2026-05-08T00:01:00Z',
    )

    assert attempt_id
    attempts = AttemptStore(layout).list_message(original_message_id)
    assert len(attempts) == 2
    events = InboundEventStore(layout).list_agent('agent1')
    assert len(events) == 2
    assert events[-1].event_type is InboundEventType.TASK_REQUEST
    mailbox = MailboxStore(layout).load('agent1')
    assert mailbox is not None
    assert mailbox.queue_depth == 2
    assert mailbox.pending_reply_count == 0
    assert mailbox.mailbox_state.value == 'blocked'
    queue = agent_queue(control, 'agent1')
    assert queue['queue_depth'] == 2


def test_record_reply_delivery_skips_non_mailbox_caller(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    bureau = MessageBureauFacade(
        layout,
        config=SimpleNamespace(agents={'agent1': {}}, cmd_enabled=True),
        clock=lambda: '2026-05-08T00:00:00Z',
    )

    message_id = bureau.record_submission(
        MessageEnvelope(
            project_id='proj-1',
            to_agent='agent1',
            from_actor='user',
            body='hello',
            task_id=None,
            reply_to=None,
            message_type='ask',
            delivery_scope=DeliveryScope.SINGLE,
        ),
        (_job('job_1', agent_name='agent1'),),
        submission_id=None,
        accepted_at='2026-05-08T00:00:00Z',
    )
    assert message_id is not None

    decision = CompletionDecision(
        terminal=True,
        status=CompletionStatus.COMPLETED,
        reason='task_complete',
        confidence=CompletionConfidence.EXACT,
        reply='done',
        anchor_seen=True,
        reply_started=True,
        reply_stable=True,
        provider_turn_ref=None,
        source_cursor=None,
        finished_at='2026-05-08T00:01:00Z',
        diagnostics={},
    )

    bureau.record_reply(
        replace(
            _job('job_1', agent_name='agent1'),
            status=JobStatus.COMPLETED,
            terminal_decision=decision.to_record(),
        ),
        decision,
        finished_at='2026-05-08T00:01:00Z',
        deliver_to_caller=True,
    )

    assert not (layout.ccbd_mailboxes_dir / 'user').exists()
