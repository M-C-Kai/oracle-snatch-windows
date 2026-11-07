<#
.SYNOPSIS
  注册 Windows 任务计划：登录时启动 oracle-snatch（常开轮询免费 ARM 实例）。

  重要：snatch.py 自身是 while True 死循环，只需启动一次。
  不要设置「失败时重启」——脚本遇到 LimitExceeded（免费额度已满）会主动
  return 1 退出，重启只会无限刷日志；429/没货脚本内部已自行退避循环。

  以当前用户身份运行（PowerShell 窗口里执行即可，无需管理员）。
#>
param(
    [string]$TaskName = "oracle-snatch",
    [string]$WorkDir  = $PSScriptRoot
)

$bat = Join-Path $WorkDir "start.bat"
if (-not (Test-Path $bat)) {
    Write-Error "找不到 $bat"
    exit 1
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$bat`"" `
    -WorkingDirectory $WorkDir

# 登录时触发（当前用户）
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 0 `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "已注册任务 '$TaskName'（登录时启动一次，无失败重启）。"
Write-Host "手动启动: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "查看状态: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "查看日志: Get-Content '$WorkDir\snatch.log' -Tail 30 -Wait"
Write-Host "卸载任务: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
