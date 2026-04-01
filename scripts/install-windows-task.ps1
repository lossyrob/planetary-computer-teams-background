[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "PlanetaryComputerTeamsBackground",
    [string]$PythonExe,
    [string]$RepoRoot,
    [string]$SettingsFile,
    [int]$IntervalSeconds = 900,
    [string]$LogFile,
    [switch]$StartNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$RunnerScript = Join-Path $RepoRoot "pc_teams_background_runner.py"
if (-not (Test-Path $RunnerScript)) {
    throw "Runner script not found: $RunnerScript"
}

if (-not $PythonExe) {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $PythonExe = $venvPython
    }
    else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw "Could not find python.exe. Pass -PythonExe explicitly."
        }
        $PythonExe = $pythonCommand.Source
    }
}
$PythonExe = (Resolve-Path $PythonExe).Path

if (-not $SettingsFile) {
    $defaultSettings = Join-Path $RepoRoot "settings.yaml"
    if (-not (Test-Path $defaultSettings)) {
        throw "settings.yaml not found. Pass -SettingsFile explicitly."
    }
    $SettingsFile = $defaultSettings
}
$SettingsFile = (Resolve-Path $SettingsFile).Path

if (-not $LogFile) {
    $logDirectory = Join-Path $env:LOCALAPPDATA "PlanetaryComputerTeamsBackground\logs"
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $LogFile = Join-Path $logDirectory "runner.log"
}
else {
    $logDirectory = Split-Path -Parent $LogFile
    if ($logDirectory) {
        New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    }
}

$argumentList = @(
    ('"{0}"' -f $RunnerScript),
    "--interval-seconds", $IntervalSeconds,
    "--settings-file", ('"{0}"' -f $SettingsFile),
    "--log-file", ('"{0}"' -f $LogFile)
)
$arguments = $argumentList -join " "

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $arguments -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$description = "Continuously refreshes the Teams background from Planetary Computer imagery. Logs: $LogFile"

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $description `
        -Force | Out-Null

    if ($StartNow) {
        Start-ScheduledTask -TaskName $TaskName
    }
}

Write-Host "Task name : $TaskName"
Write-Host "Python    : $PythonExe"
Write-Host "Runner    : $RunnerScript"
Write-Host "Settings  : $SettingsFile"
Write-Host "Log file  : $LogFile"
Write-Host "Manage with:"
Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "  Stop-ScheduledTask -TaskName `"$TaskName`""
Write-Host "  Enable-ScheduledTask -TaskName `"$TaskName`""
Write-Host "  Disable-ScheduledTask -TaskName `"$TaskName`""
Write-Host "  Get-Content `"$LogFile`" -Wait"
