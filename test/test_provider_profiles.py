from __future__ import annotations

import json
from pathlib import Path
import shutil

from agents.models import AgentSpec, PermissionMode, ProviderProfileSpec, QueuePolicy, RestoreMode, RuntimeMode, WorkspaceMode
import provider_backends.claude.launcher_runtime.home as claude_home_runtime
from provider_backends.claude.launcher_runtime.home import materialize_claude_home_config
from provider_backends.gemini.launcher_runtime.home import materialize_gemini_home_config
import provider_profiles.codex_home_config as codex_home_config
from provider_profiles.codex_home_config import codex_provider_authority_fingerprint
from provider_profiles import materialize_provider_profile
from storage.paths import PathLayout


def _spec(name: str, provider: str = "codex", *, provider_profile: ProviderProfileSpec | None = None) -> AgentSpec:
    return AgentSpec(
        name=name,
        provider=provider,
        target='.',
        workspace_mode=WorkspaceMode.GIT_WORKTREE,
        workspace_root=None,
        runtime_mode=RuntimeMode.PANE_BACKED,
        restore_default=RestoreMode.AUTO,
        permission_default=PermissionMode.MANUAL,
        queue_policy=QueuePolicy.SERIAL_PER_AGENT,
        provider_profile=provider_profile or ProviderProfileSpec(),
    )


def _write_codex_plugin_source(
    home: Path,
    *,
    plugin_name: str = 'demo-plugin',
    sha: str | None = 'plugins-sha-v1',
    marketplace_name: str = 'openai-curated',
    skill_body: str = 'plugin skill v1\n',
) -> None:
    plugin_root = home / '.tmp' / 'plugins'
    (plugin_root / '.agents' / 'plugins').mkdir(parents=True, exist_ok=True)
    (plugin_root / '.agents' / 'skills' / 'plugin-creator').mkdir(parents=True, exist_ok=True)
    (plugin_root / 'plugins' / plugin_name / '.codex-plugin').mkdir(parents=True, exist_ok=True)
    (plugin_root / 'plugins' / plugin_name / 'skills' / plugin_name).mkdir(parents=True, exist_ok=True)
    (home / '.tmp').mkdir(parents=True, exist_ok=True)
    if sha is None:
        (home / '.tmp' / 'plugins.sha').unlink(missing_ok=True)
    else:
        (home / '.tmp' / 'plugins.sha').write_text(f'{sha}\n', encoding='utf-8')
    (plugin_root / '.agents' / 'plugins' / 'marketplace.json').write_text(
        json.dumps(
            {
                'name': marketplace_name,
                'plugins': [
                    {
                        'name': plugin_name,
                        'source': {'source': 'local', 'path': f'./plugins/{plugin_name}'},
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    (plugin_root / 'plugins' / plugin_name / '.codex-plugin' / 'plugin.json').write_text(
        json.dumps({'name': plugin_name}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    (plugin_root / 'plugins' / plugin_name / 'skills' / plugin_name / 'SKILL.md').write_text(
        skill_body,
        encoding='utf-8',
    )


def test_materialize_codex_profile_copies_inherited_assets(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    source_home = tmp_path / 'system-codex-home'
    (source_home / 'skills').mkdir(parents=True, exist_ok=True)
    (source_home / 'commands').mkdir(parents=True, exist_ok=True)
    (source_home / 'config.toml').write_text('model = "gpt-5"\n', encoding='utf-8')
    (source_home / 'auth.json').write_text('{"OPENAI_API_KEY":"system-key"}', encoding='utf-8')
    (source_home / 'skills' / 'demo.md').write_text('demo skill\n', encoding='utf-8')
    (source_home / 'commands' / 'demo.md').write_text('demo command\n', encoding='utf-8')
    _write_codex_plugin_source(source_home)
    monkeypatch.setenv('CODEX_HOME', str(source_home))

    profile = materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec(
            'agent1',
            provider_profile=ProviderProfileSpec(
                mode='isolated',
                inherit_api=False,
                inherit_auth=True,
                inherit_config=True,
                inherit_skills=True,
                inherit_commands=True,
            ),
        ),
        workspace_path=project_root,
    )

    runtime_home = Path(profile.runtime_home or '')
    assert runtime_home.is_dir()
    assert (runtime_home / 'config.toml').is_file()
    assert (runtime_home / 'auth.json').is_file()
    assert (runtime_home / 'skills' / 'demo.md').is_file()
    assert (runtime_home / 'commands' / 'demo.md').is_file()
    assert (runtime_home / '.tmp' / 'plugins.sha').read_text(encoding='utf-8') == 'plugins-sha-v1\n'
    assert (runtime_home / '.tmp' / 'plugins' / '.agents' / 'plugins' / 'marketplace.json').is_file()
    assert (runtime_home / '.tmp' / 'plugins' / 'plugins' / 'demo-plugin' / '.codex-plugin' / 'plugin.json').is_file()
    assert (runtime_home / 'sessions').is_dir()


def test_materialize_codex_profile_writes_agent_local_provider_config_for_explicit_api(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo'
    source_home = tmp_path / 'system-codex-home'
    source_home.mkdir(parents=True, exist_ok=True)
    (source_home / 'config.toml').write_text(
        '\n'.join(
            [
                'model_provider = "stale"',
                'model = "gpt-5.4-openai-compact"',
                'model_reasoning_effort = "xhigh"',
                'disable_response_storage = true',
                '',
                '[projects."/tmp/demo-project"]',
                'trust_level = "trusted"',
                '',
                '[model_providers.stale]',
                'name = "stale"',
                'base_url = "https://stale.example.test/v1"',
                'wire_api = "responses"',
                'requires_openai_auth = true',
                '',
            ]
        ),
        encoding='utf-8',
    )
    monkeypatch.setenv('CODEX_HOME', str(source_home))
    _write_codex_plugin_source(
        source_home,
        plugin_name='weatherpromise',
        marketplace_name='codex-official',
        skill_body='plugin skill explicit\n',
    )

    profile = materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec(
            'agent2',
            provider_profile=ProviderProfileSpec(
                mode='isolated',
                env={
                    'OPENAI_API_KEY': 'profile-key',
                    'OPENAI_BASE_URL': 'https://api.rootflowai.com',
                },
                inherit_api=False,
                inherit_auth=False,
                inherit_config=False,
            ),
        ),
        workspace_path=project_root,
    )

    runtime_home = Path(profile.runtime_home or '')
    config_text = (runtime_home / 'config.toml').read_text(encoding='utf-8')
    assert 'model_provider = "custom"' in config_text
    assert 'model = "gpt-5.4-openai-compact"' in config_text
    assert 'model_reasoning_effort = "xhigh"' in config_text
    assert 'disable_response_storage = true' in config_text
    assert '[projects."/tmp/demo-project"]' in config_text
    assert '[model_providers.custom]' in config_text
    assert 'base_url = "https://api.rootflowai.com"' in config_text
    assert 'wire_api = "responses"' in config_text
    assert 'requires_openai_auth = false' in config_text
    assert 'https://stale.example.test/v1' not in config_text
    assert 'env_key' not in config_text
    assert codex_provider_authority_fingerprint(profile)
    auth_payload = json.loads((runtime_home / 'auth.json').read_text(encoding='utf-8'))
    assert auth_payload == {'OPENAI_API_KEY': 'profile-key'}
    assert (runtime_home / '.tmp' / 'plugins.sha').read_text(encoding='utf-8') == 'plugins-sha-v1\n'
    assert (runtime_home / '.tmp' / 'plugins' / '.agents' / 'plugins' / 'marketplace.json').is_file()
    assert (runtime_home / '.tmp' / 'plugins' / 'plugins' / 'weatherpromise' / 'skills' / 'weatherpromise' / 'SKILL.md').read_text(encoding='utf-8') == 'plugin skill explicit\n'


def test_materialize_codex_profile_refreshes_plugin_projection_when_source_changes(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    source_home = tmp_path / 'system-codex-home'
    source_home.mkdir(parents=True, exist_ok=True)
    (source_home / 'config.toml').write_text('model = "gpt-5"\n', encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME', str(source_home))
    _write_codex_plugin_source(
        source_home,
        plugin_name='weatherpromise',
        sha='plugins-sha-v1',
        marketplace_name='market-v1',
        skill_body='plugin skill v1\n',
    )

    profile = materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )

    runtime_home = Path(profile.runtime_home or '')
    marketplace_path = runtime_home / '.tmp' / 'plugins' / '.agents' / 'plugins' / 'marketplace.json'
    skill_path = runtime_home / '.tmp' / 'plugins' / 'plugins' / 'weatherpromise' / 'skills' / 'weatherpromise' / 'SKILL.md'
    assert skill_path.read_text(encoding='utf-8') == 'plugin skill v1\n'

    _write_codex_plugin_source(
        source_home,
        plugin_name='weatherpromise',
        sha='plugins-sha-v2',
        marketplace_name='market-v2',
        skill_body='plugin skill v2\n',
    )

    materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )

    marketplace_payload = json.loads(marketplace_path.read_text(encoding='utf-8'))
    assert marketplace_payload['name'] == 'market-v2'
    assert skill_path.read_text(encoding='utf-8') == 'plugin skill v2\n'
    assert (runtime_home / '.tmp' / 'plugins.sha').read_text(encoding='utf-8') == 'plugins-sha-v2\n'

    plugin_source_root = source_home / '.tmp' / 'plugins'
    plugin_sha_path = source_home / '.tmp' / 'plugins.sha'
    shutil.rmtree(plugin_source_root)
    plugin_sha_path.unlink(missing_ok=True)

    materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )

    assert not (runtime_home / '.tmp' / 'plugins').exists()
    assert not (runtime_home / '.tmp' / 'plugins.sha').exists()


def test_materialize_codex_profile_refreshes_plugin_projection_without_sha_marker(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    source_home = tmp_path / 'system-codex-home'
    source_home.mkdir(parents=True, exist_ok=True)
    (source_home / 'config.toml').write_text('model = "gpt-5"\n', encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME', str(source_home))
    _write_codex_plugin_source(
        source_home,
        plugin_name='weatherpromise',
        sha=None,
        marketplace_name='market-no-sha-v1',
        skill_body='plugin skill no sha v1\n',
    )

    profile = materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )

    runtime_home = Path(profile.runtime_home or '')
    marketplace_path = runtime_home / '.tmp' / 'plugins' / '.agents' / 'plugins' / 'marketplace.json'
    skill_path = runtime_home / '.tmp' / 'plugins' / 'plugins' / 'weatherpromise' / 'skills' / 'weatherpromise' / 'SKILL.md'
    assert not (runtime_home / '.tmp' / 'plugins.sha').exists()
    assert skill_path.read_text(encoding='utf-8') == 'plugin skill no sha v1\n'

    _write_codex_plugin_source(
        source_home,
        plugin_name='weatherpromise',
        sha=None,
        marketplace_name='market-no-sha-v2',
        skill_body='plugin skill no sha v2 updated\n',
    )

    materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )

    marketplace_payload = json.loads(marketplace_path.read_text(encoding='utf-8'))
    assert marketplace_payload['name'] == 'market-no-sha-v2'
    assert skill_path.read_text(encoding='utf-8') == 'plugin skill no sha v2 updated\n'
    assert not (runtime_home / '.tmp' / 'plugins.sha').exists()


def test_materialize_codex_profile_skips_plugin_recopy_when_sha_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    source_home = tmp_path / 'system-codex-home'
    source_home.mkdir(parents=True, exist_ok=True)
    (source_home / 'config.toml').write_text('model = "gpt-5"\n', encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME', str(source_home))
    _write_codex_plugin_source(
        source_home,
        plugin_name='weatherpromise',
        sha='stable-plugin-sha',
        marketplace_name='market-stable',
        skill_body='plugin skill stable\n',
    )

    copied_sources: list[Path] = []
    real_copytree = codex_home_config.shutil.copytree

    def tracking_copytree(src, dst, *args, **kwargs):
        src_path = Path(src)
        if src_path == source_home / '.tmp' / 'plugins':
            copied_sources.append(src_path)
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(codex_home_config.shutil, 'copytree', tracking_copytree)

    materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )
    materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )

    assert copied_sources == [source_home / '.tmp' / 'plugins']


def test_materialize_codex_profile_repairs_incomplete_plugin_projection_even_when_sha_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo'
    source_home = tmp_path / 'system-codex-home'
    source_home.mkdir(parents=True, exist_ok=True)
    (source_home / 'config.toml').write_text('model = "gpt-5"\n', encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME', str(source_home))
    _write_codex_plugin_source(
        source_home,
        plugin_name='weatherpromise',
        sha='repairable-plugin-sha',
        marketplace_name='market-repair',
        skill_body='plugin skill repair\n',
    )

    profile = materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )

    runtime_home = Path(profile.runtime_home or '')
    marketplace_path = runtime_home / '.tmp' / 'plugins' / '.agents' / 'plugins' / 'marketplace.json'
    marketplace_path.unlink()
    assert not marketplace_path.exists()

    materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec('agent1', provider_profile=ProviderProfileSpec(mode='isolated')),
        workspace_path=project_root,
    )

    marketplace_payload = json.loads(marketplace_path.read_text(encoding='utf-8'))
    assert marketplace_payload['name'] == 'market-repair'


def test_materialize_claude_profile_creates_runtime_home(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'

    profile = materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec(
            'agent1',
            provider='claude',
            provider_profile=ProviderProfileSpec(
                mode='isolated',
                inherit_api=False,
            ),
        ),
        workspace_path=project_root,
    )

    runtime_home = Path(profile.runtime_home or '')
    assert runtime_home.is_dir()


def test_materialize_claude_home_config_projects_system_settings_into_managed_home(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.claude' / 'settings.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'env': {
                    'ANTHROPIC_AUTH_TOKEN': 'system-token',
                    'ANTHROPIC_BASE_URL': 'https://claude.example.test',
                },
                'theme': 'light',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['env']['ANTHROPIC_AUTH_TOKEN'] == 'system-token'
    assert payload['env']['ANTHROPIC_BASE_URL'] == 'https://claude.example.test'
    assert payload['theme'] == 'light'


def test_materialize_claude_home_config_projects_official_login_auth_into_managed_home(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_credentials = source_home / '.claude' / '.credentials.json'
    source_legacy_auth = source_home / '.config' / 'claude-code' / 'auth.json'
    source_credentials.parent.mkdir(parents=True, exist_ok=True)
    source_legacy_auth.parent.mkdir(parents=True, exist_ok=True)
    source_credentials.write_text(
        json.dumps({'claudeAiOauth': {'refreshToken': 'system-refresh-token'}}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    source_legacy_auth.write_text(
        json.dumps({'refresh_token': 'legacy-system-refresh-token'}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    assert json.loads(layout.credentials_path.read_text(encoding='utf-8'))['claudeAiOauth']['refreshToken'] == 'system-refresh-token'
    assert json.loads(layout.auth_path.read_text(encoding='utf-8'))['refresh_token'] == 'legacy-system-refresh-token'


def test_materialize_claude_home_config_refreshes_login_metadata_without_replacing_trust(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_trust = source_home / '.claude.json'
    target_trust = target_home / '.claude.json'
    source_trust.parent.mkdir(parents=True, exist_ok=True)
    target_trust.parent.mkdir(parents=True, exist_ok=True)
    source_trust.write_text(
        json.dumps(
            {
                'oauthAccount': {
                    'emailAddress': 'user@example.test',
                    'organizationUuid': 'org-source',
                },
                'hasCompletedOnboarding': True,
                'lastOnboardingVersion': '2.1.97',
                '/source/workspace': {'hasTrustDialogAccepted': True},
                'primaryApiKey': 'must-not-project',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    target_trust.write_text(
        json.dumps(
            {
                'oauthAccount': {'emailAddress': 'stale@example.test'},
                'primaryApiKey': 'stale-key',
                '/managed/workspace': {'hasTrustDialogAccepted': True},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.trust_path.read_text(encoding='utf-8'))
    assert payload['oauthAccount']['emailAddress'] == 'user@example.test'
    assert payload['oauthAccount']['organizationUuid'] == 'org-source'
    assert payload['hasCompletedOnboarding'] is True
    assert payload['lastOnboardingVersion'] == '2.1.97'
    assert payload['/managed/workspace']['hasTrustDialogAccepted'] is True
    assert '/source/workspace' not in payload
    assert 'primaryApiKey' not in payload


def test_materialize_claude_home_config_strips_login_metadata_when_auth_not_inherited(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    target_trust = target_home / '.claude.json'
    target_trust.parent.mkdir(parents=True, exist_ok=True)
    target_trust.write_text(
        json.dumps(
            {
                'oauthAccount': {'emailAddress': 'stale@example.test'},
                'primaryApiKey': 'stale-key',
                '/managed/workspace': {'hasTrustDialogAccepted': True},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    (source_home / '.claude.json').parent.mkdir(parents=True, exist_ok=True)
    (source_home / '.claude.json').write_text('{"oauthAccount":{"emailAddress":"source@example.test"}}\n', encoding='utf-8')

    layout = materialize_claude_home_config(
        target_home,
        profile=ProviderProfileSpec(inherit_auth=False, inherit_api=False),
        source_home=source_home,
    )

    payload = json.loads(layout.trust_path.read_text(encoding='utf-8'))
    assert 'oauthAccount' not in payload
    assert 'primaryApiKey' not in payload
    assert payload['/managed/workspace']['hasTrustDialogAccepted'] is True


def test_materialize_claude_home_config_projects_macos_keychain_login_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_home.mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = json.dumps({'claudeAiOauth': {'refreshToken': 'keychain-refresh-token'}})

    def fake_run(argv, **kwargs):
        calls.append([str(part) for part in argv])
        assert kwargs['capture_output'] is True
        assert kwargs['text'] is True
        return Result()

    monkeypatch.setattr(claude_home_runtime.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(claude_home_runtime.shutil, 'which', lambda name: '/usr/bin/security')
    monkeypatch.setattr(claude_home_runtime.subprocess, 'run', fake_run)
    monkeypatch.setenv('USER', 'mac-user')

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.credentials_path.read_text(encoding='utf-8'))
    assert payload['claudeAiOauth']['refreshToken'] == 'keychain-refresh-token'
    assert calls[0] == [
        '/usr/bin/security',
        'find-generic-password',
        '-a',
        'mac-user',
        '-s',
        'Claude Code',
        '-w',
    ]


def test_materialize_claude_home_config_preserves_runtime_hooks_and_permissions(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.claude' / 'settings.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'env': {'ANTHROPIC_AUTH_TOKEN': 'system-token'},
                'theme': 'dark',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    target_settings = target_home / '.claude' / 'settings.json'
    target_settings.parent.mkdir(parents=True, exist_ok=True)
    target_settings.write_text(
        json.dumps(
            {
                'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo hook'}]}]},
                'permissions': {'allow': ['Bash(ls)']},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['env']['ANTHROPIC_AUTH_TOKEN'] == 'system-token'
    assert payload['theme'] == 'dark'
    assert payload['hooks']['Stop'][0]['hooks'][0]['command'] == 'echo hook'
    assert payload['permissions']['allow'] == ['Bash(ls)']


def test_materialize_claude_home_config_refreshes_inherited_skill_assets(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_claude_dir = source_home / '.claude'
    (source_claude_dir / 'skills' / 'review').mkdir(parents=True, exist_ok=True)
    (source_claude_dir / 'commands').mkdir(parents=True, exist_ok=True)
    (source_claude_dir / 'skills' / 'review' / 'SKILL.md').write_text('skill-v1\n', encoding='utf-8')
    (source_claude_dir / 'commands' / 'check.md').write_text('command-v1\n', encoding='utf-8')
    (source_claude_dir / 'CLAUDE.md').write_text('claude-md-v1\n', encoding='utf-8')

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    assert (layout.claude_dir / 'skills' / 'review' / 'SKILL.md').read_text(encoding='utf-8') == 'skill-v1\n'
    assert (layout.claude_dir / 'commands' / 'check.md').read_text(encoding='utf-8') == 'command-v1\n'
    assert (layout.claude_dir / 'CLAUDE.md').read_text(encoding='utf-8') == 'claude-md-v1\n'

    (source_claude_dir / 'skills' / 'review' / 'SKILL.md').write_text('skill-v2\n', encoding='utf-8')
    (source_claude_dir / 'commands' / 'check.md').write_text('command-v2\n', encoding='utf-8')
    (source_claude_dir / 'CLAUDE.md').write_text('claude-md-v2\n', encoding='utf-8')

    materialize_claude_home_config(target_home, source_home=source_home)

    assert (layout.claude_dir / 'skills' / 'review' / 'SKILL.md').read_text(encoding='utf-8') == 'skill-v2\n'
    assert (layout.claude_dir / 'commands' / 'check.md').read_text(encoding='utf-8') == 'command-v2\n'
    assert (layout.claude_dir / 'CLAUDE.md').read_text(encoding='utf-8') == 'claude-md-v2\n'


def test_materialize_claude_home_config_projects_referenced_home_hook_assets(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.claude' / 'settings.json'
    source_hook = source_home / '.codeisland' / 'codeisland-hook.sh'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_hook.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'hooks': {
                    'Stop': [
                        {
                            'hooks': [
                                {
                                    'type': 'command',
                                    'command': '$HOME/.codeisland/codeisland-hook.sh',
                                }
                            ]
                        }
                    ]
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    source_hook.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    assert (layout.home_root / '.codeisland' / 'codeisland-hook.sh').read_text(encoding='utf-8') == '#!/bin/sh\nexit 0\n'


def test_materialize_claude_home_config_does_not_project_home_hook_assets_without_config_inheritance(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.claude' / 'settings.json'
    source_hook = source_home / '.codeisland' / 'codeisland-hook.sh'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_hook.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': '${HOME}/.codeisland/codeisland-hook.sh'}]}]}},
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    source_hook.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')

    layout = materialize_claude_home_config(
        target_home,
        source_home=source_home,
        profile=ProviderProfileSpec(inherit_config=False),
    )

    assert not (layout.home_root / '.codeisland').exists()


def test_materialize_claude_home_config_respects_inherit_skills_flag(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_claude_dir = source_home / '.claude'
    (source_claude_dir / 'skills' / 'review').mkdir(parents=True, exist_ok=True)
    (source_claude_dir / 'commands').mkdir(parents=True, exist_ok=True)
    (source_claude_dir / 'skills' / 'review' / 'SKILL.md').write_text('skill\n', encoding='utf-8')
    (source_claude_dir / 'commands' / 'check.md').write_text('command\n', encoding='utf-8')
    (source_claude_dir / 'CLAUDE.md').write_text('claude-md\n', encoding='utf-8')

    layout = materialize_claude_home_config(
        target_home,
        profile=ProviderProfileSpec(inherit_skills=False, inherit_commands=True),
        source_home=source_home,
    )

    assert not (layout.claude_dir / 'skills').exists()
    assert not (layout.claude_dir / 'CLAUDE.md').exists()
    assert (layout.claude_dir / 'commands' / 'check.md').read_text(encoding='utf-8') == 'command\n'


def test_materialize_claude_home_config_preserves_managed_auth_when_source_is_logged_out(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.claude' / 'settings.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'env': {
                    'ANTHROPIC_BASE_URL': 'https://claude.example.test',
                },
                'theme': 'light',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    target_settings = target_home / '.claude' / 'settings.json'
    target_settings.parent.mkdir(parents=True, exist_ok=True)
    target_settings.write_text(
        json.dumps(
            {
                'env': {
                    'ANTHROPIC_AUTH_TOKEN': 'managed-token',
                    'ANTHROPIC_BASE_URL': 'https://managed.example.test',
                },
                'theme': 'stale-theme',
                'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo hook'}]}]},
                'permissions': {'allow': ['Bash(ls)']},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['env']['ANTHROPIC_AUTH_TOKEN'] == 'managed-token'
    assert payload['env']['ANTHROPIC_BASE_URL'] == 'https://claude.example.test'
    assert payload['theme'] == 'light'
    assert payload['hooks']['Stop'][0]['hooks'][0]['command'] == 'echo hook'
    assert payload['permissions']['allow'] == ['Bash(ls)']


def test_materialize_claude_home_config_refreshes_source_auth_over_managed_auth(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.claude' / 'settings.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'env': {
                    'ANTHROPIC_AUTH_TOKEN': 'system-token',
                    'ANTHROPIC_BASE_URL': 'https://claude.example.test',
                },
                'theme': 'light',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    target_settings = target_home / '.claude' / 'settings.json'
    target_settings.parent.mkdir(parents=True, exist_ok=True)
    target_settings.write_text(
        json.dumps(
            {
                'env': {
                    'ANTHROPIC_AUTH_TOKEN': 'managed-token',
                    'ANTHROPIC_BASE_URL': 'https://managed.example.test',
                },
                'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo hook'}]}]},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['env']['ANTHROPIC_AUTH_TOKEN'] == 'system-token'
    assert payload['env']['ANTHROPIC_BASE_URL'] == 'https://claude.example.test'
    assert payload['theme'] == 'light'
    assert payload['hooks']['Stop'][0]['hooks'][0]['command'] == 'echo hook'


def test_materialize_claude_home_config_clears_stale_managed_auth_when_auth_is_not_inherited(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    target_settings = target_home / '.claude' / 'settings.json'
    target_auth = target_home / '.config' / 'claude-code' / 'auth.json'
    target_credentials = target_home / '.claude' / '.credentials.json'
    target_settings.parent.mkdir(parents=True, exist_ok=True)
    target_settings.write_text(
        json.dumps(
            {
                'env': {'ANTHROPIC_AUTH_TOKEN': 'managed-token'},
                'theme': 'stale-theme',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    target_auth.parent.mkdir(parents=True, exist_ok=True)
    target_auth.write_text('{"refresh_token":"stale-token"}\n', encoding='utf-8')
    target_credentials.write_text('{"claudeAiOauth":{"refreshToken":"stale-token"}}\n', encoding='utf-8')

    layout = materialize_claude_home_config(
        target_home,
        profile=ProviderProfileSpec(inherit_auth=False, inherit_api=False, inherit_config=True),
        source_home=source_home,
    )

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload == {}
    assert not layout.auth_path.exists()
    assert not layout.credentials_path.exists()


def test_materialize_claude_home_config_preserves_managed_official_login_when_source_is_logged_out(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.claude' / 'settings.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'theme': 'light',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    target_credentials = target_home / '.claude' / '.credentials.json'
    target_credentials.parent.mkdir(parents=True, exist_ok=True)
    target_credentials.write_text('{"claudeAiOauth":{"refreshToken":"managed-refresh-token"}}\n', encoding='utf-8')

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    assert json.loads(layout.credentials_path.read_text(encoding='utf-8'))['claudeAiOauth']['refreshToken'] == 'managed-refresh-token'


def test_materialize_gemini_profile_keeps_runtime_home_unset_without_explicit_override(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'

    profile = materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec(
            'agent1',
            provider='gemini',
            provider_profile=ProviderProfileSpec(
                mode='isolated',
                inherit_api=False,
            ),
        ),
        workspace_path=project_root,
    )

    assert profile.runtime_home is None


def test_materialize_gemini_profile_uses_explicit_home_override(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    explicit_home = tmp_path / 'gemini-home'

    profile = materialize_provider_profile(
        layout=PathLayout(project_root),
        spec=_spec(
            'agent1',
            provider='gemini',
            provider_profile=ProviderProfileSpec(
                mode='isolated',
                home=str(explicit_home),
                inherit_api=False,
            ),
        ),
        workspace_path=project_root,
    )

    runtime_home = Path(profile.runtime_home or '')
    assert runtime_home == explicit_home.resolve()
    assert (runtime_home / '.gemini' / 'tmp').is_dir()


def test_materialize_gemini_home_config_projects_system_settings_into_managed_home(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.gemini' / 'settings.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'env': {
                    'GEMINI_API_KEY': 'system-gemini-key',
                    'GEMINI_MODEL': 'gemini-3.1-pro-preview',
                    'GOOGLE_API_KEY': 'system-google-key',
                    'GOOGLE_GEMINI_BASE_URL': 'https://chatapi.onechats.ai',
                },
                'theme': 'Default',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    layout = materialize_gemini_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['env']['GEMINI_API_KEY'] == 'system-gemini-key'
    assert payload['env']['GEMINI_MODEL'] == 'gemini-3.1-pro-preview'
    assert payload['env']['GOOGLE_API_KEY'] == 'system-google-key'
    assert payload['env']['GOOGLE_GEMINI_BASE_URL'] == 'https://chatapi.onechats.ai'
    assert payload['theme'] == 'Default'


def test_materialize_gemini_home_config_projects_dotenv_api_auth_into_managed_home(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_gemini = source_home / '.gemini'
    source_gemini.mkdir(parents=True, exist_ok=True)
    (source_gemini / 'settings.json').write_text(
        json.dumps(
            {
                'security': {
                    'auth': {
                        'selectedType': 'gemini-api-key',
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    (source_gemini / '.env').write_text(
        '\n'.join(
            [
                'GEMINI_API_KEY=system-gemini-key',
                'GOOGLE_GEMINI_BASE_URL=https://gemini.example.test',
                'GOOGLE_GENAI_USE_GCA=true',
                'GOOGLE_CLOUD_PROJECT=demo-project',
                'OTHER_SECRET=must-not-copy',
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    layout = materialize_gemini_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    dotenv = (layout.gemini_dir / '.env').read_text(encoding='utf-8')
    assert payload['security']['auth']['selectedType'] == 'gemini-api-key'
    assert 'GEMINI_API_KEY="system-gemini-key"' in dotenv
    assert 'GOOGLE_GEMINI_BASE_URL="https://gemini.example.test"' in dotenv
    assert 'GOOGLE_GENAI_USE_GCA="true"' in dotenv
    assert 'GOOGLE_CLOUD_PROJECT="demo-project"' in dotenv
    assert 'OTHER_SECRET' not in dotenv


def test_materialize_gemini_home_config_projects_oauth_credentials_for_login_auth(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.gemini' / 'settings.json'
    source_oauth = source_home / '.gemini' / 'oauth_creds.json'
    source_accounts = source_home / '.gemini' / 'google_accounts.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'security': {
                    'auth': {
                        'selectedType': 'oauth-personal',
                    }
                },
                'theme': 'Default',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    source_oauth.write_text(
        json.dumps({'refresh_token': 'system-refresh-token'}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    source_accounts.write_text(
        json.dumps({'active': 'user@example.test'}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    layout = materialize_gemini_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['security']['auth']['selectedType'] == 'oauth-personal'
    assert json.loads((layout.gemini_dir / 'oauth_creds.json').read_text(encoding='utf-8'))['refresh_token'] == 'system-refresh-token'
    assert json.loads((layout.gemini_dir / 'google_accounts.json').read_text(encoding='utf-8'))['active'] == 'user@example.test'


def test_materialize_gemini_home_config_strips_oauth_selection_and_credentials_when_auth_not_inherited(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.gemini' / 'settings.json'
    source_oauth = source_home / '.gemini' / 'oauth_creds.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'security': {
                    'auth': {
                        'selectedType': 'oauth-personal',
                    }
                },
                'theme': 'Default',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    source_oauth.write_text(
        json.dumps({'refresh_token': 'system-refresh-token'}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    target_oauth = target_home / '.gemini' / 'oauth_creds.json'
    target_oauth.parent.mkdir(parents=True, exist_ok=True)
    target_oauth.write_text('{"refresh_token":"stale-token"}\n', encoding='utf-8')
    target_accounts = target_home / '.gemini' / 'google_accounts.json'
    target_accounts.write_text('{"active":"stale@example.test"}\n', encoding='utf-8')

    layout = materialize_gemini_home_config(
        target_home,
        profile=ProviderProfileSpec(inherit_auth=False, inherit_config=True),
        source_home=source_home,
    )

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['theme'] == 'Default'
    assert payload.get('security', {}).get('auth', {}).get('selectedType') is None
    assert not (layout.gemini_dir / 'oauth_creds.json').exists()
    assert not (layout.gemini_dir / 'google_accounts.json').exists()


def test_materialize_gemini_home_config_strips_api_auth_selection_when_api_not_inherited(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.gemini' / 'settings.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    (source_home / '.gemini' / '.env').write_text('GEMINI_API_KEY=system-gemini-key\n', encoding='utf-8')
    source_settings.write_text(
        json.dumps(
            {
                'env': {'GEMINI_API_KEY': 'system-gemini-key'},
                'security': {
                    'auth': {
                        'selectedType': 'gemini-api-key',
                    }
                },
                'theme': 'Default',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    layout = materialize_gemini_home_config(
        target_home,
        profile=ProviderProfileSpec(inherit_api=False, inherit_config=True),
        source_home=source_home,
    )

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['theme'] == 'Default'
    assert payload.get('env') is None
    assert payload.get('security', {}).get('auth', {}).get('selectedType') is None
    assert not (layout.gemini_dir / '.env').exists()


def test_materialize_gemini_home_config_preserves_runtime_hooks(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_settings = source_home / '.gemini' / 'settings.json'
    source_settings.parent.mkdir(parents=True, exist_ok=True)
    source_settings.write_text(
        json.dumps(
            {
                'env': {'GEMINI_API_KEY': 'system-gemini-key'},
                'theme': 'Atom One',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    target_settings = target_home / '.gemini' / 'settings.json'
    target_settings.parent.mkdir(parents=True, exist_ok=True)
    target_settings.write_text(
        json.dumps(
            {
                'hooks': {
                    'AfterAgent': [
                        {'matcher': '*', 'hooks': [{'type': 'command', 'command': 'echo hook'}]},
                    ]
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    layout = materialize_gemini_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.settings_path.read_text(encoding='utf-8'))
    assert payload['env']['GEMINI_API_KEY'] == 'system-gemini-key'
    assert payload['theme'] == 'Atom One'
    assert payload['hooks']['AfterAgent'][0]['hooks'][0]['command'] == 'echo hook'


def test_materialize_gemini_home_config_merges_trusted_folders(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    source_trust = source_home / '.gemini' / 'trustedFolders.json'
    source_trust.parent.mkdir(parents=True, exist_ok=True)
    source_trust.write_text(
        json.dumps({'/system/project': 'TRUST_FOLDER'}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    target_trust = target_home / '.gemini' / 'trustedFolders.json'
    target_trust.parent.mkdir(parents=True, exist_ok=True)
    target_trust.write_text(
        json.dumps({'/managed/project': 'TRUST_FOLDER'}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    layout = materialize_gemini_home_config(target_home, source_home=source_home)

    payload = json.loads(layout.trusted_folders_path.read_text(encoding='utf-8'))
    assert payload['/system/project'] == 'TRUST_FOLDER'


def test_materialize_claude_home_config_syncs_extra_config_dirs(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    lark_cli = source_home / '.lark-cli' / 'config.json'
    lark_cli.parent.mkdir(parents=True, exist_ok=True)
    lark_cli.write_text('{"appId": "test"}\n', encoding='utf-8')
    lark_data = source_home / '.local' / 'share' / 'lark-cli' / 'master.key'
    lark_data.parent.mkdir(parents=True, exist_ok=True)
    lark_data.write_text('secret-key\n', encoding='utf-8')

    layout = materialize_claude_home_config(target_home, source_home=source_home)

    assert (layout.home_root / '.lark-cli' / 'config.json').read_text(encoding='utf-8') == '{"appId": "test"}\n'
    assert (layout.home_root / '.local' / 'share' / 'lark-cli' / 'master.key').read_text(encoding='utf-8') == 'secret-key\n'


def test_materialize_claude_home_config_skips_extra_config_when_inherit_config_false(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    lark_cli = source_home / '.lark-cli' / 'config.json'
    lark_cli.parent.mkdir(parents=True, exist_ok=True)
    lark_cli.write_text('{"appId": "test"}\n', encoding='utf-8')
    lark_data = source_home / '.local' / 'share' / 'lark-cli' / 'master.key'
    lark_data.parent.mkdir(parents=True, exist_ok=True)
    lark_data.write_text('secret-key\n', encoding='utf-8')
    # pre-populate stale copy in target
    stale = target_home / '.lark-cli' / 'config.json'
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"appId": "stale"}\n', encoding='utf-8')
    stale_data = target_home / '.local' / 'share' / 'lark-cli' / 'master.key'
    stale_data.parent.mkdir(parents=True, exist_ok=True)
    stale_data.write_text('stale-key\n', encoding='utf-8')

    profile = ProviderProfileSpec(inherit_config=False, inherit_auth=True)
    layout = materialize_claude_home_config(target_home, source_home=source_home, profile=profile)

    assert not (layout.home_root / '.lark-cli').exists()
    assert not (layout.home_root / '.local' / 'share' / 'lark-cli').exists()


def test_materialize_claude_home_config_skips_extra_config_when_inherit_auth_false(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    lark_cli = source_home / '.lark-cli' / 'config.json'
    lark_cli.parent.mkdir(parents=True, exist_ok=True)
    lark_cli.write_text('{"appId": "test"}\n', encoding='utf-8')
    lark_data = source_home / '.local' / 'share' / 'lark-cli' / 'master.key'
    lark_data.parent.mkdir(parents=True, exist_ok=True)
    lark_data.write_text('secret-key\n', encoding='utf-8')
    # pre-populate stale copy in target
    stale = target_home / '.lark-cli' / 'config.json'
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"appId": "stale"}\n', encoding='utf-8')
    stale_data = target_home / '.local' / 'share' / 'lark-cli' / 'master.key'
    stale_data.parent.mkdir(parents=True, exist_ok=True)
    stale_data.write_text('stale-key\n', encoding='utf-8')

    profile = ProviderProfileSpec(inherit_config=True, inherit_auth=False)
    layout = materialize_claude_home_config(target_home, source_home=source_home, profile=profile)

    assert not (layout.home_root / '.lark-cli').exists()
    assert not (layout.home_root / '.local' / 'share' / 'lark-cli').exists()


def test_materialize_claude_home_config_removes_stale_extra_config_on_disable(tmp_path: Path) -> None:
    source_home = tmp_path / 'system-home'
    target_home = tmp_path / 'managed-home'
    lark_cli = source_home / '.lark-cli' / 'config.json'
    lark_cli.parent.mkdir(parents=True, exist_ok=True)
    lark_cli.write_text('{"appId": "test"}\n', encoding='utf-8')
    lark_data = source_home / '.local' / 'share' / 'lark-cli' / 'master.key'
    lark_data.parent.mkdir(parents=True, exist_ok=True)
    lark_data.write_text('secret-key\n', encoding='utf-8')

    # First: sync with full inheritance
    profile_enabled = ProviderProfileSpec(inherit_config=True, inherit_auth=True)
    layout = materialize_claude_home_config(target_home, source_home=source_home, profile=profile_enabled)
    assert (layout.home_root / '.lark-cli' / 'config.json').read_text(encoding='utf-8') == '{"appId": "test"}\n'
    assert (layout.home_root / '.local' / 'share' / 'lark-cli' / 'master.key').read_text(encoding='utf-8') == 'secret-key\n'

    # Then: disable auth and re-materialize
    profile_disabled = ProviderProfileSpec(inherit_config=True, inherit_auth=False)
    materialize_claude_home_config(target_home, source_home=source_home, profile=profile_disabled)

    assert not (layout.home_root / '.lark-cli').exists()
    assert not (layout.home_root / '.local' / 'share' / 'lark-cli').exists()
