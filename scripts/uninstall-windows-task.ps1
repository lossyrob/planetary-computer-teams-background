[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "PlanetaryComputerTeamsBackground"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existingTask) {
    Write-Host "Task not found: $TaskName"
    return
}

if ($PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Write-Host "Removed task: $TaskName"
