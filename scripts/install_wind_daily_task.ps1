param(
    [string]$TaskName = 'ChinaCommodities-WindDaily',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if ($Remove) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    exit 0
}
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'run_wind_daily.py'
$configPath = Join-Path $repoRoot 'tmp\wind-daily\config.json'
if (!(Test-Path $python) -or !(Test-Path $configPath)) {
    throw 'Create this repository .venv, configure the official Wind SDK, then run run_wind_daily.py --init first.'
}
$settings = Get-Content -Raw -Encoding UTF8 $configPath | ConvertFrom-Json
if ($settings.enabled -ne $true) {
    throw 'Review tmp\wind-daily\config.json and set enabled=true before installing the logon task.'
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '"' + $runner + '" --watch --config "' + $configPath + '"'
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $taskSettings -Description 'Local Wind collection; no GitHub dispatch; waits for login-ready signal and yields to trading desk.' -Force | Out-Null
Write-Output "Installed $TaskName for $user. It starts at next user logon."
Write-Output "Start now if wanted: Start-ScheduledTask -TaskName '$TaskName'"
Write-Output 'This task never starts/stops trading desk, does not store a password, and does not enable remote Actions.'
