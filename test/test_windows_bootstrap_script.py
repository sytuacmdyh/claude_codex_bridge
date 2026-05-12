from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PS1 = REPO_ROOT / "scripts" / "bootstrap-windows-test-env.ps1"


def test_windows_bootstrap_script_installs_expected_prerequisites() -> None:
    text = BOOTSTRAP_PS1.read_text(encoding="utf-8")

    assert "Git.Git" in text
    assert "Python.Python.3.12" in text
    assert "OpenJS.NodeJS.LTS" in text
    assert "Invoke-CCBInstall" in text
    assert "Test-CCBInstalled" in text
    assert "CCB already installed at $InstallPrefix" in text
    assert "deferring strict Python validation to install.ps1" in text
    assert "SkipCCSwitch" in text
    assert "Start-Transcript" in text
    assert "Bootstrap log:" in text
    assert 'Join-Path $script:BootstrapScriptDir "bootstrap-logs"' in text
    assert 'Join-Path $logsDir "bootstrap.log"' in text
    assert '$env:CCB_PYTHON_CMD = $workingPython' in text
    assert 'Add-PythonCandidate "py -3"' in text
    assert 'npm global bin prefix:' in text
    assert 'where codex => ' in text
    assert 'Windows Store alias ignored' in text
    assert 'InstallAllUsers=0 PrependPath=1 Include_launcher=1' in text
    assert 'Show-ProviderSummary' in text
    assert 'Show-PathDiagnostics' in text


def test_windows_bootstrap_script_installs_expected_provider_clis() -> None:
    text = BOOTSTRAP_PS1.read_text(encoding="utf-8")

    assert "@openai/codex" in text
    assert "@anthropic-ai/claude-code" in text
    assert "@google/gemini-cli" in text
    assert "opencode-ai" in text
    assert "https://api.github.com/repos/farion1231/cc-switch/releases/latest" in text
    assert 'CC-Switch-v*-Windows.msi' in text


def test_windows_install_script_prefers_discovered_real_python_over_store_alias() -> None:
    text = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")

    assert "Get-PythonCandidates" in text
    assert 'Add-Candidate "py -3"' in text
    assert '$env:CCB_PYTHON_CMD' in text
    assert "Get-PythonVersionInfo" in text
    assert "Test-IsWindowsStoreAliasPath" in text
    assert 'sys.executable' in text
    assert 'set `"PYTHON=$escapedPythonExecutable`"' in text


def test_windows_bootstrap_script_creates_four_provider_smoke_config() -> None:
    text = BOOTSTRAP_PS1.read_text(encoding="utf-8")

    assert "cmd,writer:codex;reviewer:claude,qa:gemini,ops:opencode" in text
    assert "scripts/bootstrap-windows-test-env.ps1" in text
    assert "'```powershell'" in text
    assert "'```'" in text
    assert 'ccswitch' in text
    assert 'bootstrap-logs' in text
