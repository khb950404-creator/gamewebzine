# 알트탭 윈도우 알림: 새 기사(RSS)와 사이트 실행 실패를 윈도우 토스트 알림으로 띄운다.
# 작업 스케줄러가 주기적으로 실행한다 (install.ps1 참고). 수동 테스트: powershell -File alttab-notify.ps1 -Test
param([switch]$Test)

$Site  = "https://khb950404-creator.github.io/gamewebzine/"
$Repo  = "khb950404-creator/gamewebzine"
$State = Join-Path $env:LOCALAPPDATA "AltTabNotify\state.json"
$MaxToasts = 3   # 한 번에 개별 알림은 최대 3건, 나머지는 묶어서 한 건

[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
# 알림 보낸 앱 이름은 PowerShell 로 표시된다 (별도 앱 등록 없이 쓰는 방법)
$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Esc([string]$s) { [System.Security.SecurityElement]::Escape($s) }

function Show-Toast([string]$title, [string]$body, [string]$url, [string]$attr = "알트탭") {
    $xml = @"
<toast activationType="protocol" launch="$(Esc $url)">
  <visual><binding template="ToastGeneric">
    <text>$(Esc $title)</text><text>$(Esc $body)</text><text placement="attribution">$(Esc $attr)</text>
  </binding></visual>
  <actions><action content="열기" activationType="protocol" arguments="$(Esc $url)"/></actions>
</toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show(
        (New-Object Windows.UI.Notifications.ToastNotification $doc))
}

if ($Test) { Show-Toast "alt+tab 알림 테스트" "새 기사가 올라오면 이렇게 알려드립니다." $Site; exit }

New-Item -ItemType Directory -Force (Split-Path $State) | Out-Null
$st = if (Test-Path $State) { Get-Content $State -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
$seen = @(); if ($st -and $st.seen) { $seen = @($st.seen) }
$lastFail = if ($st) { [string]$st.lastFail } else { "" }

# 1) 새 기사
try {
    $raw = (Invoke-WebRequest -UseBasicParsing "$($Site)rss.xml?t=$([DateTime]::Now.Ticks)" -TimeoutSec 20).Content
    if ($raw -is [byte[]]) { $raw = [Text.Encoding]::UTF8.GetString($raw) }
    $items = @(([xml]$raw).rss.channel.item)
    $new = @($items | Where-Object { $seen -notcontains $_.guid.'#text' -and $seen -notcontains $_.guid })
    if ($seen.Count -eq 0) { $new = @() }   # 처음 실행 땐 기존 기사로 알림 폭탄을 보내지 않음
    $show = @($new | Select-Object -First $MaxToasts)
    foreach ($it in $show) {
        Show-Toast $it.title ($it.description -replace '\s*/\s*', ' · ') $it.link ("알트탭 · " + $it.category)
        Start-Sleep -Milliseconds 400
    }
    if ($new.Count -gt $MaxToasts) {
        Show-Toast "새 기사 $($new.Count - $MaxToasts)건 더" "알트탭에서 최신 소식을 확인하세요." $Site
    }
    $seen = @($items | ForEach-Object { if ($_.guid.'#text') { $_.guid.'#text' } else { $_.guid } })
} catch { }

# 2) 사이트 실행 실패
try {
    $run = (Invoke-RestMethod "https://api.github.com/repos/$Repo/actions/runs?per_page=1" -TimeoutSec 20 `
            -Headers @{ "User-Agent" = "AltTabNotify" }).workflow_runs[0]
    if ($run.conclusion -eq "failure" -and [string]$run.id -ne $lastFail) {
        Show-Toast "⚠️ 알트탭 실행 실패" "사이트 갱신이 실패했습니다. 눌러서 로그를 확인하세요." $run.html_url "알트탭 · 경고"
        $lastFail = [string]$run.id
    }
} catch { }

@{ seen = $seen; lastFail = $lastFail } | ConvertTo-Json | Set-Content $State -Encoding UTF8
