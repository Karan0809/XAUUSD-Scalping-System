$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "Launching Aggressive Bot (.env.aggressive)..." -ForegroundColor Cyan
$agg = Start-Process -WindowStyle Hidden -FilePath "python" -ArgumentList "$Root\scripts\run_aggressive.py --env $Root\.env.aggressive" -PassThru

Start-Sleep -Seconds 3

Write-Host "Launching Mindspace Bot (.env.mindspace)..." -ForegroundColor Green
$ms = Start-Process -WindowStyle Hidden -FilePath "python" -ArgumentList "$Root\scripts\run_mindspace.py --env $Root\.env.mindspace" -PassThru

Write-Host ""
Write-Host "Both bots running in background windows." -ForegroundColor Yellow
Write-Host "  Aggressive PID: $($agg.Id)  (window: python run_aggressive.py)"
Write-Host "  Mindspace  PID: $($ms.Id)  (window: python run_mindspace.py)"
Write-Host ""
Write-Host "To stop: Stop-Process -Id $($agg.Id),$($ms.Id)"
