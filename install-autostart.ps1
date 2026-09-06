<#
.SYNOPSIS
  Start the DDJ-200 bridge automatically at logon (no admin needed).

  Registers a per-user Scheduled Task (trigger: at logon, 15 s delay) that
  launches dist\DDJ200Bridge.exe if it has been built, otherwise bridge.py with
  pythonw.exe from the .venv next to this script. A scheduled task is more
  reliable than a HKCU Run entry on Windows 11, and the delay lets USB/audio
  devices enumerate first (the bridge copes either way).

  Any older HKCU Run entry from a previous version is removed.

.PARAMETER Remove
  Unregister the task (and the legacy Run entry).

.PARAMETER StartNow
  Also launch the bridge immediately.
#>
param(
    [switch]$Remove,
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$bridge   = Join-Path $here "bridge.py"
$taskName = "DDJ200Bridge"
$runKey   = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

# Legacy Run entry (v1 of this script) - always clear it.
if (Get-ItemProperty -Path $runKey -Name $taskName -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $runKey -Name $taskName
    Write-Host "Removed legacy Run entry."
}

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed scheduled task '$taskName'."
    } else {
        Write-Host "No scheduled task found."
    }
    exit 0
}

$exe = Join-Path $here "dist\DDJ200Bridge.exe"
if (Test-Path $exe) {
    $launcher   = $exe
    $launchArgs = $null
} else {
    $pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path $pythonw)) {
        $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if ($null -eq $cmd) {
            Write-Error "Neither dist\DDJ200Bridge.exe nor pythonw.exe found. Build the exe or create the venv first (see README)."
        }
        $pythonw = $cmd.Source
    }
    $launcher   = $pythonw
    $launchArgs = "`"$bridge`""
}

if ($launchArgs) {
    $action = New-ScheduledTaskAction -Execute $launcher -Argument $launchArgs -WorkingDirectory $here
} else {
    $action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $here
}
$user     = "$env:USERDOMAIN\$env:USERNAME"
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT15S"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Scheduled task '$taskName' registered (at logon, 15 s delay):"
if ($launchArgs) { Write-Host "  $launcher $launchArgs" } else { Write-Host "  $launcher" }

if ($StartNow) {
    if (Get-Process DDJ200Bridge -ErrorAction SilentlyContinue) {
        Write-Host "Bridge already running."
    } else {
        Start-ScheduledTask -TaskName $taskName
        Write-Host "Bridge started via the task (look for the tray icon)."
    }
}
