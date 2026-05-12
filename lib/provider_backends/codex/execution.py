from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ccbd.api_models import JobRecord
from provider_core.protocol import request_anchor_for_job, wrap_codex_turn_prompt
from provider_execution.base import ProviderPollResult, ProviderRuntimeContext, ProviderSubmission
from provider_execution.common import request_anchor_from_runtime_state
from provider_execution.reliability import CompletionReliabilityPolicy
from terminal_runtime import get_backend_for_session

from .comm import CodexLogReader
from .execution_runtime import poll_submission as _poll_submission
from .execution_runtime import resume_submission as _resume_submission
from .execution_runtime import start_active_submission as _start_active_submission
from .session import load_project_session
from .session_runtime.follow_policy import codex_session_root_path, should_follow_workspace_sessions


class CodexProviderAdapter:
    provider = 'codex'
    completion_reliability_policy = CompletionReliabilityPolicy(
        provider='codex',
        primary_authority='protocol_log',
        no_terminal_timeout_s=900.0,
    )

    def start(self, job: JobRecord, *, context: ProviderRuntimeContext | None, now: str) -> ProviderSubmission:
        return _start_active_submission(
            self,
            job,
            context=context,
            now=now,
            load_session_fn=_load_session,
            backend_for_session_fn=get_backend_for_session,
            reader_factory=_reader_factory,
            request_anchor_fn=request_anchor_for_job,
            wrap_prompt_fn=wrap_codex_turn_prompt,
        )

    def poll(self, submission: ProviderSubmission, *, now: str) -> ProviderPollResult | None:
        submission = _refresh_reader_for_current_session_binding(submission)
        return _poll_submission(submission, now=now)

    def export_runtime_state(self, submission: ProviderSubmission) -> dict[str, object]:
        return {
            'mode': submission.runtime_state.get('mode'),
            'state': submission.runtime_state.get('state') or {},
            'pane_id': submission.runtime_state.get('pane_id'),
            'request_anchor': request_anchor_from_runtime_state(submission.runtime_state, fallback=submission.job_id),
            'next_seq': submission.runtime_state.get('next_seq'),
            'anchor_seen': submission.runtime_state.get('anchor_seen'),
            'no_wrap': submission.runtime_state.get('no_wrap'),
            'bound_turn_id': submission.runtime_state.get('bound_turn_id'),
            'bound_task_id': submission.runtime_state.get('bound_task_id'),
            'reply_buffer': submission.runtime_state.get('reply_buffer'),
            'last_agent_message': submission.runtime_state.get('last_agent_message'),
            'last_final_answer': submission.runtime_state.get('last_final_answer'),
            'last_assistant_message': submission.runtime_state.get('last_assistant_message'),
            'last_assistant_signature': submission.runtime_state.get('last_assistant_signature'),
            'session_path': submission.runtime_state.get('session_path'),
            'workspace_path': submission.runtime_state.get('workspace_path'),
        }

    def resume(
        self,
        job: JobRecord,
        submission: ProviderSubmission,
        *,
        context: ProviderRuntimeContext | None,
        persisted_state,
        now: str,
    ) -> ProviderSubmission | None:
        del persisted_state, now
        return _resume_submission(
            job,
            submission,
            context=context,
            load_session_fn=_load_session,
            backend_for_session_fn=get_backend_for_session,
            reader_factory=_reader_factory,
        )


def _reader_factory(session, preferred_log: Path | None):
    work_dir = Path(session.work_dir)
    session_id_filter = session.codex_session_id or None
    follow_workspace = should_follow_workspace_sessions(
        work_dir=work_dir,
        session_file=getattr(session, "session_file", None),
        session_data=getattr(session, "data", None),
    )
    log_path = preferred_log if preferred_log is not None else (
        Path(session.codex_session_path).expanduser() if session.codex_session_path else None
    )
    if preferred_log is None:
        session_id_filter = None
        follow_workspace = True
    kwargs: dict[str, object] = {
        "log_path": log_path,
        "session_id_filter": session_id_filter,
        "work_dir": work_dir,
        "follow_workspace_sessions": follow_workspace,
    }
    session_root = codex_session_root_path(getattr(session, "data", None))
    if session_root is not None:
        kwargs["root"] = session_root
    return CodexLogReader(**kwargs)


def _refresh_reader_for_current_session_binding(submission: ProviderSubmission) -> ProviderSubmission:
    state = dict(submission.runtime_state)
    if str(state.get('mode') or '').strip().lower() != 'active':
        return submission
    work_dir = _submission_work_dir(submission, state)
    if work_dir is None:
        return submission
    session = _load_session(work_dir, submission.agent_name)
    if session is None:
        return submission
    current_log = _current_session_log(session)
    if current_log is None or not current_log.exists():
        return submission

    current_log_str = _normalized_path_string(current_log)
    poll_state = dict(state.get('state') or {})
    poll_state_log_str = _normalized_path_string(poll_state.get('log_path'))
    reader = state.get('reader')
    reader_log_str = _normalized_path_string(getattr(reader, '_preferred_log', None))
    reader_filter = str(getattr(reader, '_session_id_filter', '') or '').strip()
    current_filter = str(getattr(session, 'codex_session_id', '') or '').strip()

    if (
        current_log_str == poll_state_log_str
        and current_log_str == reader_log_str
        and (not current_filter or current_filter == reader_filter)
    ):
        return submission

    updated_state = {
        **state,
        'reader': _reader_factory(session, current_log),
        'workspace_path': str(work_dir),
    }
    if current_log_str != poll_state_log_str:
        updated_state['state'] = {
            **poll_state,
            'log_path': current_log,
            'offset': 0,
            'last_rescan': 0.0,
        }
    return replace(submission, runtime_state=updated_state)


def _submission_work_dir(submission: ProviderSubmission, state: dict[str, object]) -> Path | None:
    diagnostics = submission.diagnostics if isinstance(submission.diagnostics, dict) else {}
    raw = state.get('workspace_path') or diagnostics.get('workspace_path')
    if not raw:
        return None
    try:
        return Path(str(raw)).expanduser()
    except Exception:
        return None


def _current_session_log(session) -> Path | None:
    raw = str(getattr(session, 'codex_session_path', '') or '').strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except Exception:
        return None


def _normalized_path_string(value: object) -> str:
    if value is None:
        return ''
    try:
        return str(Path(value).expanduser())
    except Exception:
        return str(value or '').strip()


def _load_session(work_dir: Path, agent_name: str):
    from .execution_runtime.start import load_session as _runtime_load_session

    return _runtime_load_session(load_project_session, work_dir, agent_name=agent_name)


def build_execution_adapter() -> CodexProviderAdapter:
    return CodexProviderAdapter()


__all__ = ['CodexProviderAdapter', 'build_execution_adapter']
