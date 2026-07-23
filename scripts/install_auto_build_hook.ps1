# Install git post-commit hook: rebuild exe after relevant commits.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$hookDir = Join-Path $Root ".git\hooks"
if (-not (Test-Path -LiteralPath $hookDir)) {
    throw "Not a git repo or missing .git/hooks: $hookDir"
}
$hook = Join-Path $hookDir "post-commit"
$buildScript = Join-Path $Root "scripts\post-commit-build.ps1"
# Git for Windows runs hooks via sh; call PowerShell.
$hookBody = @"
#!/bin/sh
# Auto-build BohriumVPS.exe after commit (relevant sources only).
# Skip: SKIP_BUILD=1 git commit ...
ROOT="`$(git rev-parse --show-toplevel)"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "`$ROOT/scripts/post-commit-build.ps1" || true
"@
# Use LF for shell hook
$hookBody = $hookBody -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($hook, $hookBody)
Write-Host "Installed: $hook"
Write-Host "On commit (if ui/vps/bohrium/bypass/... changed) -> background build_win.ps1"
Write-Host "Skip: `$env:SKIP_BUILD=1; git commit ..."
Write-Host "Log: dist\auto_build.log"
