from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from provider_profiles.codex_home_config import codex_provider_authority_fingerprint
from provider_backends.codex.launcher_runtime.session_paths import load_resume_session_id


def test_load_resume_session_id_prefers_session_field_then_start_cmd(tmp_path: Path) -> None:
    ccb_dir = tmp_path / ".ccb"
    agent_dir = ccb_dir / "agents" / "agent1" / "runtime"
    agent_dir.mkdir(parents=True, exist_ok=True)
    session_file = ccb_dir / ".codex-agent1-session"
    session_file.write_text(json.dumps({"codex_session_id": "sid-1"}), encoding="utf-8")

    spec = SimpleNamespace(name="agent1")

    assert load_resume_session_id(spec, agent_dir) == "sid-1"

    session_file.write_text(json.dumps({"start_cmd": "codex resume sid-2"}), encoding="utf-8")

    assert load_resume_session_id(spec, agent_dir) == "sid-2"


def test_load_resume_session_id_rejects_session_path_outside_bound_root(tmp_path: Path) -> None:
    ccb_dir = tmp_path / ".ccb"
    agent_dir = ccb_dir / "agents" / "agent1" / "runtime"
    agent_dir.mkdir(parents=True, exist_ok=True)
    managed_root = ccb_dir / "agents" / "agent1" / "provider-state" / "codex" / "home" / "sessions"
    legacy_path = ccb_dir / "provider-profiles" / "agent1" / "codex" / "sessions" / "2026" / "05" / "10" / "legacy.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"type":"session"}\n', encoding="utf-8")
    session_file = ccb_dir / ".codex-agent1-session"
    session_file.write_text(
        json.dumps(
            {
                "codex_session_id": "sid-legacy",
                "codex_session_root": str(managed_root),
                "codex_session_path": str(legacy_path),
                "start_cmd": "codex resume sid-legacy",
            }
        ),
        encoding="utf-8",
    )

    spec = SimpleNamespace(name="agent1")

    assert load_resume_session_id(spec, agent_dir) is None


def test_load_resume_session_id_skips_legacy_resume_when_explicit_provider_authority_is_new(tmp_path: Path) -> None:
    ccb_dir = tmp_path / '.ccb'
    agent_dir = ccb_dir / 'agents' / 'agent1' / 'runtime'
    agent_dir.mkdir(parents=True, exist_ok=True)
    session_file = ccb_dir / '.codex-agent1-session'
    session_file.write_text(json.dumps({'codex_session_id': 'sid-1'}), encoding='utf-8')

    spec = SimpleNamespace(name='agent1')
    profile = SimpleNamespace(
        inherit_api=False,
        env={
            'OPENAI_API_KEY': 'profile-key',
            'OPENAI_BASE_URL': 'https://api.rootflowai.com',
        },
    )

    assert load_resume_session_id(spec, agent_dir, profile) is None

    session_file.write_text(
        json.dumps(
            {
                'codex_session_id': 'sid-1',
                'codex_provider_authority_fingerprint': codex_provider_authority_fingerprint(profile),
            }
        ),
        encoding='utf-8',
    )

    assert load_resume_session_id(spec, agent_dir, profile) is None

    session_file.write_text(
        json.dumps(
            {
                'codex_session_id': 'sid-1',
                'codex_provider_authority_fingerprint': codex_provider_authority_fingerprint(profile),
                'codex_session_authority_fingerprint': codex_provider_authority_fingerprint(profile),
            }
        ),
        encoding='utf-8',
    )

    assert load_resume_session_id(spec, agent_dir, profile) == 'sid-1'
