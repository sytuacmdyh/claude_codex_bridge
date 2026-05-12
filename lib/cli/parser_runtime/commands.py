from __future__ import annotations

import argparse

from cli.models import (
    ParsedAckCommand,
    ParsedCancelCommand,
    ParsedCleanupCommand,
    ParsedConfigValidateCommand,
    ParsedDoctorCommand,
    ParsedInboxCommand,
    ParsedKillCommand,
    ParsedLogsCommand,
    ParsedPendCommand,
    ParsedPingCommand,
    ParsedPsCommand,
    ParsedQueueCommand,
    ParsedResubmitCommand,
    ParsedRetryCommand,
    ParsedTraceCommand,
    ParsedWaitCommand,
    ParsedWatchCommand,
)

from .common import parse_args, require_no_extra
from .constants import WAIT_COMMAND_TO_MODE


def parse_cancel(tokens: list[str], *, project: str | None, error_type) -> ParsedCancelCommand:
    if len(tokens) != 1:
        raise error_type('cancel requires <job_id>')
    return ParsedCancelCommand(project=project, job_id=tokens[0])


def parse_kill(tokens: list[str], *, project: str | None, error_type) -> ParsedKillCommand:
    parser = argparse.ArgumentParser(prog='ccb kill', add_help=False)
    parser.add_argument('-f', '--force', action='store_true')
    namespace = parse_args(parser, tokens, error_message='invalid kill command', error_type=error_type)
    return ParsedKillCommand(project=project, force=bool(namespace.force))


def parse_cleanup(tokens: list[str], *, project: str | None, error_type) -> ParsedCleanupCommand:
    require_no_extra(tokens, command='cleanup', error_type=error_type)
    return ParsedCleanupCommand(project=project)


def parse_ps(tokens: list[str], *, project: str | None, error_type) -> ParsedPsCommand:
    require_no_extra(tokens, command='ps', error_type=error_type)
    return ParsedPsCommand(project=project)


def parse_ping(tokens: list[str], *, project: str | None, error_type) -> ParsedPingCommand:
    if len(tokens) != 1:
        raise error_type('ping requires <agent_name|all>')
    return ParsedPingCommand(project=project, target=tokens[0])


def parse_watch(tokens: list[str], *, project: str | None, error_type) -> ParsedWatchCommand:
    if len(tokens) != 1:
        raise error_type('watch requires <agent_name|job_id>')
    return ParsedWatchCommand(project=project, target=tokens[0])


def parse_pend(tokens: list[str], *, project: str | None, error_type) -> ParsedPendCommand:
    parser = argparse.ArgumentParser(prog='ccb pend', add_help=False)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--inbox', action='store_true')
    parser.add_argument('--queue', action='store_true')
    parser.add_argument('--detail', action='store_true')
    parser.add_argument('target')
    parser.add_argument('count', nargs='?')
    namespace = parse_args(parser, tokens, error_message='invalid pend command', error_type=error_type)
    selected_modes = [name for name in ('watch', 'inbox', 'queue') if bool(getattr(namespace, name))]
    if len(selected_modes) > 1:
        raise error_type('pend supports at most one observer mode: --watch, --inbox, or --queue')
    observer_mode = 'snapshot'
    if namespace.watch:
        observer_mode = 'watch'
    elif namespace.inbox:
        observer_mode = 'inbox'
    elif namespace.queue:
        observer_mode = 'queue'
    if namespace.detail and observer_mode not in {'inbox', 'queue'}:
        raise error_type('pend --detail requires --inbox or --queue')
    count: int | None = None
    if namespace.count is not None:
        try:
            count = int(namespace.count)
        except ValueError as exc:
            raise error_type('pend count must be an integer') from exc
        if count <= 0:
            raise error_type('pend count must be positive')
    if count is not None and observer_mode != 'snapshot':
        raise error_type('pend count is only supported for snapshot mode')
    return ParsedPendCommand(
        project=project,
        target=str(namespace.target),
        count=count,
        observer_mode=observer_mode,
        detail=bool(namespace.detail),
    )


def parse_queue(tokens: list[str], *, project: str | None, error_type) -> ParsedQueueCommand:
    parser = argparse.ArgumentParser(prog='ccb queue', add_help=False)
    parser.add_argument('--detail', action='store_true')
    parser.add_argument('target')
    namespace = parse_args(parser, tokens, error_message='invalid queue command', error_type=error_type)
    return ParsedQueueCommand(project=project, target=str(namespace.target), detail=bool(namespace.detail))


def parse_trace(tokens: list[str], *, project: str | None, error_type) -> ParsedTraceCommand:
    if len(tokens) != 1:
        raise error_type('trace requires <submission_id|message_id|attempt_id|reply_id|job_id>')
    return ParsedTraceCommand(project=project, target=tokens[0])


def parse_resubmit(tokens: list[str], *, project: str | None, error_type) -> ParsedResubmitCommand:
    if len(tokens) != 1:
        raise error_type('resubmit requires <message_id>')
    return ParsedResubmitCommand(project=project, message_id=tokens[0])


def parse_repair(tokens: list[str], *, project: str | None, error_type):
    if not tokens:
        raise error_type('repair requires one of: ack, retry, resubmit')
    mode = tokens[0]
    rest = tokens[1:]
    if mode == 'ack':
        return parse_ack(rest, project=project, error_type=error_type)
    if mode == 'retry':
        return parse_retry(rest, project=project, error_type=error_type)
    if mode == 'resubmit':
        return parse_resubmit(rest, project=project, error_type=error_type)
    raise error_type('repair only supports: ack, retry, resubmit')


def parse_retry(tokens: list[str], *, project: str | None, error_type) -> ParsedRetryCommand:
    if len(tokens) != 1:
        raise error_type('retry requires <job_id|attempt_id>')
    return ParsedRetryCommand(project=project, target=tokens[0])


def parse_wait(command_name: str, tokens: list[str], *, project: str | None, error_type) -> ParsedWaitCommand:
    parser = argparse.ArgumentParser(prog=f'ccb {command_name}', add_help=False)
    parser.add_argument('--timeout', type=float, default=None)
    if command_name == 'wait-quorum':
        parser.add_argument('quorum', type=int)
        parser.add_argument('target')
    else:
        parser.add_argument('target')
    namespace = parse_args(parser, tokens, error_message=f'invalid {command_name} command', error_type=error_type)
    timeout_s = float(namespace.timeout) if namespace.timeout is not None else None
    if timeout_s is not None and timeout_s <= 0:
        raise error_type('wait timeout must be positive')
    quorum = int(namespace.quorum) if getattr(namespace, 'quorum', None) is not None else None
    if quorum is not None and quorum <= 0:
        raise error_type('wait quorum must be positive')
    return ParsedWaitCommand(
        project=project,
        mode=WAIT_COMMAND_TO_MODE[command_name],
        target=str(namespace.target),
        quorum=quorum,
        timeout_s=timeout_s,
    )


def parse_inbox(tokens: list[str], *, project: str | None, error_type) -> ParsedInboxCommand:
    parser = argparse.ArgumentParser(prog='ccb inbox', add_help=False)
    parser.add_argument('--detail', action='store_true')
    parser.add_argument('agent_name')
    namespace = parse_args(parser, tokens, error_message='invalid inbox command', error_type=error_type)
    return ParsedInboxCommand(project=project, agent_name=str(namespace.agent_name), detail=bool(namespace.detail))


def parse_ack(tokens: list[str], *, project: str | None, error_type) -> ParsedAckCommand:
    if not tokens or len(tokens) > 2:
        raise error_type('ack requires <agent_name> [inbound_event_id]')
    inbound_event_id = tokens[1] if len(tokens) == 2 else None
    return ParsedAckCommand(project=project, agent_name=tokens[0], inbound_event_id=inbound_event_id)


def parse_logs(tokens: list[str], *, project: str | None, error_type) -> ParsedLogsCommand:
    if len(tokens) != 1:
        raise error_type('logs requires <agent_name>')
    return ParsedLogsCommand(project=project, agent_name=tokens[0])


def parse_doctor(tokens: list[str], *, project: str | None, error_type) -> ParsedDoctorCommand:
    if tokens[:1] in (['ps'], ['--runtime']):
        return parse_ps(tokens[1:], project=project, error_type=error_type)
    if tokens[:1] in (['logs'], ['--logs']):
        return parse_logs(tokens[1:], project=project, error_type=error_type)
    if tokens[:1] == ['storage']:
        parser = argparse.ArgumentParser(prog='ccb doctor storage', add_help=False)
        parser.add_argument('--json', dest='json_output', action='store_true')
        namespace = parse_args(parser, tokens[1:], error_message='invalid doctor storage command', error_type=error_type)
        return ParsedDoctorCommand(project=project, storage=True, json_output=bool(namespace.json_output))
    parser = argparse.ArgumentParser(prog='ccb doctor', add_help=False)
    parser.add_argument('--output', dest='output_path', nargs='?', const='', default=None)
    try:
        namespace = parse_args(parser, tokens, error_message='invalid doctor command', error_type=error_type)
    except Exception as exc:
        if '--bundle' in tokens:
            raise error_type('`doctor --bundle` is no longer supported; use `doctor --output`') from exc
        raise
    bundle = namespace.output_path is not None
    output_path = str(namespace.output_path) if namespace.output_path else None
    return ParsedDoctorCommand(project=project, bundle=bundle, output_path=output_path)


def parse_config(tokens: list[str], *, project: str | None, error_type) -> ParsedConfigValidateCommand:
    if tokens != ['validate']:
        raise error_type('config only supports: ccb config validate')
    return ParsedConfigValidateCommand(project=project)


__all__ = [
    'parse_ack',
    'parse_cancel',
    'parse_cleanup',
    'parse_config',
    'parse_doctor',
    'parse_inbox',
    'parse_kill',
    'parse_logs',
    'parse_pend',
    'parse_ping',
    'parse_ps',
    'parse_queue',
    'parse_repair',
    'parse_resubmit',
    'parse_retry',
    'parse_trace',
    'parse_wait',
    'parse_watch',
]
