# Registers a Windows scheduled task that runs one paper-trading tick at 5 minutes past every
# hour, hidden (no window), as your Windows user. It starts WSL if needed, and a run missed
# while the PC was asleep happens as soon as it wakes.
#
# Run in PowerShell (no admin needed):
#   powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\home\momata\projects\cryptobot\deploy\register_windows_task.ps1
# Remove it again:
#   Unregister-ScheduledTask -TaskName "cryptobot paper tick" -Confirm:$false

$tick = "/home/momata/projects/cryptobot/deploy/wsl_tick.sh"
$action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless wsl.exe -d Ubuntu -u momata -- $tick"
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "cryptobot paper tick" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Hourly paper-trading tick (Binance testnet). See cryptobot/docs/deploy.md" | Out-Null

Start-ScheduledTask -TaskName "cryptobot paper tick"
Start-Sleep -Seconds 30
$info = Get-ScheduledTaskInfo -TaskName "cryptobot paper tick"
Write-Host "Registered. Test run result: $($info.LastTaskResult) (0 = OK). Next run: $($info.NextRunTime)"
Write-Host "Log: \\wsl$\Ubuntu\home\momata\projects\cryptobot\data\live.log"
