# Auto-build WinServer package after commits that touch app/runtime sources.
# Install: powershell -ExecutionPolicy Bypass -File scripts\install_auto_build_hook.ps1
# Skip once: $env:SKIP_BUILD=1; git commit ...
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $Root

if ($env:SKIP_BUILD -eq "1") {
    Write-Host "[auto-build] SKIP_BUILD=1, skip"
    exit 0
}

$changed = @(git diff-tree --no-commit-id --name-only -r HEAD 2>$null)
$need = $false
$patterns = @(
    '^ui\.py$',
    '^vps\.py$',
    '^bohrium_',
    '^captcha_multi\.py$',
    '^paths\.py$',
    '^requirements\.txt$',
    '^BohriumVPS\.spec$',
    '^build_win\.ps1$',
    '^bypass/'
)
foreach ($f in $changed) {
    foreach ($p in $patterns) {
        if ($f -match $p) { $need = $true; break }
    }
    if ($need) { break }
}
if (-not $need) {
    Write-Host "[auto-build] no runtime sources changed, skip"
    exit 0
}

$logDir = Join-Path $Root "dist"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$log = Join-Path $logDir "auto_build.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath $log -Value "`n==== $stamp auto-build start (commit $(git rev-parse --short HEAD)) ===="

# Background so git commit is not blocked for ~10 minutes
$ps = Join-Path $Root "build_win.ps1"
$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$ps`""
Start-Process -FilePath "powershell.exe" -ArgumentList $arg -WorkingDirectory $Root `
    -WindowStyle Minimized -RedirectStandardOutput $log -RedirectStandardError $log -NoNewWindow:$false | Out-Null
Write-Host "[auto-build] started in background -> $log"
Write-Host "[auto-build] output: dist\BohriumVPS\ and dist\BohriumVPS-WinServer.zip"
