# Build standalone package for Windows Server (no Python required on target).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

Write-Host "==> Install build deps"
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

Write-Host "==> Clean old build"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue dist, build

Write-Host "==> PyInstaller (onedir, windowed)"
python -m PyInstaller --noconfirm --clean BohriumVPS.spec

$Out = Join-Path $Root "dist\BohriumVPS"
$Exe = Join-Path $Out "BohriumVPS.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Build failed: BohriumVPS.exe not found"
}

$Readme = Join-Path $Out "README.txt"
$lines = @(
    "Bohrium VPS Standalone for Windows Server 2019/2022/2025"
    ""
    "Run: double-click BohriumVPS.exe (no Python install needed)"
    ""
    "Features:"
    "  - count / workers"
    "  - finite expand: child mine only"
    "  - infinite expand: each layer keeps spawning"
    "  - SKU high-to-low fallback"
    "  - schedule timer (e.g. every 30 minutes)"
    "  - progress / success rate / logs"
    "  - config saved as ui_config.json"
    ""
    "Data next to exe:"
    "  ui_config.json"
    "  vps_result.json"
    "  vps_runs\"
    ""
    "Network: need access to platform.bohrium.com / www.bohrium.com"
)
$lines | Set-Content -LiteralPath $Readme -Encoding UTF8

Write-Host "==> Done: $Out"
Write-Host "Copy the whole BohriumVPS folder to the server and run BohriumVPS.exe"
