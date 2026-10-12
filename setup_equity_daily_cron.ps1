# setup_equity_daily_cron.ps1
# Registers a Windows Scheduled Task to run the Equity Pipeline every trading day at 18:30 (6:30 PM IST)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$batPath = Join-Path $scriptDir "run_daily_equity_pipeline.bat"
$taskName = "Equity_Master_Daily_Pipeline"
$scheduledTime = "18:30"

Write-Host "Registering Windows Scheduled Task '$taskName'..." -ForegroundColor Cyan
Write-Host "Target: $batPath"
Write-Host "Schedule: Daily at $scheduledTime IST (after market close)"

try {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batPath`""
    $trigger = New-ScheduledTaskTrigger -Daily -At $scheduledTime
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "[SUCCESS] Task '$taskName' successfully registered!" -ForegroundColor Green
    Write-Host "The pipeline will run automatically every evening at $scheduledTime IST."
} catch {
    Write-Warning "PowerShell cmdlet failed: $_. Falling back to schtasks.exe..."
    schtasks /create /tn $taskName /tr "`"$batPath`"" /sc daily /st $scheduledTime /f
}

Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Format-List TaskName, State, Actions, Triggers
