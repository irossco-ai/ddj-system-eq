<#
.SYNOPSIS
  Switch Equalizer APO on an output device from its default SFX/EFX install
  slots to the older LFX/GFX slots (what Configurator's "Troubleshooting
  options" does). Some drivers - Focusrite USB is a known one - only run
  effects from the LFX/GFX slots, so APO sits registered but never loads.
  Also enables APO's trace log and restarts the Windows Audio service.
  RUN AS ADMINISTRATOR.

  Quick test for whether you need this: Equalizer APO is ticked for the
  device in Configurator, yet writing "Preamp: -60 dB" into a config file
  changes nothing.

.PARAMETER Device
  Part of the output device's name as shown in Windows Sound settings
  (e.g. "Speakers", "Scarlett"). If it matches several active devices, or
  none, the script lists them and exits.
.PARAMETER List
  Just list active output devices with their current APO slots.
.PARAMETER Revert
  Put the SFX/EFX layout back and disable tracing.
#>
param(
    [string]$Device = "",
    [switch]$List,
    [switch]$Revert
)
$ErrorActionPreference = "Stop"

$APO_LFX = "{EACD2258-FCAC-4FF4-B36D-419E924A6D79}"   # EqualizerAPO pre-mix / stream
$APO_GFX = "{EC1CC9CE-FAED-4822-828A-82A81A6F018F}"   # EqualizerAPO post-mix / endpoint
$FX  = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d}"       # PKEY_FX_*Clsid
$ORG = "{d3993a3f-99c2-4402-b5ec-a92a0367664b}"       # EqualizerAPO's "original value" store
$NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"     # PKEY_Device_FriendlyName
$renderRel = "SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"

function Get-Endpoints {
    Get-ChildItem "HKLM:\$renderRel" | ForEach-Object {
        $k = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        if ($k.DeviceState -ne 1) { return }
        $p = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
        $fx = Get-ItemProperty "$($_.PSPath)\FxProperties" -ErrorAction SilentlyContinue
        $slots = @()
        foreach ($s in 1, 2, 5, 6, 7) {
            $v = $fx."$FX,$s"
            if ("$v" -match "EACD2258|EC1CC9CE") { $slots += @{1="LFX";2="GFX";5="SFX";6="MFX";7="EFX"}[$s] }
        }
        [PSCustomObject]@{ Guid = $_.PSChildName; Name = $p.$NAME; ApoSlots = ($slots -join "/") }
    }
}

$eps = @(Get-Endpoints)
if ($List -or -not $Device) {
    $eps | Format-Table Name, ApoSlots, Guid -AutoSize | Out-String -Width 160 | Write-Host
    if (-not $Device) { Write-Host "Re-run with -Device '<part of name>' to switch one to LFX/GFX." }
    exit 0
}

$hits = @($eps | Where-Object { $_.Name -like "*$Device*" })
if ($hits.Count -ne 1) {
    Write-Host "'$Device' matched $($hits.Count) active output devices:" -ForegroundColor Yellow
    $eps | Format-Table Name, ApoSlots -AutoSize | Out-String | Write-Host
    exit 1
}
$ep = $hits[0]
Write-Host "Device: $($ep.Name)  [$($ep.Guid)]  current APO slots: $($ep.ApoSlots)"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole("Administrators")
if (-not $isAdmin) { Write-Host "Run this from an Administrator PowerShell." -ForegroundColor Yellow; exit 1 }

# Administrators only hold SetValue+ReadKey on MMDevices keys, so open the key
# asking for exactly those rights (the PowerShell registry provider asks for
# more and is refused).
$rights = [System.Security.AccessControl.RegistryRights]::SetValue -bor `
          [System.Security.AccessControl.RegistryRights]::QueryValues
$fxKey = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
    "$renderRel\$($ep.Guid)\FxProperties", [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, $rights)
if ($null -eq $fxKey) { Write-Error "FxProperties key not found for $($ep.Name)" }

function Set-Slot($n, $v) { $fxKey.SetValue("$FX,$n", $v, [Microsoft.Win32.RegistryValueKind]::String) }
function Remove-Slot($n) { try { $fxKey.DeleteValue("$FX,$n", $false) } catch { } }

$backup = Join-Path $PSScriptRoot ("fxproperties-backup-" + $ep.Guid.Trim("{}") + ".reg")
reg export "HKLM\$renderRel\$($ep.Guid)\FxProperties" "$backup" /y | Out-Null
Write-Host "Backed up FxProperties to $backup"

if ($Revert) {
    Set-Slot 5 $APO_LFX; Set-Slot 7 $APO_GFX
    Remove-Slot 1; Remove-Slot 2
    Set-ItemProperty "HKLM:\SOFTWARE\EqualizerAPO" -Name EnableTrace -Value "false"
    $fxKey.Close()
    Restart-Service audiosrv -Force
    Write-Host "Reverted to SFX/EFX and restarted Windows Audio."
    exit 0
}

foreach ($slot in 1, 2) {
    $prev = $fxKey.GetValue("$FX,$slot")
    if ($prev -and $prev -notmatch "EACD2258|EC1CC9CE") { $fxKey.SetValue("$ORG,$slot", $prev) }
}
Set-Slot 1 $APO_LFX
Set-Slot 2 $APO_GFX
foreach ($slot in 5, 7) {
    $orig = $fxKey.GetValue("$ORG,$slot")
    if ($orig) { Set-Slot $slot $orig } else { Remove-Slot $slot }
}
$fxKey.Close()
Set-ItemProperty "HKLM:\SOFTWARE\EqualizerAPO" -Name EnableTrace -Value "true"
Write-Host "Switched $($ep.Name) to LFX/GFX. Restarting Windows Audio..."
Restart-Service audiosrv -Force
Start-Sleep 2
Write-Host "Done. Pause/resume playback so the stream reopens, then test."
