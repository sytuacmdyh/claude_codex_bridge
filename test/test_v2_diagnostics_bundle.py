from __future__ import annotations

import json
import os
from pathlib import Path
import tarfile

from cli.context import CliContextBuilder
from cli.models import ParsedDoctorCommand
import cli.services.diagnostics_runtime.bundle as bundle_runtime
from cli.services.diagnostics import export_diagnostic_bundle
from project.ids import compute_project_id


def _read_tar_json(bundle_path: Path, member_name: str) -> dict:
    with tarfile.open(bundle_path, 'r:gz') as archive:
        with archive.extractfile(member_name) as handle:
            assert handle is not None
            return json.loads(handle.read().decode('utf-8'))


def _archive_members(bundle_path: Path) -> list[str]:
    with tarfile.open(bundle_path, 'r:gz') as archive:
        return archive.getnames()


def test_export_diagnostic_bundle_collects_reports_and_log_tails(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-bundle'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    context = CliContextBuilder().build(
        ParsedDoctorCommand(project=None, bundle=True),
        cwd=project_root,
        bootstrap_if_missing=False,
    )

    context.paths.ccbd_dir.mkdir(parents=True, exist_ok=True)
    context.paths.ccbd_state_path.write_text('{"record_type":"ccbd_project_namespace_state"}\n', encoding='utf-8')
    context.paths.ccbd_start_policy_path.write_text('{"record_type":"ccbd_start_policy"}\n', encoding='utf-8')
    context.paths.ccbd_lifecycle_log_path.write_text('{"record_type":"ccbd_project_namespace_event"}\n', encoding='utf-8')
    heartbeat_path = context.paths.heartbeat_subject_path('job_progress', 'job_1')
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(
        '{"record_type":"heartbeat_state"}\n',
        encoding='utf-8',
    )
    context.paths.ccbd_startup_report_path.write_text('{"broken":false}\n', encoding='utf-8')
    context.paths.ccbd_dir.joinpath('ccbd.stdout.log').write_text('\n'.join(f'line {i}' for i in range(400)), encoding='utf-8')
    context.paths.agent_runtime_path('demo').parent.mkdir(parents=True, exist_ok=True)
    context.paths.agent_runtime_path('demo').write_text(
        json.dumps(
            {
                'schema_version': 2,
                'record_type': 'agent_runtime',
                'agent_name': 'demo',
                'state': 'idle',
                'pid': 101,
                'started_at': '2026-04-03T00:00:00Z',
                'last_seen_at': '2026-04-03T00:00:01Z',
                'runtime_ref': 'tmux:%1',
                'session_ref': None,
                'workspace_path': str(context.paths.workspace_path('demo')),
                'project_id': context.project.project_id,
                'backend_type': 'tmux',
                'queue_depth': 0,
                'socket_path': None,
                'health': 'healthy',
            }
        ) + '\n',
        encoding='utf-8',
    )

    summary = export_diagnostic_bundle(context, ParsedDoctorCommand(project=None, bundle=True))
    bundle_path = Path(summary.bundle_path)
    manifest = _read_tar_json(bundle_path, f'{summary.bundle_id}/manifest.json')

    assert bundle_path.exists()
    assert summary.file_count >= 4
    assert summary.truncated_count >= 1
    assert any(entry['archive_path'] == 'project/.ccb/ccbd/state.json' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/ccbd/start-policy.json' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/ccbd/lifecycle.jsonl' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/ccbd/heartbeats/job_progress/job_1.json' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/ccbd/startup-report.json' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/ccbd/ccbd.stdout.log' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/agents/demo/runtime.json' for entry in manifest['entries'])


def test_export_diagnostic_bundle_includes_relocated_runtime_state_files(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-bundle-relocated'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    relocated_root = tmp_path / 'state-root'
    project_id = compute_project_id(project_root)
    (project_root / '.ccb' / 'runtime-root-ref.json').write_text(
        (
            '{"schema_version":1,"record_type":"ccb_runtime_root_ref","project_id":"'
            + project_id
            + '","runtime_state_root":"'
            + str(relocated_root)
            + '","created_at":"2026-05-07T00:00:00Z"}\n'
        ),
        encoding='utf-8',
    )
    context = CliContextBuilder().build(
        ParsedDoctorCommand(project=None, bundle=True),
        cwd=project_root,
        bootstrap_if_missing=False,
    )

    context.paths.ensure_runtime_state_root(created_at='2026-05-07T00:00:00Z')
    context.paths.ccbd_state_path.parent.mkdir(parents=True, exist_ok=True)
    context.paths.ccbd_state_path.write_text('{"record_type":"ccbd_project_namespace_state"}\n', encoding='utf-8')
    context.paths.ccbd_start_policy_path.write_text('{"record_type":"ccbd_start_policy"}\n', encoding='utf-8')

    summary = export_diagnostic_bundle(context, ParsedDoctorCommand(project=None, bundle=True))
    bundle_path = Path(summary.bundle_path)
    manifest = _read_tar_json(bundle_path, f'{summary.bundle_id}/manifest.json')

    assert any(entry['archive_path'] == 'project/.ccb/runtime-root-ref.json' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/runtime-root.json' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/ccbd/state.json' for entry in manifest['entries'])
    assert any(entry['archive_path'] == 'project/.ccb/ccbd/start-policy.json' for entry in manifest['entries'])
    assert any(entry['source_path'] == str(context.paths.runtime_root_marker_path) for entry in manifest['entries'])


def test_export_diagnostic_bundle_survives_corrupt_runtime_and_report_files(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-bundle-corrupt'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    context = CliContextBuilder().build(
        ParsedDoctorCommand(project=None, bundle=True),
        cwd=project_root,
        bootstrap_if_missing=False,
    )

    context.paths.ccbd_startup_report_path.parent.mkdir(parents=True, exist_ok=True)
    context.paths.ccbd_startup_report_path.write_text('{this is not json}\n', encoding='utf-8')
    context.paths.agent_runtime_path('demo').parent.mkdir(parents=True, exist_ok=True)
    context.paths.agent_runtime_path('demo').write_text('{this is also not json}\n', encoding='utf-8')

    summary = export_diagnostic_bundle(context, ParsedDoctorCommand(project=None, bundle=True))
    bundle_path = Path(summary.bundle_path)
    manifest = _read_tar_json(bundle_path, f'{summary.bundle_id}/manifest.json')

    assert bundle_path.exists()
    assert any(
        entry['archive_path'] == 'project/.ccb/ccbd/startup-report.json' and entry['status'] == 'included'
        for entry in manifest['entries']
    )
    assert any(
        entry['archive_path'] == 'project/.ccb/agents/demo/runtime.json' and entry['status'] == 'included'
        for entry in manifest['entries']
    )


def test_export_diagnostic_bundle_includes_provider_state_and_excludes_auth(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-bundle-provider-state'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    context = CliContextBuilder().build(
        ParsedDoctorCommand(project=None, bundle=True),
        cwd=project_root,
        bootstrap_if_missing=False,
    )

    provider_state_dir = context.paths.agent_provider_state_dir('demo', 'codex')
    session_log = provider_state_dir / 'home' / 'sessions' / '2026' / '04' / '19' / 'rollout-demo-session.jsonl'
    session_log.parent.mkdir(parents=True, exist_ok=True)
    session_log.write_text('{"type":"session_meta"}\n', encoding='utf-8')
    isolated_home = provider_state_dir / 'home'
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / 'config.toml').write_text('[model]\nname="gpt-5"\n', encoding='utf-8')
    (isolated_home / 'auth.json').write_text('{"OPENAI_API_KEY":"secret"}\n', encoding='utf-8')
    plugin_manifest = isolated_home / '.tmp' / 'plugins' / '.agents' / 'plugins' / 'marketplace.json'
    plugin_manifest.parent.mkdir(parents=True, exist_ok=True)
    plugin_manifest.write_text('{"name":"market"}\n', encoding='utf-8')
    (isolated_home / '.tmp' / 'plugins.sha').write_text('plugin-sha\n', encoding='utf-8')

    summary = export_diagnostic_bundle(context, ParsedDoctorCommand(project=None, bundle=True))
    bundle_path = Path(summary.bundle_path)
    manifest = _read_tar_json(bundle_path, f'{summary.bundle_id}/manifest.json')
    storage_summary = _read_tar_json(bundle_path, f'{summary.bundle_id}/generated/storage-summary.json')
    members = _archive_members(bundle_path)

    assert any(
        entry['archive_path'] == 'project/.ccb/agents/demo/provider-state/codex/home/sessions/2026/04/19/rollout-demo-session.jsonl'
        and entry['status'] == 'included'
        for entry in manifest['entries']
    )
    assert any(
        entry['archive_path'] == 'project/.ccb/agents/demo/provider-state/codex/home/config.toml'
        and entry['status'] == 'included'
        for entry in manifest['entries']
    )
    assert all(
        entry['archive_path'] != 'project/.ccb/agents/demo/provider-state/codex/home/auth.json'
        for entry in manifest['entries']
    )
    assert all(
        entry['archive_path'] != 'project/.ccb/agents/demo/provider-state/codex/home/.tmp/plugins/.agents/plugins/marketplace.json'
        for entry in manifest['entries']
    )
    assert any(
        entry['relative_path'] == 'agents/demo/provider-state/codex/home/.tmp/plugins/.agents/plugins/marketplace.json'
        and entry['storage_class'] == 'startup_authority_bundle'
        for entry in storage_summary['entries']
    )
    assert f'{summary.bundle_id}/generated/storage-summary.json' in members
    assert f'{summary.bundle_id}/project/.ccb/agents/demo/provider-state/codex/home/auth.json' not in members
    assert (
        f'{summary.bundle_id}/project/.ccb/agents/demo/provider-state/codex/home/.tmp/plugins/.agents/plugins/marketplace.json'
        not in members
    )


def test_export_diagnostic_bundle_hard_excludes_provider_cache_when_storage_summary_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-bundle-provider-state-storage-error'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('codexer:codex\nclauder:claude\ngem:gemini\n', encoding='utf-8')
    context = CliContextBuilder().build(
        ParsedDoctorCommand(project=None, bundle=True),
        cwd=project_root,
        bootstrap_if_missing=False,
    )

    codex_home = context.paths.agent_provider_state_dir('codexer', 'codex') / 'home'
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / 'config.toml').write_text('model = "gpt-5"\n', encoding='utf-8')
    plugin_manifest = codex_home / '.tmp' / 'plugins' / '.agents' / 'plugins' / 'marketplace.json'
    plugin_manifest.parent.mkdir(parents=True, exist_ok=True)
    plugin_manifest.write_text('{"name":"market"}\n', encoding='utf-8')
    (codex_home / '.tmp' / 'plugins.sha').write_text('plugin-sha\n', encoding='utf-8')
    outside = tmp_path / 'outside-provider-state'
    outside.mkdir(parents=True, exist_ok=True)
    (outside / 'leaked.json').write_text('{"secret":"outside"}\n', encoding='utf-8')
    try:
        os.symlink(outside, codex_home / 'linked-outside')
    except OSError:
        pass

    claude_home = context.paths.agent_provider_state_dir('clauder', 'claude') / 'home'
    claude_version_manifest = claude_home / '.local' / 'share' / 'claude' / 'versions' / '2.1.137' / 'metadata.json'
    claude_version_manifest.parent.mkdir(parents=True, exist_ok=True)
    claude_version_manifest.write_text('{"version":"2.1.137"}\n', encoding='utf-8')

    gemini_home = context.paths.agent_provider_state_dir('gem', 'gemini') / 'home'
    gemini_cache = gemini_home / '.npm' / '_cacache' / 'index.json'
    gemini_cache.parent.mkdir(parents=True, exist_ok=True)
    gemini_cache.write_text('{"cache":true}\n', encoding='utf-8')
    gemini_node_gyp = gemini_home / '.cache' / 'node-gyp' / 'config.json'
    gemini_node_gyp.parent.mkdir(parents=True, exist_ok=True)
    gemini_node_gyp.write_text('{"cache":true}\n', encoding='utf-8')

    def fail_storage_summary(_context):
        raise RuntimeError('storage failed')

    monkeypatch.setattr(bundle_runtime, 'summarize_storage', fail_storage_summary)

    summary = export_diagnostic_bundle(context, ParsedDoctorCommand(project=None, bundle=True))
    bundle_path = Path(summary.bundle_path)
    manifest = _read_tar_json(bundle_path, f'{summary.bundle_id}/manifest.json')
    storage_summary = _read_tar_json(bundle_path, f'{summary.bundle_id}/generated/storage-summary.json')
    members = _archive_members(bundle_path)

    assert storage_summary['error'] == 'storage failed'
    assert any(
        entry['archive_path'] == 'project/.ccb/agents/codexer/provider-state/codex/home/config.toml'
        and entry['status'] == 'included'
        for entry in manifest['entries']
    )
    assert all('/.tmp/plugins/' not in entry['archive_path'] for entry in manifest['entries'])
    assert all('/.local/share/claude/versions/' not in entry['archive_path'] for entry in manifest['entries'])
    assert all('/.npm/_cacache/' not in entry['archive_path'] for entry in manifest['entries'])
    assert all('/.cache/node-gyp/' not in entry['archive_path'] for entry in manifest['entries'])
    assert all('linked-outside' not in entry['archive_path'] for entry in manifest['entries'])
    assert all('/.tmp/plugins/' not in member for member in members)
    assert all('/.local/share/claude/versions/' not in member for member in members)
    assert all('/.npm/_cacache/' not in member for member in members)
    assert all('/.cache/node-gyp/' not in member for member in members)
    assert all('linked-outside' not in member for member in members)


def test_export_diagnostic_bundle_excludes_gemini_auth_artifacts(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-bundle-gemini-provider-state'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:gemini\n', encoding='utf-8')
    context = CliContextBuilder().build(
        ParsedDoctorCommand(project=None, bundle=True),
        cwd=project_root,
        bootstrap_if_missing=False,
    )

    provider_state_dir = context.paths.agent_provider_state_dir('demo', 'gemini')
    managed_home = provider_state_dir / 'home' / '.gemini'
    managed_home.mkdir(parents=True, exist_ok=True)
    (managed_home / 'settings.json').write_text('{"security":{"auth":{"selectedType":"oauth-personal"}}}\n', encoding='utf-8')
    (managed_home / '.env').write_text('GEMINI_API_KEY=secret\n', encoding='utf-8')
    (managed_home / 'google_accounts.json').write_text('{"active":"user@example.test"}\n', encoding='utf-8')
    (managed_home / 'oauth_creds.json').write_text('{"refresh_token":"secret"}\n', encoding='utf-8')

    summary = export_diagnostic_bundle(context, ParsedDoctorCommand(project=None, bundle=True))
    bundle_path = Path(summary.bundle_path)
    manifest = _read_tar_json(bundle_path, f'{summary.bundle_id}/manifest.json')
    members = _archive_members(bundle_path)

    assert any(
        entry['archive_path'] == 'project/.ccb/agents/demo/provider-state/gemini/home/.gemini/settings.json'
        and entry['status'] == 'included'
        for entry in manifest['entries']
    )
    assert all(
        entry['archive_path'] != 'project/.ccb/agents/demo/provider-state/gemini/home/.gemini/oauth_creds.json'
        for entry in manifest['entries']
    )
    assert all(
        entry['archive_path'] != 'project/.ccb/agents/demo/provider-state/gemini/home/.gemini/.env'
        for entry in manifest['entries']
    )
    assert all(
        entry['archive_path'] != 'project/.ccb/agents/demo/provider-state/gemini/home/.gemini/google_accounts.json'
        for entry in manifest['entries']
    )
    assert f'{summary.bundle_id}/project/.ccb/agents/demo/provider-state/gemini/home/.gemini/oauth_creds.json' not in members
    assert f'{summary.bundle_id}/project/.ccb/agents/demo/provider-state/gemini/home/.gemini/.env' not in members
    assert f'{summary.bundle_id}/project/.ccb/agents/demo/provider-state/gemini/home/.gemini/google_accounts.json' not in members


def test_export_diagnostic_bundle_excludes_claude_credentials(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-bundle-claude-provider-state'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:claude\n', encoding='utf-8')
    context = CliContextBuilder().build(
        ParsedDoctorCommand(project=None, bundle=True),
        cwd=project_root,
        bootstrap_if_missing=False,
    )

    provider_state_dir = context.paths.agent_provider_state_dir('demo', 'claude')
    managed_home = provider_state_dir / 'home' / '.claude'
    managed_home.mkdir(parents=True, exist_ok=True)
    (managed_home / 'settings.json').write_text('{"theme":"dark"}\n', encoding='utf-8')
    (managed_home / '.credentials.json').write_text('{"claudeAiOauth":{"refreshToken":"secret"}}\n', encoding='utf-8')

    summary = export_diagnostic_bundle(context, ParsedDoctorCommand(project=None, bundle=True))
    bundle_path = Path(summary.bundle_path)
    manifest = _read_tar_json(bundle_path, f'{summary.bundle_id}/manifest.json')
    members = _archive_members(bundle_path)

    assert any(
        entry['archive_path'] == 'project/.ccb/agents/demo/provider-state/claude/home/.claude/settings.json'
        and entry['status'] == 'included'
        for entry in manifest['entries']
    )
    assert all(
        entry['archive_path'] != 'project/.ccb/agents/demo/provider-state/claude/home/.claude/.credentials.json'
        for entry in manifest['entries']
    )
    assert f'{summary.bundle_id}/project/.ccb/agents/demo/provider-state/claude/home/.claude/.credentials.json' not in members


def test_export_diagnostic_bundle_excludes_claude_home_hook_assets(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-bundle-claude-hook-assets'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:claude\n', encoding='utf-8')
    context = CliContextBuilder().build(
        ParsedDoctorCommand(project=None, bundle=True),
        cwd=project_root,
        bootstrap_if_missing=False,
    )

    provider_state_dir = context.paths.agent_provider_state_dir('demo', 'claude')
    managed_home = provider_state_dir / 'home'
    (managed_home / '.claude').mkdir(parents=True, exist_ok=True)
    (managed_home / '.claude' / 'settings.json').write_text('{"theme":"dark"}\n', encoding='utf-8')
    (managed_home / '.codeisland').mkdir(parents=True, exist_ok=True)
    (managed_home / '.codeisland' / 'state.json').write_text('{"secret":"token"}\n', encoding='utf-8')
    (managed_home / '.codeisland' / 'codeisland-hook.sh').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')

    summary = export_diagnostic_bundle(context, ParsedDoctorCommand(project=None, bundle=True))
    bundle_path = Path(summary.bundle_path)
    manifest = _read_tar_json(bundle_path, f'{summary.bundle_id}/manifest.json')
    members = _archive_members(bundle_path)

    assert any(
        entry['archive_path'] == 'project/.ccb/agents/demo/provider-state/claude/home/.claude/settings.json'
        and entry['status'] == 'included'
        for entry in manifest['entries']
    )
    assert all('/.codeisland/' not in entry['archive_path'] for entry in manifest['entries'])
    assert all('/.codeisland/' not in member for member in members)
