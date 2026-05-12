param(
  [Parameter(Position = 0)]
  [ValidateSet("install", "uninstall", "install-skills", "help")]
  [string]$Command = "help",
  [string]$InstallPrefix = "$env:LOCALAPPDATA\codex-dual",
  [switch]$Yes
)

# --- UTF-8 / BOM compatibility (Windows PowerShell 5.1) ---
# Keep this near the top so Chinese/emoji output is rendered correctly.
try {
  $script:utf8NoBom = [System.Text.UTF8Encoding]::new($false)
} catch {
  $script:utf8NoBom = [System.Text.Encoding]::UTF8
}
try { $OutputEncoding = $script:utf8NoBom } catch {}
try { [Console]::OutputEncoding = $script:utf8NoBom } catch {}
try { [Console]::InputEncoding = $script:utf8NoBom } catch {}
try { chcp 65001 | Out-Null } catch {}

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# Constants
$script:CCB_START_MARKER = "<!-- CCB_CONFIG_START -->"
$script:CCB_END_MARKER = "<!-- CCB_CONFIG_END -->"
$script:CCB_ROLES_START_MARKER = "<!-- CCB_ROLES_START -->"
$script:CCB_ROLES_END_MARKER = "<!-- CCB_ROLES_END -->"
$script:CCB_RUBRICS_START_MARKER = "<!-- REVIEW_RUBRICS_START -->"
$script:CCB_RUBRICS_END_MARKER = "<!-- REVIEW_RUBRICS_END -->"

$script:SCRIPTS_TO_LINK = @(
  "ccb",
  "ask", "autonew", "ctx-transfer"
)

$script:CLAUDE_MARKDOWN = @(
  # Old CCB command markdown removed; ask is the only installed CCB skill.
)

$script:LEGACY_SCRIPTS = @(
  "cast", "cast-w", "codex-ask", "codex-pending", "codex-ping",
  "claude-codex-dual", "claude_codex", "claude_ai", "claude_bridge"
)

# i18n support
function Get-CCBLang {
  $lang = $env:CCB_LANG
  if ($lang -in @("zh", "cn", "chinese")) { return "zh" }
  if ($lang -in @("en", "english")) { return "en" }
  # Auto-detect from system
  try {
    $culture = (Get-Culture).Name
    if ($culture -like "zh*") { return "zh" }
  } catch {}
  return "en"
}

$script:CCBLang = Get-CCBLang

function Get-Msg {
  param([string]$Key, [string]$Arg1 = "", [string]$Arg2 = "")
  $msgs = @{
    "install_complete" = @{ en = "Installation complete"; zh = "安装完成" }
    "uninstall_complete" = @{ en = "Uninstall complete"; zh = "卸载完成" }
    "python_old" = @{ en = "Python version too old: $Arg1"; zh = "Python 版本过旧: $Arg1" }
    "requires_python" = @{ en = "ccb requires Python 3.10+"; zh = "ccb 需要 Python 3.10+" }
    "confirm_windows" = @{ en = "Continue installation in Windows? (y/N)"; zh = "确认继续在 Windows 中安装？(y/N)" }
    "cancelled" = @{ en = "Installation cancelled"; zh = "安装已取消" }
    "windows_warning" = @{ en = "You are installing ccb in native Windows environment"; zh = "你正在 Windows 原生环境安装 ccb" }
    "same_env" = @{ en = "ccb/ask/ping/pend must run in the same environment as codex/gemini."; zh = "ccb/ask/ping/pend 必须与 codex/gemini 在同一环境运行。" }
  }
  if ($msgs.ContainsKey($Key)) {
    return $msgs[$Key][$script:CCBLang]
  }
  return $Key
}

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  .\install.ps1 install    # Install or update"
  Write-Host "  .\install.ps1 uninstall  # Uninstall"
  Write-Host "  .\install.ps1 install-skills  # Re-install skills only"
  Write-Host ""
  Write-Host "Options:"
  Write-Host "  -InstallPrefix <path>    # Custom install location (default: $env:LOCALAPPDATA\codex-dual)"
  Write-Host ""
  Write-Host "Requirements:"
  Write-Host "  - Python 3.10+"
}

function Test-IsWindowsStoreAliasPath {
  param([string]$PathText)
  if ([string]::IsNullOrWhiteSpace($PathText)) {
    return $false
  }
  $normalized = $PathText.Trim().Trim('"').ToLowerInvariant()
  return (
    $normalized -like "*\microsoft\windowsapps\python.exe" -or
    $normalized -like "*\microsoft\windowsapps\python3.exe"
  )
}

function Get-PythonVersionInfo {
  param([string]$PythonCmd)

  if ([string]::IsNullOrWhiteSpace($PythonCmd)) {
    throw "Python command is empty"
  }
  # Handle commands with arguments (e.g., "py -3")
  $cmdParts = $PythonCmd -split ' ', 2
  $fileName = $cmdParts[0]
  $baseArgs = if ($cmdParts.Length -gt 1) { $cmdParts[1] } else { "" }

  # Use ProcessStartInfo for reliable execution across different Python installations
  # (e.g., Miniconda, custom paths). The & operator can fail in some environments.
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $fileName
  try {
    # Combine base arguments with Python code arguments
    if ($baseArgs) {
      $psi.Arguments = "$baseArgs -c `"import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}|{v.major}|{v.minor}|{sys.executable}')`""
    } else {
      $psi.Arguments = "-c `"import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}|{v.major}|{v.minor}|{sys.executable}')`""
    }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    $process.Start() | Out-Null
    $process.WaitForExit()

    $vinfo = $process.StandardOutput.ReadToEnd().Trim()
    if ($process.ExitCode -ne 0 -or [string]::IsNullOrEmpty($vinfo)) {
      $stderr = $process.StandardError.ReadToEnd().Trim()
      if ([string]::IsNullOrWhiteSpace($stderr)) {
        $stderr = "exit code $($process.ExitCode)"
      }
      throw $stderr
    }

    $vparts = $vinfo -split "\|", 4
    if ($vparts.Length -lt 4) {
      throw "Unexpected version output: $vinfo"
    }

    $version = $vparts[0]
    $major = [int]$vparts[1]
    $minor = [int]$vparts[2]
    return @{
      Command = $PythonCmd
      Version = $version
      Major = $major
      Minor = $minor
      Executable = $vparts[3]
    }
  } catch {
    $errorText = $_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($errorText)) {
      $errorText = [string]$_
    }
    throw "Failed to query Python version using '$PythonCmd': $errorText"
  }
}

function Get-PythonCandidates {
  $candidates = New-Object System.Collections.Generic.List[string]

  function Add-Candidate {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return }
    $trimmed = $Value.Trim()
    if (Test-IsWindowsStoreAliasPath $trimmed) { return }
    if ($candidates -notcontains $trimmed) {
      $candidates.Add($trimmed)
    }
  }

  Add-Candidate $env:CCB_PYTHON_CMD
  Add-Candidate "py -3"
  Add-Candidate "python"
  Add-Candidate "python3"

  try {
    $wherePython = & where.exe python 2>$null
    foreach ($item in @($wherePython)) {
      $candidatePath = [string]$item
      if ([string]::IsNullOrWhiteSpace($candidatePath)) { continue }
      Add-Candidate $candidatePath.Trim()
    }
  } catch {}

  $globPatterns = @(
    "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
    "$env:ProgramFiles\Python*\python.exe",
    "$env:ProgramFiles\Python\Python*\python.exe",
    "$env:ProgramFiles(x86)\Python*\python.exe",
    "$env:ProgramFiles(x86)\Python\Python*\python.exe"
  )
  foreach ($pattern in $globPatterns) {
    try {
      Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | ForEach-Object {
        Add-Candidate $_.FullName
      }
    } catch {}
  }

  return @($candidates)
}

function Find-Python {
  foreach ($candidate in Get-PythonCandidates) {
    try {
      $info = Get-PythonVersionInfo -PythonCmd $candidate
      if (
        $info.Major -eq 3 -and
        $info.Minor -ge 10 -and
        -not (Test-IsWindowsStoreAliasPath $info.Executable)
      ) {
        return $candidate
      }
    } catch {}
  }
  return $null
}

function Require-Python310 {
  param([string]$PythonCmd)

  try {
    $info = Get-PythonVersionInfo -PythonCmd $PythonCmd
  } catch {
    Write-Host "[ERROR] $($_.Exception.Message)"
    exit 1
  }

  if (($info.Major -ne 3) -or ($info.Minor -lt 10)) {
    Write-Host "[ERROR] Python version too old: $($info.Version)"
    Write-Host "   ccb requires Python 3.10+"
    Write-Host "   Download: https://www.python.org/downloads/"
    exit 1
  }
  Write-Host "[OK] Python $($info.Version) ($($info.Executable))"
}

function Confirm-BackendEnv {
  if ($Yes -or $env:CCB_INSTALL_ASSUME_YES -eq "1") { return }

  if (-not [Environment]::UserInteractive) {
    Write-Host "[ERROR] Non-interactive environment detected, aborting to prevent Windows/WSL mismatch."
    Write-Host "   If codex/gemini will run in native Windows:"
    Write-Host "   Re-run: powershell -ExecutionPolicy Bypass -File .\install.ps1 install -Yes"
    exit 1
  }

  Write-Host ""
  Write-Host "================================================================"
  Write-Host "[WARNING] You are installing ccb in native Windows environment"
  Write-Host "================================================================"
  Write-Host "ccb/ask/ping/pend must run in the same environment as codex/gemini."
  Write-Host ""
  Write-Host "Please confirm: You will install and run codex/gemini in native Windows (not WSL)."
  Write-Host "If you plan to run codex/gemini in WSL, exit and run in WSL:"
  Write-Host "   ./install.sh install"
  Write-Host "================================================================"
  $reply = Read-Host "Continue installation in Windows? (y/N)"
  if ($reply.Trim().ToLower() -notin @("y", "yes")) {
    Write-Host "Installation cancelled"
    exit 1
  }
}

function Install-Native {
  Confirm-BackendEnv

  $binDir = Join-Path $InstallPrefix "bin"
  $pythonCmd = Find-Python

  if (-not $pythonCmd) {
    Write-Host "Python not found. Please install Python and add it to PATH."
    Write-Host "Download: https://www.python.org/downloads/"
    exit 1
  }

  Require-Python310 -PythonCmd $pythonCmd

  Write-Host "Installing ccb to $InstallPrefix ..."
  Write-Host "Using Python: $pythonCmd"
  $pythonInfo = Get-PythonVersionInfo -PythonCmd $pythonCmd
  $pythonExecutable = $pythonInfo.Executable
  if ([string]::IsNullOrWhiteSpace($pythonExecutable)) {
    Write-Host "[ERROR] Failed to resolve a concrete Python executable."
    exit 1
  }

  $cleanInstall = $false
  $cleanEnv = ($env:CCB_CLEAN_INSTALL -as [string])
  if ($cleanEnv -and $cleanEnv.Trim() -notin @("0", "false", "no", "off")) {
    $cleanInstall = $true
  }
  if ($cleanInstall -and (Test-Path $InstallPrefix)) {
    $repoRootResolved = $repoRoot
    $installResolved = $InstallPrefix
    try { $repoRootResolved = (Resolve-Path $repoRoot).Path } catch {}
    try { $installResolved = (Resolve-Path $InstallPrefix).Path } catch {}
    if ($repoRootResolved -ne $installResolved) {
      Remove-Item -Recurse -Force $InstallPrefix
    }
  }

  if (-not (Test-Path $InstallPrefix)) {
    New-Item -ItemType Directory -Path $InstallPrefix -Force | Out-Null
  }
  if (-not (Test-Path $binDir)) {
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
  }

  $items = @("ccb", "lib", "bin", "commands", "mcp", "droid_skills")
  foreach ($item in $items) {
    $src = Join-Path $repoRoot $item
    $dst = Join-Path $InstallPrefix $item
    if (Test-Path $src) {
      if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
      Copy-Item -Recurse -Force $src $dst
    }
  }

  # Exclude web UI code from installation (CLI-only mail setup)
  $webDir = Join-Path $InstallPrefix "lib\\web"
  if (Test-Path $webDir) { Remove-Item -Recurse -Force $webDir }
  $ccbWeb = Join-Path $InstallPrefix "bin\\ccb-web"
  if (Test-Path $ccbWeb) { Remove-Item -Force $ccbWeb }

  function Fix-PythonShebang {
    param([string]$TargetPath)
    if (-not $TargetPath -or -not (Test-Path $TargetPath)) { return }
    try {
      $text = [System.IO.File]::ReadAllText($TargetPath, [System.Text.Encoding]::UTF8)
      if ($text -match '^\#\!/usr/bin/env python3') {
        $text = $text -replace '^\#\!/usr/bin/env python3', '#!/usr/bin/env python'
        [System.IO.File]::WriteAllText($TargetPath, $text, $script:utf8NoBom)
      }
    } catch {
      return
    }
  }

  $scripts = @(
    "ccb",
    "ask", "autonew", "ctx-transfer"
  )

  # In MSYS/Git-Bash, invoking the script file directly will honor the shebang.
  # Windows typically has `python` but not `python3`, so rewrite shebangs for compatibility.
  foreach ($script in $scripts) {
    if ($script -eq "ccb") {
      Fix-PythonShebang (Join-Path $InstallPrefix "ccb")
    } else {
      Fix-PythonShebang (Join-Path $InstallPrefix ("bin\\" + $script))
    }
  }

  foreach ($script in $scripts) {
    $batPath = Join-Path $binDir "$script.bat"
    $cmdPath = Join-Path $binDir "$script.cmd"
    if ($script -eq "ccb") {
      $relPath = "..\\ccb"
    } else {
      # Script is installed alongside the wrapper under $InstallPrefix\bin
      $relPath = $script
    }
    $escapedPythonExecutable = $pythonExecutable.Replace('"', '""')
    $wrapperContent = "@echo off`r`nset `"PYTHON=$escapedPythonExecutable`"`r`nif not exist `"%PYTHON%`" set `"PYTHON=python`"`r`nwhere python >NUL 2>&1 || if /I `"%PYTHON%`"==`"python`" set `"PYTHON=py -3`"`r`n%PYTHON% `"%~dp0$relPath`" %*"
    [System.IO.File]::WriteAllText($batPath, $wrapperContent, $script:utf8NoBom)
    # .cmd wrapper for PowerShell/CMD users (and tools preferring .cmd over raw shebang scripts)
    [System.IO.File]::WriteAllText($cmdPath, $wrapperContent, $script:utf8NoBom)
  }

  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $pathList = if ($userPath) { $userPath -split ";" | Where-Object { $_ } } else { @() }
  $binDirLower = $binDir.ToLower()
  $alreadyInPath = $pathList | Where-Object { $_.ToLower() -eq $binDirLower }
  if (-not $alreadyInPath) {
    $newPath = ($pathList + $binDir) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added $binDir to user PATH"
  }

  # Git version injection
  function Get-GitVersionInfo {
    param([string]$RepoRoot)

    $commit = ""
    $date = ""

    # 方法1: 本地 Git
    if (Get-Command git -ErrorAction SilentlyContinue) {
      if (Test-Path (Join-Path $RepoRoot ".git")) {
        try {
          $commit = (git -C $RepoRoot log -1 --format='%h' 2>$null)
          $date = (git -C $RepoRoot log -1 --format='%cs' 2>$null)
        } catch {}
      }
    }

    # 方法2: 环境变量
    if (-not $commit -and $env:CCB_GIT_COMMIT) {
      $commit = $env:CCB_GIT_COMMIT
      $date = $env:CCB_GIT_DATE
    }

    # 方法3: GitHub API
    if (-not $commit) {
      try {
        $api = "https://api.github.com/repos/bfly123/claude_code_bridge/commits/main"
        $response = Invoke-RestMethod -Uri $api -TimeoutSec 5 -ErrorAction Stop
        $commit = $response.sha.Substring(0,7)
        $date = $response.commit.committer.date.Substring(0,10)
      } catch {}
    }

    return @{Commit=$commit; Date=$date}
  }

  # 注入版本信息到 ccb 文件
  $verInfo = Get-GitVersionInfo -RepoRoot $repoRoot
  if ($verInfo.Commit) {
    $ccbPath = Join-Path $InstallPrefix "ccb"
    if (Test-Path $ccbPath) {
      try {
        $content = Get-Content $ccbPath -Raw -Encoding UTF8
        $content = $content -replace 'GIT_COMMIT = ""', "GIT_COMMIT = `"$($verInfo.Commit)`""
        $content = $content -replace 'GIT_DATE = ""', "GIT_DATE = `"$($verInfo.Date)`""
        [System.IO.File]::WriteAllText($ccbPath, $content, [System.Text.UTF8Encoding]::new($false))
        Write-Host "Injected version info: $($verInfo.Commit) $($verInfo.Date)"
      } catch {
        Write-Warning "Failed to inject version info: $_"
      }
    }
  }
  Install-CodexSkills
  Install-ClaudeConfig
  Install-DroidSkills
  Install-DroidDelegation -PythonCmd $pythonCmd -InstallPrefix $InstallPrefix
  Cleanup-LegacyFiles -InstallPrefix $InstallPrefix

  Write-Host ""
  Write-Host "Installation complete!"
  Write-Host "Restart your terminal for PATH changes to take effect."
  Write-Host ""
  Write-Host "Quick start:"
  Write-Host "  ccb             # Start providers from ccb.config (default: all four)"
  Write-Host "  ccb codex       # Start with Codex backend"
  Write-Host "  ccb gemini      # Start with Gemini backend"
  Write-Host "  ccb opencode    # Start with OpenCode backend"
  Write-Host "  ccb claude      # Start with Claude backend"
}

# Clean up legacy daemon files from the pre-ccbd era
function Cleanup-LegacyFiles {
  param([string]$InstallPrefix)

  Write-Host "Cleaning up legacy files..."
  $cleaned = 0

  # Legacy daemon scripts in bin/
  $legacyDaemons = @("caskd", "gaskd", "oaskd", "laskd", "daskd")
  $binDir = Join-Path $InstallPrefix "bin"

  foreach ($daemon in $legacyDaemons) {
    $daemonPath = Join-Path $binDir $daemon
    if (Test-Path $daemonPath) {
      Remove-Item -Force $daemonPath
      Write-Host "  Removed legacy daemon script: $daemonPath"
      $cleaned++
    }
  }

  # Legacy daemon state files in cache
  $cacheDir = Join-Path $env:LOCALAPPDATA "ccb"
  $legacyStates = @("caskd.json", "gaskd.json", "oaskd.json", "laskd.json", "daskd.json")

  foreach ($state in $legacyStates) {
    $statePath = Join-Path $cacheDir $state
    if (Test-Path $statePath) {
      Remove-Item -Force $statePath
      Write-Host "  Removed legacy state file: $statePath"
      $cleaned++
    }
  }

  # Legacy daemon module files in lib/
  $libDir = Join-Path $InstallPrefix "lib"
  $legacyModules = @("caskd_daemon.py", "gaskd_daemon.py", "oaskd_daemon.py", "laskd_daemon.py", "daskd_daemon.py")

  foreach ($module in $legacyModules) {
    $modulePath = Join-Path $libDir $module
    if (Test-Path $modulePath) {
      Remove-Item -Force $modulePath
      Write-Host "  Removed legacy module: $modulePath"
      $cleaned++
    }
  }

  if ($cleaned -eq 0) {
    Write-Host "  No legacy files found"
  } else {
    Write-Host "  Cleaned up $cleaned legacy file(s)"
  }
}

function Install-ClaudeSkills {
  $claudeDir = Join-Path $env:USERPROFILE ".claude"
  $skillsDir = Join-Path $claudeDir "skills"
  $srcSkills = Join-Path $repoRoot "claude_skills"

  if (-not (Test-Path $srcSkills)) {
    return
  }

  if (-not (Test-Path $claudeDir)) {
    New-Item -ItemType Directory -Path $claudeDir -Force | Out-Null
  }
  if (-not (Test-Path $skillsDir)) {
    New-Item -ItemType Directory -Path $skillsDir -Force | Out-Null
  }

  $deprecatedSkills = @(
    "bask", "bpend", "bping", "cask", "cpend", "cping", "dask", "dpend", "dping",
    "gask", "gpend", "gping", "hask", "hpend", "hping", "lask", "lpend", "lping",
    "mounted", "oask", "opend", "oping", "qask", "qpend", "qping",
    "auto", "ping", "pend", "autonew", "all-plan", "docs", "tp", "tr", "file-op", "review", "continue"
  )
  foreach ($skill in $deprecatedSkills) {
    $skillPath = Join-Path $skillsDir $skill
    if (Test-Path $skillPath) {
      Remove-Item -Recurse -Force $skillPath
      Write-Host "  Removed obsolete skill: $skill"
    }
  }

  Write-Host "Installing Claude ask skill (PowerShell SKILL.md template)..."
  Get-ChildItem -Path $srcSkills -Directory | Where-Object { $_.Name -eq "ask" } | ForEach-Object {

    $skillName = $_.Name
    $srcDir = $_.FullName
    $dstDir = Join-Path $skillsDir $skillName
    $dstSkillMd = Join-Path $dstDir "SKILL.md"

    # Remove and recreate to clean stale files
    if (Test-Path $dstDir) {
      Remove-Item -Recurse -Force $dstDir
    }
    New-Item -ItemType Directory -Path $dstDir -Force | Out-Null

    $srcSkillMd = Join-Path $srcDir "SKILL.md.powershell"
    if (-not (Test-Path $srcSkillMd)) {
      $srcSkillMd = Join-Path $srcDir "SKILL.md"
    }
    if (-not (Test-Path $srcSkillMd)) {
      return
    }

    Copy-Item -Force $srcSkillMd $dstSkillMd

    # Copy additional subdirectories (e.g., references/) if they exist
    Get-ChildItem -Path $srcDir -Directory | ForEach-Object {
      $subDirName = $_.Name
      $srcSubDir = $_.FullName
      $dstSubDir = Join-Path $dstDir $subDirName
      Copy-Item -Recurse -Force $srcSubDir $dstSubDir
    }

    Write-Host "  Updated skill: $skillName"
  }
  Write-Host "Updated Claude skills directory: $skillsDir"
}

function Install-CodexSkills {
  $skillsSrc = Join-Path $repoRoot "codex_skills"
  $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
  $skillsDst = Join-Path $codexHome "skills"

  if (-not (Test-Path $skillsSrc)) {
    return
  }

  if (-not (Test-Path $skillsDst)) {
    New-Item -ItemType Directory -Path $skillsDst -Force | Out-Null
  }

  $deprecatedSkills = @("ping", "pend", "autonew", "all-plan", "file-op")
  foreach ($skill in $deprecatedSkills) {
    $skillPath = Join-Path $skillsDst $skill
    if (Test-Path $skillPath) {
      Remove-Item -Recurse -Force $skillPath
      Write-Host "  Removed obsolete skill: $skill"
    }
  }

  Write-Host "Installing Codex ask skill (PowerShell SKILL.md template)..."
  Get-ChildItem -Path $skillsSrc -Directory | Where-Object { $_.Name -eq "ask" } | ForEach-Object {
    $skillName = $_.Name
    $srcDir = $_.FullName
    $dstDir = Join-Path $skillsDst $skillName
    $dstSkillMd = Join-Path $dstDir "SKILL.md"

    # Remove and recreate to clean stale files
    if (Test-Path $dstDir) {
      Remove-Item -Recurse -Force $dstDir
    }
    New-Item -ItemType Directory -Path $dstDir -Force | Out-Null

    $srcSkillMd = Join-Path $srcDir "SKILL.md.powershell"
    if (-not (Test-Path $srcSkillMd)) {
      $srcSkillMd = Join-Path $srcDir "SKILL.md"
    }
    if (-not (Test-Path $srcSkillMd)) {
      return
    }

    Copy-Item -Force $srcSkillMd $dstSkillMd

    # Copy additional subdirectories (e.g., references/) if they exist
    Get-ChildItem -Path $srcDir -Directory | ForEach-Object {
      $subDirName = $_.Name
      $srcSubDir = $_.FullName
      $dstSubDir = Join-Path $dstDir $subDirName
      Copy-Item -Recurse -Force $srcSubDir $dstSubDir
    }

    Write-Host "  Updated Codex skill: $skillName"
  }
  Write-Host "Updated Codex skills directory: $skillsDst"
}

function Install-DroidSkills {
  $skillsSrc = Join-Path $repoRoot "droid_skills"
  $factoryHome = if ($env:FACTORY_HOME) { $env:FACTORY_HOME } else { Join-Path $env:USERPROFILE ".factory" }
  $skillsDst = Join-Path $factoryHome "skills"

  if (-not (Test-Path $skillsSrc)) {
    return
  }

  if (-not (Get-Command droid -ErrorAction SilentlyContinue)) {
    return
  }

  if (-not (Test-Path $skillsDst)) {
    New-Item -ItemType Directory -Path $skillsDst -Force | Out-Null
  }

  $deprecatedSkills = @("ping", "pend", "autonew", "all-plan")
  foreach ($skill in $deprecatedSkills) {
    $skillPath = Join-Path $skillsDst $skill
    if (Test-Path $skillPath) {
      Remove-Item -Recurse -Force $skillPath
      Write-Host "  Removed obsolete skill: $skill"
    }
  }

  Write-Host "Installing Droid/Factory ask skill..."
  Get-ChildItem -Path $skillsSrc -Directory | Where-Object { $_.Name -eq "ask" } | ForEach-Object {
    $skillName = $_.Name
    $srcDir = $_.FullName
    $dstDir = Join-Path $skillsDst $skillName

    $srcSkillMd = Join-Path $srcDir "SKILL.md"
    if (-not (Test-Path $srcSkillMd)) {
      return
    }

    # Remove and recreate to clean stale files
    if (Test-Path $dstDir) {
      Remove-Item -Recurse -Force $dstDir
    }
    New-Item -ItemType Directory -Path $dstDir -Force | Out-Null

    Copy-Item -Force $srcSkillMd (Join-Path $dstDir "SKILL.md")

    # Copy additional subdirectories
    Get-ChildItem -Path $srcDir -Directory | ForEach-Object {
      Copy-Item -Recurse -Force $_.FullName (Join-Path $dstDir $_.Name)
    }

    Write-Host "  Updated Factory skill: $skillName"
  }
  Write-Host "Updated Factory skills directory: $skillsDst"
}

function Install-DroidDelegation {
  param(
    [string]$PythonCmd,
    [string]$InstallPrefix
  )

  if ($env:CCB_DROID_AUTOINSTALL -eq "0") {
    return
  }
  $droidCmd = Get-Command droid -ErrorAction SilentlyContinue
  if (-not $droidCmd) {
    return
  }
  $serverPath = Join-Path $InstallPrefix "mcp\\ccb-delegation\\server.py"
  if (-not (Test-Path $serverPath)) {
    Write-Host "WARN: Droid MCP server not found at $serverPath; skipping"
    return
  }
  if ($env:CCB_DROID_AUTOINSTALL_FORCE -eq "1") {
    try { & $droidCmd.Source "mcp" "remove" "ccb-delegation" | Out-Null } catch {}
  }
  try {
    & $droidCmd.Source "mcp" "add" "ccb-delegation" "--type" "stdio" $PythonCmd $serverPath | Out-Null
    Write-Host "OK: Droid MCP delegation registered"
  } catch {
    Write-Warning "Droid MCP delegation setup failed: $_"
  }
}

function Install-ClaudeConfig {
  $claudeDir = Join-Path $env:USERPROFILE ".claude"
  $commandsDir = Join-Path $claudeDir "commands"
  $settingsJson = Join-Path $claudeDir "settings.json"

  if (-not (Test-Path $claudeDir)) {
    New-Item -ItemType Directory -Path $claudeDir -Force | Out-Null
  }
  if (-not (Test-Path $commandsDir)) {
    New-Item -ItemType Directory -Path $commandsDir -Force | Out-Null
  }

  $srcCommands = Join-Path $repoRoot "commands"
  if (Test-Path $srcCommands) {
    Get-ChildItem -Path $srcCommands -Filter "*.md" | ForEach-Object {
      Copy-Item -Force $_.FullName (Join-Path $commandsDir $_.Name)
    }
  }

  # Install skills
  Install-ClaudeSkills

  Remove-CCBMemoryInjections

  $allowList = @(
    "Bash(ccb ask *)", "Bash(ccb ping *)", "Bash(ccb pend *)"
  )

  if (Test-Path $settingsJson) {
    try {
      $settings = Get-Content -Raw $settingsJson | ConvertFrom-Json
    } catch {
      $settings = @{}
    }
  } else {
    $settings = @{}
  }

  if (-not $settings.permissions) {
    $settings | Add-Member -NotePropertyName "permissions" -NotePropertyValue @{} -Force
  }
  if (-not $settings.permissions.allow) {
    $settings.permissions | Add-Member -NotePropertyName "allow" -NotePropertyValue @() -Force
  }

  $currentAllow = [System.Collections.ArrayList]@($settings.permissions.allow)
  $updated = $false
  foreach ($item in $allowList) {
    if ($currentAllow -notcontains $item) {
      $currentAllow.Add($item) | Out-Null
      $updated = $true
    }
  }

  if ($updated) {
    $settings.permissions.allow = $currentAllow.ToArray()
    $settings | ConvertTo-Json -Depth 10 | Out-File -Encoding UTF8 -FilePath $settingsJson
    Write-Host "Updated settings.json with permissions"
  }

}

function Remove-MarkedMemoryBlock {
  param(
    [string]$Path,
    [string]$StartMarker,
    [string]$EndMarker,
    [string]$Label
  )
  if (-not (Test-Path $Path)) { return }
  $content = Get-Content -Raw $Path -Encoding UTF8
  if (-not $content.Contains($StartMarker)) { return }
  $pattern = "(?s)\r?\n?$([regex]::Escape($StartMarker)).*?$([regex]::Escape($EndMarker))\r?\n?"
  $content = [regex]::Replace($content, $pattern, "`n").Trim() + "`n"
  [System.IO.File]::WriteAllText($Path, $content, $script:utf8NoBom)
  Write-Host "Removed CCB memory block from $Label"
}

function Remove-CCBMemoryInjections {
  $claudeMd = Join-Path $env:USERPROFILE ".claude\CLAUDE.md"
  Remove-MarkedMemoryBlock -Path $claudeMd -StartMarker $script:CCB_START_MARKER -EndMarker $script:CCB_END_MARKER -Label "CLAUDE.md"

  $agentsMd = Join-Path $installPrefix "AGENTS.md"
  Remove-MarkedMemoryBlock -Path $agentsMd -StartMarker $script:CCB_ROLES_START_MARKER -EndMarker $script:CCB_ROLES_END_MARKER -Label "AGENTS.md"
  Remove-MarkedMemoryBlock -Path $agentsMd -StartMarker $script:CCB_RUBRICS_START_MARKER -EndMarker $script:CCB_RUBRICS_END_MARKER -Label "AGENTS.md"

  $clinerules = Join-Path $installPrefix ".clinerules"
  Remove-MarkedMemoryBlock -Path $clinerules -StartMarker $script:CCB_ROLES_START_MARKER -EndMarker $script:CCB_ROLES_END_MARKER -Label ".clinerules"
}

function Uninstall-Native {
  $binDir = Join-Path $InstallPrefix "bin"

  # 1. Remove project directory
  if (Test-Path $InstallPrefix) {
    Remove-Item -Recurse -Force $InstallPrefix
    Write-Host "Removed $InstallPrefix"
  }

  # 2. Remove from user PATH
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($userPath) {
    $pathList = $userPath -split ";" | Where-Object { $_ }
    $binDirLower = $binDir.ToLower()
    $newPathList = $pathList | Where-Object { $_.ToLower() -ne $binDirLower }
    if ($newPathList.Count -ne $pathList.Count) {
      $newPath = $newPathList -join ";"
      [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
      Write-Host "Removed $binDir from user PATH"
    }
  }

  # 3. Remove Claude skills
  $claudeSkillsDir = Join-Path $env:USERPROFILE ".claude\skills"
  $ccbSkills = @("ask", "ping", "pend", "autonew", "all-plan", "docs", "tp", "tr", "file-op", "review", "continue")
  if (Test-Path $claudeSkillsDir) {
    Write-Host "Removing CCB Claude skills..."
    foreach ($skill in $ccbSkills) {
      $skillPath = Join-Path $claudeSkillsDir $skill
      if (Test-Path $skillPath) {
        Remove-Item -Recurse -Force $skillPath
        Write-Host "  Removed skill: $skill"
      }
    }
  }

  # 4. Remove CLAUDE.md CCB config block
  $claudeMd = Join-Path $env:USERPROFILE ".claude\CLAUDE.md"
  if (Test-Path $claudeMd) {
    $content = Get-Content $claudeMd -Raw -Encoding UTF8
    if ($content -match $script:CCB_START_MARKER) {
      Write-Host "Removing CCB config from CLAUDE.md..."
      $pattern = "(?s)$([regex]::Escape($script:CCB_START_MARKER)).*?$([regex]::Escape($script:CCB_END_MARKER))\r?\n?"
      $content = $content -replace $pattern, ""
      $content = $content.Trim() + "`n"
      [System.IO.File]::WriteAllText($claudeMd, $content, $script:utf8NoBom)
      Write-Host "  Removed CCB config block"
    }
  }

  # 5. Remove settings.json permissions
  $settingsFile = Join-Path $env:USERPROFILE ".claude\settings.json"
  if (Test-Path $settingsFile) {
    $permsToRemove = @(
      "Bash(ccb ask *)", "Bash(ccb ping *)", "Bash(ccb pend *)",
      "Bash(ask:*)", "Bash(ping:*)", "Bash(ccb-ping:*)", "Bash(pend:*)"
    )
    try {
      $settings = Get-Content $settingsFile -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($settings.permissions -and $settings.permissions.allow) {
        $originalCount = $settings.permissions.allow.Count
        $settings.permissions.allow = @($settings.permissions.allow | Where-Object { $_ -notin $permsToRemove })
        if ($settings.permissions.allow.Count -ne $originalCount) {
          $settings | ConvertTo-Json -Depth 10 | Set-Content $settingsFile -Encoding UTF8
          Write-Host "Removed CCB permissions from settings.json"
        }
      }
    } catch {
      Write-Host "WARN: Could not clean settings.json: $_"
    }
  }

  # 6. Remove Codex skills
  $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
  $codexSkillsDir = Join-Path $codexHome "skills"
  if (Test-Path $codexSkillsDir) {
    Write-Host "Removing CCB Codex skills..."
    foreach ($skill in $ccbSkills) {
      $skillPath = Join-Path $codexSkillsDir $skill
      if (Test-Path $skillPath) {
        Remove-Item -Recurse -Force $skillPath
        Write-Host "  Removed skill: $skill"
      }
    }
  }

  # 7. Remove Droid skills
  $factoryHome = if ($env:FACTORY_HOME) { $env:FACTORY_HOME } else { Join-Path $env:USERPROFILE ".factory" }
  $droidSkillsDir = Join-Path $factoryHome "skills"
  if (Test-Path $droidSkillsDir) {
    Write-Host "Removing CCB Droid skills..."
    foreach ($skill in $ccbSkills) {
      $skillPath = Join-Path $droidSkillsDir $skill
      if (Test-Path $skillPath) {
        Remove-Item -Recurse -Force $skillPath
        Write-Host "  Removed skill: $skill"
      }
    }
  }

  Write-Host "Uninstall complete."
}

if ($Command -eq "help") {
  Show-Usage
  exit 0
}

if ($Command -eq "install") {
  Install-Native
  exit 0
}

if ($Command -eq "uninstall") {
  Uninstall-Native
  exit 0
}

if ($Command -eq "install-skills") {
  Install-ClaudeSkills
  Install-CodexSkills
  Install-DroidSkills
  exit 0
}
