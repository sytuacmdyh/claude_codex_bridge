from __future__ import annotations

import json

from ccbd.api_models import RpcRequest, RpcResponse

_REQUEST_READ_TIMEOUT_S = 0.5
_MAX_REQUEST_BYTES = 1024 * 1024


def handle_connection(server, conn) -> str | None:
    request = None
    after_response_action = None
    try:
        conn.settimeout(_REQUEST_READ_TIMEOUT_S)
        raw = _recv_request_line(conn)
        if not raw:
            return None
        message = json.loads(raw.split(b'\n', 1)[0].decode('utf-8'))
        request = RpcRequest.from_record(message)
        handler = server._handlers.get(request.op)
        if handler is None:
            response = RpcResponse.failure(f'unknown op: {request.op}')
        else:
            guard = getattr(server, '_request_guard', None)
            rejection = guard(request.op) if guard is not None else None
            if rejection:
                response = RpcResponse.failure(rejection)
            else:
                payload = handler(request.request)
                if isinstance(payload, tuple) and len(payload) == 2:
                    payload, after_response_action = payload
                response = RpcResponse.success(payload)
    except Exception as exc:
        response = RpcResponse.failure(str(exc))
    try:
        conn.sendall((json.dumps(response.to_record(), ensure_ascii=False) + '\n').encode('utf-8'))
    except OSError:
        return getattr(request, 'op', None)
    if after_response_action is not None:
        try:
            server.queue_after_response_action(after_response_action)
        except Exception:
            pass
    return getattr(request, 'op', None)


def _recv_request_line(conn) -> bytes:
    raw = b''
    while b'\n' not in raw:
        chunk = conn.recv(65536)
        if not chunk:
            break
        raw += chunk
        if len(raw) > _MAX_REQUEST_BYTES:
            raise ValueError('ccbd request exceeds maximum size')
    return raw


__all__ = ['handle_connection']
