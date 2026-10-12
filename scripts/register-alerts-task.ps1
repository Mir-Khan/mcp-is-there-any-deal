<#
.SYNOPSIS
Registers (or replaces) a Windows scheduled task that runs the deal alert job every few hours.

.DESCRIPTION
The task runs `python -m deals_mcp.alerts` with this project's virtual environment, using
pythonw.exe so that no console window opens. It runs as the current user while they are logged in,
so no password is stored, and a run missed while the computer was off is made up when it is next on.
Output goes to ~/.deals-mcp/alerts.log.

.EXAMPLE
.\scripts\register-alerts-task.ps1
.\scripts\register-alerts-task.ps1 -Hours 3
.\scripts\register-alerts-task.ps1 -Remove
#>
param(
    [int]$Hours = 6,
    [string]$TaskName = "mcp-is-there-any-deal alerts",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed the scheduled task '$TaskName'."
    return
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "Could not find $pythonw; create the virtual environment first (see the README)."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m deals_mcp.alerts" -WorkingDirectory $projectRoot
# The first run is a minute from now, and then it repeats every $Hours hours.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours $Hours)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Checks the IsThereAnyDeal wishlist and posts new deals to Discord." -Force | Out-Null
Write-Output "Registered '$TaskName' to run every $Hours hours, starting in a minute."
