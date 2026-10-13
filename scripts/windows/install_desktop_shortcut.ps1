#Requires -Version 5.1
<#
.SYNOPSIS
	在桌面创建 BossHunter 一键启动快捷方式（cmd 观感 / pwsh 7 内核）。

.DESCRIPTION
	生成的 BossHunter.lnk 指向 PowerShell 7（pwsh.exe）并带起 start_bosshunter.ps1：
	  - 目标：pwsh.exe
	  - 参数：-NoLogo -ExecutionPolicy Bypass -File "<repo>\scripts\windows\start_bosshunter.ps1"
	  - 工作目录：仓库根目录
	  - 图标：assets\bosshunter.ico

	两个反向约束（改动前请先想清楚）：
	  1. 参数里不能有 -NoExit：否则按 [E] 退出后窗口永不自动关闭；
	  2. 参数里不能有 -WindowStyle Hidden：否则控制台不可见，违反「可视化过程」要求。

	快捷方式不存储任何 API Key、账号或平台凭据。
#>
[CmdletBinding()]
param(
	[string]$ShortcutName = "BossHunter"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Launcher = Join-Path $PSScriptRoot "start_bosshunter.ps1"
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "$ShortcutName.lnk"
$IconPath = Join-Path $RepoRoot "assets\bosshunter.ico"

if (-not (Test-Path -LiteralPath $Launcher)) {
	throw "启动器不存在：$Launcher"
}

function Resolve-PwshPath {
	# 优先使用当前宿主（若本身就是 PowerShell 7），否则去标准安装位置找。
	if ($PSVersionTable.PSEdition -eq "Core" -and $PSVersionTable.PSVersion.Major -ge 7) {
		return (Get-Process -Id $PID).Path
	}
	$candidates = @(
		(Join-Path $env:ProgramFiles "PowerShell\7\pwsh.exe"),
		(Join-Path ${env:ProgramFiles(x86)} "PowerShell\7\pwsh.exe")
	)
	foreach ($candidate in $candidates) {
		if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
	}
	$found = Get-Command "pwsh.exe" -ErrorAction SilentlyContinue
	if ($found) { return $found.Source }
	throw "未找到 PowerShell 7（pwsh.exe）。请先安装：winget install --id Microsoft.PowerShell"
}

$Pwsh = Resolve-PwshPath
$Arguments = "-NoLogo -ExecutionPolicy Bypass -File `"$Launcher`""

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Pwsh
$Shortcut.Arguments = $Arguments
$Shortcut.WorkingDirectory = $RepoRoot
$Shortcut.Description = "启动 BossHunter：专属 Chrome（CDP 9222）+ 本地工作台 8686"
if (Test-Path -LiteralPath $IconPath) {
	$Shortcut.IconLocation = "$IconPath,0"
}
$Shortcut.Save()

# 落盘后回读校验，防止手滑写出不满足契约的快捷方式。
$Verify = $Shell.CreateShortcut($ShortcutPath)
$problems = @()
if ($Verify.TargetPath -notlike "*pwsh.exe") { $problems += "目标不是 pwsh.exe：$($Verify.TargetPath)" }
if ($Verify.Arguments -match "-NoExit") { $problems += "参数含 -NoExit，按 [E] 后窗口不会自动关闭" }
if ($Verify.Arguments -match "-WindowStyle\s+Hidden") { $problems += "参数含 -WindowStyle Hidden，控制台将不可见" }
if (-not (Test-Path -LiteralPath $IconPath)) { $problems += "图标缺失：$IconPath" }

Write-Host ""
Write-Host "  已创建桌面快捷方式：$ShortcutPath" -ForegroundColor Green
Write-Host "    目标    ：$($Verify.TargetPath)"
Write-Host "    参数    ：$($Verify.Arguments)"
Write-Host "    工作目录：$($Verify.WorkingDirectory)"
Write-Host "    图标    ：$($Verify.IconLocation)"
if ($problems.Count -gt 0) {
	Write-Host ""
	Write-Host "  以下检查未通过，请人工确认：" -ForegroundColor Yellow
	foreach ($p in $problems) { Write-Host "    - $p" -ForegroundColor Yellow }
} else {
	Write-Host "  契约校验：可见控制台 + 无 -NoExit + 图标就绪" -ForegroundColor Green
}
Write-Host ""
