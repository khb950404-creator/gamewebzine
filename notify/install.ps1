# 알트탭 알림을 작업 스케줄러에 등록 (관리자 권한 불필요, 현재 사용자 전용)
#   설치:  powershell -ExecutionPolicy Bypass -File install.ps1
#   제거:  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
param([switch]$Uninstall, [int]$Minutes = 20)

$name = "AltTab 알림"
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $env:LOCALAPPDATA "AltTabNotify") -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "알트탭 알림을 제거했습니다."; exit
}
$vbs = Join-Path $PSScriptRoot "run-hidden.vbs"
$action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
           -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$logon   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
           -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $name -Action $action -Trigger @($trigger, $logon) -Settings $set `
    -Description "알트탭 새 기사·실행 실패를 윈도우 알림으로 표시" -Force | Out-Null
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "alttab-notify.ps1")   # 기준점 저장
Write-Host "설치 완료: $Minutes 분마다 확인합니다. (작업 스케줄러 > '$name')"
