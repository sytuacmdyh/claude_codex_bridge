from __future__ import annotations

from types import SimpleNamespace

from mailbox_kernel import InboundEventRecord, InboundEventStatus, InboundEventType, MailboxRecord, MailboxState
from message_bureau.control_queue import ack_reply, agent_queue, inbox, mailbox_head, queue_summary
from message_bureau.models import AttemptRecord, AttemptState, MessageRecord, MessageState, ReplyRecord, ReplyTerminalStatus
from message_bureau.reply_payloads import compose_reply_payload


def _mailbox_record(
    *,
    mailbox_id: str,
    agent_name: str,
    active_inbound_event_id,
    queue_depth: int,
    pending_reply_count: int,
    head_inbound_event_id,
    head_event_type,
    head_status,
    head_message_id,
    head_attempt_id,
    head_payload_ref,
    last_inbound_started_at,
    last_inbound_finished_at,
    mailbox_state,
    lease_version: int,
    updated_at: str,
    summary_version: int = 1,
    summary_source: str = 'history-refresh',
    summary_refreshed_at: str | None = None,
):
    return MailboxRecord(
        mailbox_id=mailbox_id,
        agent_name=agent_name,
        summary_version=summary_version,
        summary_source=summary_source,
        summary_refreshed_at=summary_refreshed_at or updated_at,
        active_inbound_event_id=active_inbound_event_id,
        queue_depth=queue_depth,
        pending_reply_count=pending_reply_count,
        head_inbound_event_id=head_inbound_event_id,
        head_event_type=head_event_type,
        head_status=head_status,
        head_message_id=head_message_id,
        head_attempt_id=head_attempt_id,
        head_payload_ref=head_payload_ref,
        last_inbound_started_at=last_inbound_started_at,
        last_inbound_finished_at=last_inbound_finished_at,
        mailbox_state=mailbox_state,
        lease_version=lease_version,
        updated_at=updated_at,
    )


class _MailboxStore:
    def __init__(self, records: dict[str, object | None]) -> None:
        self._records = records

    def load(self, agent_name: str):
        return self._records.get(agent_name)

    def list_all(self):
        return [record for record in self._records.values() if record is not None]


class _InboundStore:
    def __init__(self, records: dict[str, list[object]]) -> None:
        self._records = records

    def list_agent(self, agent_name: str):
        return list(self._records.get(agent_name, ()))


class _FailingInboundStore(_InboundStore):
    def list_agent(self, agent_name: str):
        raise AssertionError(f'queue all should not scan inbox history for {agent_name}')


class _HeaderOnlyInboundStore(_InboundStore):
    def __init__(self, records: dict[str, list[object]], *, latest_by_id: dict[tuple[str, str], object]) -> None:
        super().__init__(records)
        self._latest_by_id = latest_by_id

    def get_latest(self, agent_name: str, inbound_event_id: str):
        return self._latest_by_id.get((agent_name, inbound_event_id))


class _SingleStore:
    def __init__(self, records: dict[str, object], *, list_records: dict[str, list[object]] | None = None) -> None:
        self._records = records
        self._list_records = list_records or {}

    def get_latest(self, record_id: str):
        return self._records.get(record_id)

    def list_message(self, message_id: str):
        return list(self._list_records.get(message_id, ()))


class _MailboxKernel:
    def __init__(self, head, *, next_head=None) -> None:
        self._head = head
        self._next_head = next_head
        self._acked = False

    def head_pending_event(self, agent_name: str):
        del agent_name
        if self._acked:
            return self._next_head
        return self._head

    def ack_reply(self, agent_name: str, inbound_event_id: str, *, started_at: str, finished_at: str):
        del agent_name, inbound_event_id, started_at, finished_at
        self._acked = True
        return self._head

    def rebuild_mailbox_summary(self, agent_name: str, *, updated_at: str):
        del agent_name, updated_at
        return self._next_head


def _service(*, mailbox_store, inbound_store, attempt_store, message_store, reply_store, mailbox_kernel):
    return SimpleNamespace(
        _known_mailboxes={'agent1'},
        _config=SimpleNamespace(agents={'agent1': object()}),
        _clock=lambda: '2026-04-05T00:00:00Z',
        _mailbox_store=mailbox_store,
        _inbound_store=inbound_store,
        _attempt_store=attempt_store,
        _message_store=message_store,
        _reply_store=reply_store,
        _mailbox_kernel=mailbox_kernel,
    )


def test_agent_queue_derives_delivering_state_without_mailbox_record() -> None:
    event = InboundEventRecord(
        inbound_event_id='iev_1',
        agent_name='agent1',
        event_type=InboundEventType.TASK_REQUEST,
        message_id='msg_1',
        attempt_id='att_1',
        payload_ref=None,
        priority=10,
        status=InboundEventStatus.DELIVERING,
        created_at='2026-04-05T00:00:00Z',
    )
    attempt = AttemptRecord(
        attempt_id='att_1',
        message_id='msg_1',
        agent_name='agent1',
        provider='codex',
        job_id='job_1',
        retry_index=0,
        health_snapshot_ref=None,
        started_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        attempt_state=AttemptState.RUNNING,
    )
    message = MessageRecord(
        message_id='msg_1',
        origin_message_id=None,
        from_actor='user',
        target_scope='single',
        target_agents=('agent1',),
        created_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        message_state=MessageState.RUNNING,
    )
    service = _service(
        mailbox_store=_MailboxStore({'agent1': None}),
        inbound_store=_InboundStore({'agent1': [event]}),
        attempt_store=_SingleStore({'att_1': attempt}),
        message_store=_SingleStore({'msg_1': message}),
        reply_store=_SingleStore({}, list_records={'msg_1': []}),
        mailbox_kernel=_MailboxKernel(event),
    )

    payload = agent_queue(service, 'agent1')

    assert payload['mailbox_state'] == MailboxState.DELIVERING.value
    assert payload['active_inbound_event_id'] == 'iev_1'
    assert payload['queue_depth'] == 1


def test_queue_summary_ignores_stale_cmd_residue() -> None:
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': None,
                'cmd': SimpleNamespace(
                    mailbox_id='mbx_cmd',
                    agent_name='cmd',
                    active_inbound_event_id=None,
                    queue_depth=1,
                    pending_reply_count=0,
                    last_inbound_started_at=None,
                    last_inbound_finished_at=None,
                    mailbox_state=MailboxState.BLOCKED,
                    lease_version=3,
                    updated_at='2026-04-05T00:00:00Z',
                ),
            }
        ),
        inbound_store=_InboundStore(
            {
                'agent1': [],
                'cmd': [],
            }
        ),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=_MailboxKernel(None),
    )
    service._known_mailboxes = {'agent1'}

    payload = queue_summary(service, 'all')

    assert payload['agent_count'] == 1
    assert {item['agent_name'] for item in payload['agents']} == {'agent1'}
    assert payload['total_queue_depth'] == 0


def test_queue_summary_all_uses_mailbox_summary_without_scanning_inbox_history() -> None:
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': _mailbox_record(
                    mailbox_id='mbx_agent1',
                    agent_name='agent1',
                    active_inbound_event_id='iev_1',
                    queue_depth=2,
                    pending_reply_count=1,
                    head_inbound_event_id='iev_1',
                    head_event_type='task_reply',
                    head_status='delivering',
                    head_message_id='msg_1',
                    head_attempt_id='att_1',
                    head_payload_ref='reply:rep_1',
                    last_inbound_started_at='2026-04-05T00:00:00Z',
                    last_inbound_finished_at='2026-04-05T00:01:00Z',
                    mailbox_state=MailboxState.DELIVERING,
                    lease_version=7,
                    updated_at='2026-04-05T00:01:00Z',
                ),
            }
        ),
        inbound_store=_FailingInboundStore({'agent1': []}),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=_MailboxKernel(None),
    )

    payload = queue_summary(service, 'all')

    assert payload['agent_count'] == 1
    assert payload['queued_agent_count'] == 1
    assert payload['active_agent_count'] == 1
    assert payload['total_queue_depth'] == 2
    assert payload['total_pending_reply_count'] == 1
    assert payload['agents'][0]['agent_name'] == 'agent1'
    assert payload['agents'][0]['queue_depth'] == 2
    assert payload['agents'][0]['pending_reply_count'] == 1
    assert payload['agents'][0]['active_inbound_event_id'] == 'iev_1'


def test_queue_summary_all_ignores_non_configured_mailbox_even_if_summary_exists() -> None:
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': None,
                'agent2': _mailbox_record(
                    mailbox_id='mbx_agent2',
                    agent_name='agent2',
                    active_inbound_event_id='iev_2',
                    queue_depth=1,
                    pending_reply_count=0,
                    head_inbound_event_id='iev_2',
                    head_event_type='task_request',
                    head_status='delivering',
                    head_message_id='msg_2',
                    head_attempt_id='att_2',
                    head_payload_ref='job:job_2',
                    last_inbound_started_at='2026-04-05T00:00:00Z',
                    last_inbound_finished_at=None,
                    mailbox_state=MailboxState.DELIVERING,
                    lease_version=1,
                    updated_at='2026-04-05T00:00:00Z',
                ),
            }
        ),
        inbound_store=_FailingInboundStore({'agent1': [], 'agent2': []}),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=_MailboxKernel(None),
    )
    service._known_mailboxes = {'agent1'}

    payload = queue_summary(service, 'all')

    assert payload['agent_count'] == 1
    assert {item['agent_name'] for item in payload['agents']} == {'agent1'}


def test_agent_queue_uses_mailbox_summary_for_header_facts() -> None:
    active = InboundEventRecord(
        inbound_event_id='iev_1',
        agent_name='agent1',
        event_type=InboundEventType.TASK_REPLY,
        message_id='msg_1',
        attempt_id='att_1',
        payload_ref=compose_reply_payload('rep_1'),
        priority=10,
        status=InboundEventStatus.DELIVERING,
        created_at='2026-04-05T00:00:00Z',
        started_at='2026-04-05T00:00:10Z',
    )
    attempt = AttemptRecord(
        attempt_id='att_1',
        message_id='msg_1',
        agent_name='agent1',
        provider='codex',
        job_id='job_1',
        retry_index=0,
        health_snapshot_ref=None,
        started_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        attempt_state=AttemptState.REPLY_READY,
    )
    message = MessageRecord(
        message_id='msg_1',
        origin_message_id=None,
        from_actor='agent2',
        target_scope='single',
        target_agents=('agent1',),
        created_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        message_state=MessageState.COMPLETED,
    )
    reply = ReplyRecord(
        reply_id='rep_1',
        message_id='msg_1',
        attempt_id='att_1',
        agent_name='agent2',
        terminal_status=ReplyTerminalStatus.COMPLETED,
        reply='done',
        diagnostics={},
        finished_at='2026-04-05T00:00:40Z',
    )
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': _mailbox_record(
                    mailbox_id='mbx_agent1',
                    agent_name='agent1',
                    active_inbound_event_id='iev_1',
                    queue_depth=2,
                    pending_reply_count=1,
                    head_inbound_event_id='iev_1',
                    head_event_type='task_reply',
                    head_status='delivering',
                    head_message_id='msg_1',
                    head_attempt_id='att_1',
                    head_payload_ref='reply:rep_1',
                    last_inbound_started_at='2026-04-05T00:00:10Z',
                    last_inbound_finished_at='2026-04-05T00:00:40Z',
                    mailbox_state=MailboxState.DELIVERING,
                    lease_version=7,
                    updated_at='2026-04-05T00:01:00Z',
                ),
            }
        ),
        inbound_store=_HeaderOnlyInboundStore(
            {'agent1': []},
            latest_by_id={('agent1', 'iev_1'): active},
        ),
        attempt_store=_SingleStore({'att_1': attempt}),
        message_store=_SingleStore({'msg_1': message}),
        reply_store=_SingleStore({'rep_1': reply}, list_records={'msg_1': [reply]}),
        mailbox_kernel=_MailboxKernel(active),
    )

    payload = agent_queue(service, 'agent1')

    assert payload['mailbox_state'] == MailboxState.DELIVERING.value
    assert payload['queue_depth'] == 2
    assert payload['pending_reply_count'] == 1
    assert payload['active_inbound_event_id'] == 'iev_1'
    assert payload['active']['inbound_event_id'] == 'iev_1'
    assert payload['active']['reply_id'] == 'rep_1'
    assert payload['queued_events'] == []


def test_queue_summary_single_agent_detail_false_uses_summary_only() -> None:
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': _mailbox_record(
                    mailbox_id='mbx_agent1',
                    agent_name='agent1',
                    active_inbound_event_id='iev_1',
                    queue_depth=2,
                    pending_reply_count=1,
                    head_inbound_event_id='iev_1',
                    head_event_type='task_reply',
                    head_status='queued',
                    head_message_id='msg_1',
                    head_attempt_id='att_1',
                    head_payload_ref='reply:rep_1',
                    last_inbound_started_at='2026-04-05T00:00:00Z',
                    last_inbound_finished_at='2026-04-05T00:00:40Z',
                    mailbox_state=MailboxState.BLOCKED,
                    lease_version=3,
                    updated_at='2026-04-05T00:01:00Z',
                ),
            }
        ),
        inbound_store=_FailingInboundStore({'agent1': []}),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=_MailboxKernel(None),
    )

    payload = queue_summary(service, 'agent1', detail=False)

    assert payload['target'] == 'agent1'
    assert payload['agent']['summary_status'] == 'ok'
    assert payload['agent']['queue_depth'] == 2
    assert payload['agent']['pending_reply_count'] == 1
    assert 'queued_events' not in payload['agent']
    assert 'active' not in payload['agent']


def test_queue_summary_single_agent_default_uses_summary_only() -> None:
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': _mailbox_record(
                    mailbox_id='mbx_agent1',
                    agent_name='agent1',
                    active_inbound_event_id='iev_1',
                    queue_depth=2,
                    pending_reply_count=1,
                    head_inbound_event_id='iev_1',
                    head_event_type='task_reply',
                    head_status='queued',
                    head_message_id='msg_1',
                    head_attempt_id='att_1',
                    head_payload_ref='reply:rep_1',
                    last_inbound_started_at='2026-04-05T00:00:00Z',
                    last_inbound_finished_at='2026-04-05T00:00:40Z',
                    mailbox_state=MailboxState.BLOCKED,
                    lease_version=3,
                    updated_at='2026-04-05T00:01:00Z',
                ),
            }
        ),
        inbound_store=_FailingInboundStore({'agent1': []}),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=_MailboxKernel(None),
    )

    payload = queue_summary(service, 'agent1')

    assert payload['target'] == 'agent1'
    assert payload['agent']['summary_status'] == 'ok'
    assert payload['agent']['queue_depth'] == 2
    assert payload['agent']['pending_reply_count'] == 1
    assert 'queued_events' not in payload['agent']
    assert 'active' not in payload['agent']


def test_queue_summary_preserves_mailbox_state_authority_from_summary_record() -> None:
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': _mailbox_record(
                    mailbox_id='mbx_agent1',
                    agent_name='agent1',
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
                    mailbox_state=MailboxState.DEGRADED,
                    lease_version=3,
                    updated_at='2026-04-05T00:01:00Z',
                ),
            }
        ),
        inbound_store=_FailingInboundStore({'agent1': []}),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=_MailboxKernel(None),
    )

    payload = queue_summary(service, 'agent1')

    assert payload['agent']['summary_status'] == 'ok'
    assert payload['agent']['mailbox_state'] == MailboxState.DEGRADED.value


def test_inbox_uses_mailbox_summary_for_agent_header() -> None:
    reply_event = InboundEventRecord(
        inbound_event_id='iev_1',
        agent_name='agent1',
        event_type=InboundEventType.TASK_REPLY,
        message_id='msg_1',
        attempt_id='att_1',
        payload_ref=compose_reply_payload('rep_1'),
        priority=10,
        status=InboundEventStatus.QUEUED,
        created_at='2026-04-05T00:00:00Z',
    )
    attempt = AttemptRecord(
        attempt_id='att_1',
        message_id='msg_1',
        agent_name='agent1',
        provider='codex',
        job_id='job_1',
        retry_index=0,
        health_snapshot_ref=None,
        started_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        attempt_state=AttemptState.REPLY_READY,
    )
    message = MessageRecord(
        message_id='msg_1',
        origin_message_id=None,
        from_actor='agent2',
        target_scope='single',
        target_agents=('agent1',),
        created_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        message_state=MessageState.COMPLETED,
    )
    reply = ReplyRecord(
        reply_id='rep_1',
        message_id='msg_1',
        attempt_id='att_1',
        agent_name='agent2',
        terminal_status=ReplyTerminalStatus.COMPLETED,
        reply='done',
        diagnostics={},
        finished_at='2026-04-05T00:00:40Z',
    )
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': _mailbox_record(
                    mailbox_id='mbx_agent1',
                    agent_name='agent1',
                    active_inbound_event_id=None,
                    queue_depth=1,
                    pending_reply_count=1,
                    head_inbound_event_id='iev_1',
                    head_event_type='task_reply',
                    head_status='queued',
                    head_message_id='msg_1',
                    head_attempt_id='att_1',
                    head_payload_ref='reply:rep_1',
                    last_inbound_started_at='2026-04-05T00:00:00Z',
                    last_inbound_finished_at='2026-04-05T00:00:40Z',
                    mailbox_state=MailboxState.BLOCKED,
                    lease_version=3,
                    updated_at='2026-04-05T00:01:00Z',
                ),
            }
        ),
        inbound_store=_InboundStore({'agent1': [reply_event]}),
        attempt_store=_SingleStore({'att_1': attempt}),
        message_store=_SingleStore({'msg_1': message}),
        reply_store=_SingleStore({'rep_1': reply}, list_records={'msg_1': [reply]}),
        mailbox_kernel=_MailboxKernel(reply_event),
    )

    payload = inbox(service, 'agent1')

    assert payload['summary_status'] == 'ok'
    assert payload['agent']['queue_depth'] == 1
    assert payload['agent']['pending_reply_count'] == 1
    assert payload['agent']['mailbox_state'] == MailboxState.BLOCKED.value
    assert payload['agent']['active_inbound_event_id'] is None
    assert payload['item_count'] == 1
    assert payload['head']['reply_id'] == 'rep_1'
    assert payload['items'] == []


def test_inbox_detail_true_expands_history_items() -> None:
    attempt = AttemptRecord(
        attempt_id='att_1',
        message_id='msg_1',
        agent_name='agent1',
        provider='codex',
        job_id='job_1',
        retry_index=0,
        health_snapshot_ref=None,
        started_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        attempt_state=AttemptState.REPLY_READY,
    )
    reply = ReplyRecord(
        reply_id='rep_1',
        message_id='msg_1',
        attempt_id='att_1',
        agent_name='agent2',
        terminal_status=ReplyTerminalStatus.COMPLETED,
        reply='done',
        diagnostics={},
        finished_at='2026-04-05T00:00:40Z',
    )
    message = MessageRecord(
        message_id='msg_1',
        origin_message_id=None,
        from_actor='agent2',
        target_scope='single',
        target_agents=('agent1',),
        created_at='2026-04-05T00:00:05Z',
        updated_at='2026-04-05T00:00:40Z',
        message_state=MessageState.COMPLETED,
    )
    reply_event = InboundEventRecord(
        inbound_event_id='iev_1',
        agent_name='agent1',
        event_type=InboundEventType.TASK_REPLY,
        message_id='msg_1',
        attempt_id='att_1',
        payload_ref='reply:rep_1',
        priority=10,
        status=InboundEventStatus.QUEUED,
        created_at='2026-04-05T00:00:05Z',
        started_at='2026-04-05T00:00:10Z',
        finished_at='2026-04-05T00:00:40Z',
    )
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': _mailbox_record(
                    mailbox_id='mbx_agent1',
                    agent_name='agent1',
                    active_inbound_event_id=None,
                    queue_depth=1,
                    pending_reply_count=1,
                    head_inbound_event_id='iev_1',
                    head_event_type='task_reply',
                    head_status='queued',
                    head_message_id='msg_1',
                    head_attempt_id='att_1',
                    head_payload_ref='reply:rep_1',
                    last_inbound_started_at='2026-04-05T00:00:10Z',
                    last_inbound_finished_at='2026-04-05T00:00:40Z',
                    mailbox_state=MailboxState.BLOCKED,
                    lease_version=3,
                    updated_at='2026-04-05T00:01:00Z',
                ),
            }
        ),
        inbound_store=_InboundStore({'agent1': [reply_event]}),
        attempt_store=_SingleStore({'att_1': attempt}),
        message_store=_SingleStore({'msg_1': message}),
        reply_store=_SingleStore({'rep_1': reply}, list_records={'msg_1': [reply]}),
        mailbox_kernel=_MailboxKernel(reply_event),
    )

    payload = inbox(service, 'agent1', detail=True)

    assert payload['summary_status'] == 'ok'
    assert payload['item_count'] == 1
    assert payload['items'][0]['reply_id'] == 'rep_1'


def test_inbox_detail_false_uses_summary_head_without_items() -> None:
    attempt = AttemptRecord(
        attempt_id='att_1',
        message_id='msg_1',
        agent_name='agent1',
        provider='codex',
        job_id='job_1',
        retry_index=0,
        health_snapshot_ref=None,
        started_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        attempt_state=AttemptState.REPLY_READY,
    )
    reply = ReplyRecord(
        reply_id='rep_1',
        message_id='msg_1',
        attempt_id='att_1',
        agent_name='agent2',
        terminal_status=ReplyTerminalStatus.COMPLETED,
        reply='done',
        diagnostics={},
        finished_at='2026-04-05T00:00:40Z',
    )
    service = _service(
        mailbox_store=_MailboxStore(
            {
                'agent1': _mailbox_record(
                    mailbox_id='mbx_agent1',
                    agent_name='agent1',
                    active_inbound_event_id=None,
                    queue_depth=1,
                    pending_reply_count=1,
                    head_inbound_event_id='iev_1',
                    head_event_type='task_reply',
                    head_status='queued',
                    head_message_id='msg_1',
                    head_attempt_id='att_1',
                    head_payload_ref='reply:rep_1',
                    last_inbound_started_at='2026-04-05T00:00:00Z',
                    last_inbound_finished_at='2026-04-05T00:00:40Z',
                    mailbox_state=MailboxState.BLOCKED,
                    lease_version=3,
                    updated_at='2026-04-05T00:01:00Z',
                ),
            }
        ),
        inbound_store=_FailingInboundStore({'agent1': []}),
        attempt_store=_SingleStore({'att_1': attempt}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({'rep_1': reply}, list_records={'msg_1': [reply]}),
        mailbox_kernel=_MailboxKernel(None),
    )

    payload = inbox(service, 'agent1', detail=False)

    assert payload['target'] == 'agent1'
    assert payload['summary_status'] == 'ok'
    assert payload['item_count'] == 1
    assert payload['head']['reply_id'] == 'rep_1'
    assert payload['items'] == []


def test_queue_summary_missing_summary_surfaces_degraded_state() -> None:
    service = _service(
        mailbox_store=_MailboxStore({'agent1': None}),
        inbound_store=_FailingInboundStore({'agent1': []}),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=SimpleNamespace(),
    )

    payload = queue_summary(service, 'agent1')

    assert payload['target'] == 'agent1'
    assert payload['agent']['summary_status'] == 'missing'
    assert payload['agent']['mailbox_state'] is None
    assert payload['agent']['queue_depth'] == 0
    assert payload['agent']['pending_reply_count'] == 0


def test_mailbox_head_missing_summary_surfaces_degraded_state() -> None:
    service = _service(
        mailbox_store=_MailboxStore({'agent1': None}),
        inbound_store=_FailingInboundStore({'agent1': []}),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=SimpleNamespace(),
    )

    payload = mailbox_head(service, 'agent1')

    assert payload['target'] == 'agent1'
    assert payload['summary_status'] == 'missing'
    assert payload['head'] is None


def test_queue_summary_summary_load_error_surfaces_degraded_state() -> None:
    class _BrokenMailboxStore(_MailboxStore):
        def load(self, agent_name: str):
            raise ValueError(f'{agent_name}: broken summary')

    service = _service(
        mailbox_store=_BrokenMailboxStore({'agent1': None}),
        inbound_store=_FailingInboundStore({'agent1': []}),
        attempt_store=_SingleStore({}),
        message_store=_SingleStore({}),
        reply_store=_SingleStore({}),
        mailbox_kernel=SimpleNamespace(),
    )

    payload = queue_summary(service, 'agent1')

    assert payload['agent']['summary_status'] == 'error'
    assert payload['agent']['summary_error'] == 'agent1: broken summary'


def test_ack_reply_returns_reply_metadata_and_next_head() -> None:
    head = InboundEventRecord(
        inbound_event_id='iev_reply_1',
        agent_name='agent1',
        event_type=InboundEventType.TASK_REPLY,
        message_id='msg_1',
        attempt_id='att_1',
        payload_ref=compose_reply_payload('rep_1'),
        priority=5,
        status=InboundEventStatus.DELIVERING,
        created_at='2026-04-05T00:00:00Z',
    )
    next_head = InboundEventRecord(
        inbound_event_id='iev_reply_2',
        agent_name='agent1',
        event_type=InboundEventType.TASK_REQUEST,
        message_id='msg_2',
        attempt_id='att_2',
        payload_ref=None,
        priority=6,
        status=InboundEventStatus.QUEUED,
        created_at='2026-04-05T00:01:00Z',
    )
    attempt = AttemptRecord(
        attempt_id='att_1',
        message_id='msg_1',
        agent_name='agent1',
        provider='codex',
        job_id='job_1',
        retry_index=0,
        health_snapshot_ref=None,
        started_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        attempt_state=AttemptState.REPLY_READY,
    )
    message = MessageRecord(
        message_id='msg_1',
        origin_message_id=None,
        from_actor='agent2',
        target_scope='single',
        target_agents=('agent1',),
        created_at='2026-04-05T00:00:00Z',
        updated_at='2026-04-05T00:00:00Z',
        message_state=MessageState.COMPLETED,
    )
    reply = ReplyRecord(
        reply_id='rep_1',
        message_id='msg_1',
        attempt_id='att_1',
        agent_name='agent2',
        terminal_status=ReplyTerminalStatus.COMPLETED,
        reply='done',
        diagnostics={'notice': True, 'notice_kind': 'heartbeat', 'last_progress_at': '2026-04-05T00:00:30Z'},
        finished_at='2026-04-05T00:00:40Z',
    )
    service = _service(
        mailbox_store=_MailboxStore({'agent1': _mailbox_record(
            mailbox_id='mbx_agent1',
            agent_name='agent1',
            active_inbound_event_id='iev_reply_2',
            queue_depth=1,
            pending_reply_count=0,
            head_inbound_event_id='iev_reply_2',
            head_event_type='task_request',
            head_status='queued',
            head_message_id='msg_2',
            head_attempt_id='att_2',
            head_payload_ref=None,
            last_inbound_started_at='2026-04-05T00:01:00Z',
            last_inbound_finished_at=None,
            mailbox_state=MailboxState.DELIVERING,
            lease_version=4,
            updated_at='2026-04-05T00:01:00Z',
        )}),
        inbound_store=_InboundStore({'agent1': [head, next_head]}),
        attempt_store=_SingleStore({'att_1': attempt, 'att_2': attempt}),
        message_store=_SingleStore({'msg_1': message, 'msg_2': message}),
        reply_store=_SingleStore({'rep_1': reply}, list_records={'msg_2': [], 'msg_1': [reply]}),
        mailbox_kernel=_MailboxKernel(head, next_head=next_head),
    )

    payload = ack_reply(service, 'agent1')

    assert payload['acknowledged_inbound_event_id'] == 'iev_reply_1'
    assert payload['job_id'] == 'job_1'
    assert payload['reply_id'] == 'rep_1'
    assert payload['reply_notice_kind'] == 'heartbeat'
    assert payload['next_inbound_event_id'] == 'iev_reply_2'
    assert payload['reply'] == 'done'
