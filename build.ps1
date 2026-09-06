<#
.SYNOPSIS
  Build the standalone executables with PyInstaller.

  dist\DDJ200Bridge.exe       windowed (tray only, no console) - use this for autostart
  dist\DDJ200Bridge-cli.exe   console build for --monitor / --list-ports / --flat / --console

  Requires the .venv created per README (pip install -r requirements.txt pyinstaller).
#>
$ErrorActionPreference = "Stop"
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { Write-Error "No .venv found. See README step 2." }

Set-Location $here
& $python -m PyInstaller --version | Out-Null
if (-not $?) { & $python -m pip install pyinstaller }

# Tray icon as .ico for the exe
& $python make_icon.py

$icon   = Join-Path $here "icon.ico"
$common = @("--onefile", "--clean", "--noconfirm", "--icon", $icon, "--distpath", "dist", "--workpath", "build", "--specpath", "build")

& $python -m PyInstaller @common --windowed --name DDJ200Bridge     bridge.py
& $python -m PyInstaller @common --console  --name DDJ200Bridge-cli bridge.py

Write-Host ""
Write-Host "Built:"
Get-ChildItem dist\*.exe | ForEach-Object { "  {0}  ({1:N1} MB)" -f $_.FullName, ($_.Length / 1MB) }
