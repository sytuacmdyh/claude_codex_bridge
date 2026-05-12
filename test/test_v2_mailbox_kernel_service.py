from __future__ import annotations

from pathlib import Path

import pytest

from mailbox_kernel import (
    DeliveryLeaseStore,
    InboundEventRecord,
    InboundEventStatus,
    InboundEventStore,
    InboundEventType,
    MailboxKernelService,
    MailboxState,
    MailboxStore,
)
from storage.paths import PathLayout


def test_mailbox_kernel_claim_and_consume_updates_mailbox_and_lease(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    inbound_store = InboundEventStore(layout)
    mailbox_store = MailboxStore(layout)
    lease_store = DeliveryLeaseStore(layout)
    service = MailboxKernelService(
        layout,
        clock=lambda: '2026-03-30T10:00:00Z',
        inbound_store=inbound_store,
        mailbox_store=mailbox_store,
        lease_store=lease_store,
    )

    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-task',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REQUEST,
            message_id='msg-1',
            attempt_id='att-1',
            payload_ref='job:job-1',
            priority=100,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:00Z',
        )
    )
    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-reply',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REPLY,
            message_id='msg-2',
            attempt_id='att-2',
            payload_ref='reply:rep-2',
            priority=10,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:01Z',
        )
    )

    claimed = service.claim_next('agent1', event_type=InboundEventType.TASK_REQUEST, started_at='2026-03-30T10:00:05Z')

    assert claimed is not None
    assert claimed.inbound_event_id == 'evt-task'
    assert claimed.status is InboundEventStatus.DELIVERING
    lease = lease_store.load('agent1')
    assert lease is not None
    assert lease.inbound_event_id == 'evt-task'
    mailbox = mailbox_store.load('agent1')
    assert mailbox is not None
    assert mailbox.mailbox_state is MailboxState.DELIVERING
    assert mailbox.summary_version == 1
    assert mailbox.summary_source == 'history-refresh'
    assert mailbox.active_inbound_event_id == 'evt-task'
    assert mailbox.queue_depth == 2
    assert mailbox.pending_reply_count == 1

    consumed = service.consume('agent1', 'evt-task', finished_at='2026-03-30T10:00:10Z')

    assert consumed is not None
    assert consumed.status is InboundEventStatus.CONSUMED
    assert lease_store.load('agent1') is None
    mailbox = mailbox_store.load('agent1')
    assert mailbox is not None
    assert mailbox.mailbox_state is MailboxState.BLOCKED
    assert mailbox.summary_version == 2
    assert mailbox.summary_source == 'transition-terminal'
    assert mailbox.active_inbound_event_id is None
    assert mailbox.queue_depth == 1
    assert mailbox.pending_reply_count == 1


def test_mailbox_kernel_rejects_second_claim_while_other_event_is_active(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    inbound_store = InboundEventStore(layout)
    service = MailboxKernelService(layout, clock=lambda: '2026-03-30T10:00:00Z', inbound_store=inbound_store)

    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-1',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REQUEST,
            message_id='msg-1',
            attempt_id='att-1',
            payload_ref='job:job-1',
            priority=100,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:00Z',
        )
    )
    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-2',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REQUEST,
            message_id='msg-2',
            attempt_id='att-2',
            payload_ref='job:job-2',
            priority=100,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:01Z',
        )
    )

    first = service.claim_next('agent1', event_type=InboundEventType.TASK_REQUEST, started_at='2026-03-30T10:00:05Z')
    second = service.claim('agent1', 'evt-2', started_at='2026-03-30T10:00:06Z')

    assert first is not None
    assert second is None
    mailbox = MailboxStore(layout).load('agent1')
    assert mailbox is not None
    assert mailbox.mailbox_state is MailboxState.DELIVERING
    assert mailbox.summary_version == 1
    assert mailbox.active_inbound_event_id == 'evt-1'


def test_mailbox_kernel_ack_reply_claims_and_consumes_head_reply(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    inbound_store = InboundEventStore(layout)
    mailbox_store = MailboxStore(layout)
    lease_store = DeliveryLeaseStore(layout)
    service = MailboxKernelService(
        layout,
        clock=lambda: '2026-03-30T10:00:00Z',
        inbound_store=inbound_store,
        mailbox_store=mailbox_store,
        lease_store=lease_store,
    )

    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-reply',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REPLY,
            message_id='msg-1',
            attempt_id='att-1',
            payload_ref='reply:rep-1',
            priority=10,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:00Z',
        )
    )
    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-task',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REQUEST,
            message_id='msg-2',
            attempt_id='att-2',
            payload_ref='job:job-2',
            priority=100,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:01Z',
        )
    )

    consumed = service.ack_reply(
        'agent1',
        'evt-reply',
        started_at='2026-03-30T10:00:05Z',
        finished_at='2026-03-30T10:00:05Z',
    )

    assert consumed is not None
    assert consumed.inbound_event_id == 'evt-reply'
    assert consumed.status is InboundEventStatus.CONSUMED
    current = inbound_store.get_latest('agent1', 'evt-reply')
    assert current is not None
    assert current.started_at == '2026-03-30T10:00:05Z'
    assert current.finished_at == '2026-03-30T10:00:05Z'
    assert lease_store.load('agent1') is None
    mailbox = mailbox_store.load('agent1')
    assert mailbox is not None
    assert mailbox.mailbox_state is MailboxState.BLOCKED
    assert mailbox.summary_version == 2
    assert mailbox.summary_source == 'transition-terminal'
    assert mailbox.queue_depth == 1
    assert mailbox.pending_reply_count == 0


def test_mailbox_kernel_rewrite_head_uses_transition_writer_for_reply_delivery(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    inbound_store = InboundEventStore(layout)
    mailbox_store = MailboxStore(layout)
    lease_store = DeliveryLeaseStore(layout)
    service = MailboxKernelService(
        layout,
        clock=lambda: '2026-03-30T10:00:00Z',
        inbound_store=inbound_store,
        mailbox_store=mailbox_store,
        lease_store=lease_store,
    )

    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-reply',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REPLY,
            message_id='msg-1',
            attempt_id='att-1',
            payload_ref='reply:rep-1',
            priority=10,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:00Z',
        )
    )
    service.apply_incremental_summary_update(
        'agent1',
        queue_delta=1,
        pending_reply_delta=1,
        updated_at='2026-03-30T10:00:00Z',
    )

    rewritten = service.rewrite_head(
        'agent1',
        'evt-reply',
        payload_ref='reply:rep-1 delivery:job-delivery-1',
        status=InboundEventStatus.QUEUED,
        updated_at='2026-03-30T10:00:05Z',
        clear_progress=True,
    )

    assert rewritten is not None
    assert rewritten.payload_ref == 'reply:rep-1 delivery:job-delivery-1'
    assert rewritten.status is InboundEventStatus.QUEUED
    assert rewritten.started_at is None
    assert rewritten.finished_at is None
    mailbox = mailbox_store.load('agent1')
    assert mailbox is not None
    assert mailbox.summary_source == 'transition-rewrite-head'
    assert mailbox.summary_version == 2
    assert mailbox.queue_depth == 1
    assert mailbox.pending_reply_count == 1
    assert mailbox.head_inbound_event_id == 'evt-reply'
    assert mailbox.head_payload_ref == 'reply:rep-1 delivery:job-delivery-1'


def test_mailbox_kernel_transition_writer_uses_existing_summary_authority(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    inbound_store = InboundEventStore(layout)
    mailbox_store = MailboxStore(layout)
    lease_store = DeliveryLeaseStore(layout)
    service = MailboxKernelService(
        layout,
        clock=lambda: '2026-03-30T10:00:00Z',
        inbound_store=inbound_store,
        mailbox_store=mailbox_store,
        lease_store=lease_store,
    )

    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-task',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REQUEST,
            message_id='msg-1',
            attempt_id='att-1',
            payload_ref='job:job-1',
            priority=100,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:00Z',
        )
    )
    inbound_store.append(
        InboundEventRecord(
            inbound_event_id='evt-reply',
            agent_name='agent1',
            event_type=InboundEventType.TASK_REPLY,
            message_id='msg-2',
            attempt_id='att-2',
            payload_ref='reply:rep-2',
            priority=10,
            status=InboundEventStatus.QUEUED,
            created_at='2026-03-30T10:00:01Z',
        )
    )
    service.apply_incremental_summary_update(
        'agent1',
        queue_delta=2,
        pending_reply_delta=1,
        updated_at='2026-03-30T10:00:02Z',
    )

    claimed = service.claim_next('agent1', event_type=InboundEventType.TASK_REQUEST, started_at='2026-03-30T10:00:05Z')

    assert claimed is not None
    mailbox = mailbox_store.load('agent1')
    assert mailbox is not None
    assert mailbox.summary_version == 2
    assert mailbox.summary_source == 'transition-claim'
    assert mailbox.queue_depth == 2
    assert mailbox.pending_reply_count == 1
    assert mailbox.active_inbound_event_id == 'evt-task'
    assert mailbox.head_inbound_event_id == 'evt-task'
    assert mailbox.head_status == 'delivering'

    consumed = service.consume('agent1', 'evt-task', finished_at='2026-03-30T10:00:10Z')

    assert consumed is not None
    mailbox = mailbox_store.load('agent1')
    assert mailbox is not None
    assert mailbox.summary_version == 3
    assert mailbox.summary_source == 'transition-terminal'
    assert mailbox.queue_depth == 1
    assert mailbox.pending_reply_count == 1
    assert mailbox.active_inbound_event_id is None
    assert mailbox.head_inbound_event_id == 'evt-reply'
    assert mailbox.head_status == 'queued'


def test_mailbox_kernel_ack_reply_rejects_cmd_mailbox_owner(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    inbound_store = InboundEventStore(layout)
    mailbox_store = MailboxStore(layout)
    lease_store = DeliveryLeaseStore(layout)
    service = MailboxKernelService(
        layout,
        clock=lambda: '2026-03-30T10:00:00Z',
        inbound_store=inbound_store,
        mailbox_store=mailbox_store,
        lease_store=lease_store,
    )

    with pytest.raises(ValueError, match="actor 'cmd' does not own a mailbox"):
        inbound_store.append(
            InboundEventRecord(
                inbound_event_id='evt-cmd-reply',
                agent_name='cmd',
                event_type=InboundEventType.TASK_REPLY,
                message_id='msg-cmd',
                attempt_id='att-cmd',
                payload_ref='reply:rep-cmd',
                priority=10,
                status=InboundEventStatus.QUEUED,
                created_at='2026-03-30T10:00:00Z',
            )
        )


def test_mailbox_kernel_rebuild_mailbox_summary_returns_newer_summary_when_cas_is_stale(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    inbound_store = InboundEventStore(layout)
    mailbox_store = MailboxStore(layout)
    lease_store = DeliveryLeaseStore(layout)
    service = MailboxKernelService(
        layout,
        clock=lambda: '2026-03-30T10:00:00Z',
        inbound_store=inbound_store,
        mailbox_store=mailbox_store,
        lease_store=lease_store,
    )

    mailbox_store.save(
        service._mailbox_record_cls(
            mailbox_id='mbx_agent1',
            agent_name='agent1',
            summary_version=2,
            summary_source='transition-claim',
            summary_refreshed_at='2026-03-30T10:00:10Z',
            active_inbound_event_id='evt-task',
            queue_depth=1,
            pending_reply_count=0,
            head_inbound_event_id='evt-task',
            head_event_type='task_request',
            head_status='delivering',
            head_message_id='msg-1',
            head_attempt_id='att-1',
            head_payload_ref='job:job-1',
            last_inbound_started_at='2026-03-30T10:00:10Z',
            last_inbound_finished_at=None,
            mailbox_state=MailboxState.DELIVERING,
            lease_version=1,
            updated_at='2026-03-30T10:00:10Z',
        )
    )

    stale_candidate = service._mailbox_record_cls(
        mailbox_id='mbx_agent1',
        agent_name='agent1',
        summary_version=1,
        summary_source='history-refresh',
        summary_refreshed_at='2026-03-30T10:00:00Z',
        active_inbound_event_id=None,
        queue_depth=0,
        pending_reply_count=0,
        head_inbound_event_id=None,
        head_event_type=None,
        head_status=None,
        head_message_id=None,
        head_attempt_id=None,
        head_payload_ref=None,
        last_inbound_started_at=None,
        last_inbound_finished_at=None,
        mailbox_state=MailboxState.IDLE,
        lease_version=0,
        updated_at='2026-03-30T10:00:00Z',
    )

    saved = mailbox_store.compare_and_save(stale_candidate, expected_summary_version=1)

    assert saved is False
    mailbox = mailbox_store.load('agent1')
    assert mailbox is not None
    assert mailbox.summary_version == 2
    assert mailbox.summary_source == 'transition-claim'
