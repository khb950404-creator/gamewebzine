' 작업 스케줄러가 PowerShell 창을 띄우지 않고 알림 스크립트를 실행하도록 감싼다
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & dir & "\alttab-notify.ps1""", 0, False
