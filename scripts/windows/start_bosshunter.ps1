#Requires -Version 7.0
<#
.SYNOPSIS
	BossHunter Windows 一键启动器（PowerShell 7 控制台主体）。

.DESCRIPTION
	双击桌面快捷方式后：
	  1. 拉起独立 profile 的 Chrome（调试端口自动探测：从 -ChromeDebugPort 起顺延到第一个空闲端口），
	     自动打开 BOSS直聘 / 智联招聘 / 51job / 猎聘 四个标签页；
	  2. 执行 `python -m bosshunter.main connect` 自检浏览器运行时；
	  3. 启动本地工作台 http://127.0.0.1:8686，并在同一个 Chrome 窗口里打开；
	  4. 全程彩色时间戳可视化输出，完整日志用 Start-Transcript 隐藏式落盘到 logs\。

	交互：
	  [E] 关闭全部服务、释放端口，3 秒后自动关闭本窗口
	  [R] 刷新当前状态
	  [T] 重新打开工作台标签页

.NOTES
	- 运行器优先级：-PythonPath > 仓库 .venv\Scripts\python.exe > PATH 的 bosshunter > py / python。
	- .venv\Scripts\bosshunter.exe 垫片内嵌旧路径，不可用；一律走 `python -m bosshunter.main`。
	- 使用 -SkipChrome 可跳过 Chrome 启动；使用 -AutoExitSeconds N 可做无人值守冒烟测试。
	- 使用 -ChromeDebugPort N（默认 9333）指定首选调试端口；被占用时自动顺延到下一个空闲端口，
	  并把实际端口写回 config.yaml 的 browser.chrome_ports 首位，避免 CDP 代理连到别的程序
	  （例如工作台 IDE 会用 --remote-debugging-port=9222，抢占后会让预检误报「未发现平台页面」）。
	- 清理只处理本项目自己的进程；端口被外部程序占用时只告警，不强杀。
#>
[CmdletBinding()]
param(
	[switch]$SkipChrome,
	[string]$PythonPath,
	[int]$AutoExitSeconds = 0,
	[string[]]$PlatformUrls = @(
		"https://www.zhipin.com",
		"https://www.zhaopin.com/nanjing/",
		"http://51job.com/nanjing",
		"https://c.liepin.com/?time=1789092279907"
	),
	[int]$WorkbenchPort = 8686,
	[int]$ChromeDebugPort = 9333
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$LogDir = Join-Path $RepoRoot "logs"
$WorkbenchUrl = "http://127.0.0.1:$WorkbenchPort"
$ChromeProfile = Join-Path $env:LOCALAPPDATA "BossHunterChrome"
$ProxyPort = 3456
# 历史上用过的调试端口：只作兼容候选与残留检查，不再假定本项目一定能用上它们
# （9222 常被工作台 IDE 之类的 Electron 程序长期占用）。
$LegacyChromePorts = @(9222, 9229, 9333)
$ManagedPorts = @($WorkbenchPort, $ProxyPort)

$script:WebProcess = $null
$script:ChromeProcess = $null
$script:TranscriptStarted = $false
$script:ResolvedChromeDebugPort = $null

if (-not (Test-Path -LiteralPath $LogDir)) {
	New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# ---------------------------------------------------------------------------
# 可视化输出
# ---------------------------------------------------------------------------

function Write-Log {
	param(
		[Parameter(Mandatory)][string]$Message,
		[ValidateSet("INFO", "STEP", "OK", "WARN", "ERR")][string]$Level = "INFO"
	)
	$color = switch ($Level) {
		"STEP" { "Cyan" }
		"OK" { "Green" }
		"WARN" { "Yellow" }
		"ERR" { "Red" }
		default { "Gray" }
	}
	Write-Host ("[{0}]" -f (Get-Date -Format "HH:mm:ss")) -NoNewline -ForegroundColor DarkGray
	Write-Host (" {0,-4} " -f $Level) -NoNewline -ForegroundColor $color
	Write-Host $Message
}

function Write-Banner {
	Write-Host ""
	Write-Host "  +--------------------------------------------------------------+" -ForegroundColor DarkCyan
	Write-Host "  |  BossHunter  -  一键启动器 (PowerShell 7)                    |" -ForegroundColor Cyan
	Write-Host "  +--------------------------------------------------------------+" -ForegroundColor DarkCyan
	Write-Host ("     仓库：{0}" -f $RepoRoot) -ForegroundColor DarkGray
	Write-Host ("     工作台：{0}" -f $WorkbenchUrl) -ForegroundColor DarkGray
	$chromePortText = if ($script:ResolvedChromeDebugPort) { [string]$script:ResolvedChromeDebugPort } else { "未启用" }
	Write-Host ("     Chrome 调试端口：{0}    CDP 代理：{1}" -f $chromePortText, $ProxyPort) -ForegroundColor DarkGray
	Write-Host ""
}

function Show-KeyHelp {
	Write-Host ""
	Write-Host "  [E] 关闭全部服务并退出    [R] 刷新状态    [T] 重开工作台标签" -ForegroundColor DarkGray
	Write-Host ""
}

# ---------------------------------------------------------------------------
# 进程 / 端口工具
# ---------------------------------------------------------------------------

function Stop-ProcessTree {
	param([Parameter(Mandatory)][int]$TargetId)

	$children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $TargetId" -ErrorAction SilentlyContinue)
	foreach ($child in $children) {
		Stop-ProcessTree -TargetId ([int]$child.ProcessId)
	}
	Stop-Process -Id $TargetId -Force -ErrorAction SilentlyContinue
}

function Get-ProcessesMatching {
	param([Parameter(Mandatory)][string]$Pattern)
	@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
		Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Pattern*" })
}

function Get-PortOwner {
	param([Parameter(Mandatory)][int]$Port)
	@(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
		Select-Object -ExpandProperty OwningProcess -Unique)
}

function Test-OwnedProcess {
	param([Parameter(Mandatory)][int]$ProcessId)

	$proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
	if (-not $proc -or -not $proc.CommandLine) { return $false }
	$commandLine = [string]$proc.CommandLine
	return ($commandLine -like "*$ChromeProfile*") -or ($commandLine -like "*$RepoRoot*")
}

function Get-ProcessIdentity {
	param([Parameter(Mandatory)][int]$ProcessId)

	$proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
	if (-not $proc) { return "PID $ProcessId" }
	return ("{0} (PID {1})" -f $proc.Name, $ProcessId)
}

function Test-PortBindable {
	param([Parameter(Mandatory)][int]$Port)

	# 真实 bind 探测：同时覆盖「已被监听」和「落在系统保留端口段」两种情况。
	$listener = $null
	try {
		$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
		$listener.Start()
		return $true
	} catch {
		return $false
	} finally {
		if ($listener) { try { $listener.Stop() } catch { } }
	}
}

function Test-PortAvailable {
	param([Parameter(Mandatory)][int]$Port)

	if (@(Get-PortOwner -Port $Port).Count -gt 0) { return $false }
	return (Test-PortBindable -Port $Port)
}

function Resolve-ChromeDebugPort {
	param(
		[Parameter(Mandatory)][int]$PreferredPort,
		[int]$MaxAttempts = 50
	)

	# 从首选端口起顺延，返回第一个真正空闲的端口。
	# 不做静默日志：调用方拿到结果后在 banner 之后再输出，避免刷在标题前面。
	$rejected = @()
	for ($offset = 0; $offset -lt $MaxAttempts; $offset++) {
		$port = $PreferredPort + $offset
		if ($port -gt 65535) { break }
		if (Test-PortAvailable -Port $port) {
			return @{ Port = $port; Rejected = $rejected }
		}
		$owners = @(Get-PortOwner -Port $port)
		$ownerText = if ($owners.Count -gt 0) { "被 PID " + ($owners -join ",") + " 占用" } else { "被系统保留" }
		$rejected += ("{0} {1}" -f $port, $ownerText)
	}
	return @{ Port = $null; Rejected = $rejected }
}

function Test-WorkbenchReady {
	try {
		$null = Invoke-WebRequest -Uri "$WorkbenchUrl/" -UseBasicParsing -TimeoutSec 2
		return $true
	} catch {
		return $false
	}
}

function Test-ChromeDebugReady {
	param([Parameter(Mandatory)][int]$Port)

	# 只判断「端口有人应答」是不够的：Electron 应用的 /json/version 也会上报
	# Browser = Chrome/xxx，会被误当成 Google Chrome（9222 上的工作台 IDE 就是这样）。
	# 所以额外要求 User-Agent 不带 Electron/。
	try {
		$version = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
	} catch {
		return $false
	}
	if ([string]$version.'User-Agent' -like "*Electron/*") {
		Write-Log ("端口 {0} 的调试端点来自 Electron 应用，不是 Google Chrome" -f $Port) "WARN"
		return $false
	}
	$browser = [string]$version.Browser
	if (-not $browser) { return $true }
	return ($browser -like "Chrome/*") -or ($browser -like "HeadlessChrome/*") -or ($browser -like "Edg/*")
}

function Invoke-Cleanup {
	param([switch]$Quiet)

	Write-Log "开始清理：停止服务并释放端口" "STEP"

	if ($script:WebProcess) {
		Write-Log ("停止工作台进程树 (PID {0})" -f $script:WebProcess.Id)
		Stop-ProcessTree -TargetId $script:WebProcess.Id
		$script:WebProcess = $null
	}

	# 只清理本项目的残留工作台 / Browser Runtime 进程。
	# 注意：不要按「仓库路径 + python」做宽匹配，否则会误杀同一仓库里用户自己跑的其他 Python 进程
	# （例如正在跑的 pytest）。
	$runtimeSignatures = @(
		"bosshunter.main",
		"bosshunter\main.py",
		"cdp-proxy.mjs",
		"check-runtime.mjs"
	)
	foreach ($signature in $runtimeSignatures) {
		foreach ($proc in Get-ProcessesMatching -Pattern $signature) {
			if ($proc.CommandLine -notlike "*$RepoRoot*") { continue }
			Write-Log ("停止残留服务 {0} (PID {1})" -f $proc.Name, $proc.ProcessId)
			Stop-ProcessTree -TargetId ([int]$proc.ProcessId)
		}
	}

	foreach ($proc in Get-ProcessesMatching -Pattern $ChromeProfile) {
		Write-Log ("停止 Chrome 进程树 (PID {0})" -f $proc.ProcessId)
		Stop-ProcessTree -TargetId ([int]$proc.ProcessId)
	}

	if ($script:ChromeProcess) {
		Stop-ProcessTree -TargetId $script:ChromeProcess.Id
		$script:ChromeProcess = $null
	}

	# 兜底：按端口清掉本项目自己的残留监听者。
	# 只处理本项目进程：2026-09-15 实测旧逻辑按端口强杀占用者，把长期占用 9222 的工作台 IDE
	# 一起杀掉（IDE 随即报 Unhandled TypeError: Object has been destroyed 崩退）。外部程序只告警。
	$leftover = @()
	$foreign = @()
	foreach ($port in $ManagedPorts) {
		foreach ($owner in Get-PortOwner -Port $port) {
			if (Test-OwnedProcess -ProcessId ([int]$owner)) {
				Write-Log ("端口 {0} 仍被本项目进程 PID {1} 占用，强制释放" -f $port, $owner) "WARN"
				Stop-ProcessTree -TargetId ([int]$owner)
				$leftover += $port
			} else {
				$foreign += ("{0} ← {1}" -f $port, (Get-ProcessIdentity -ProcessId ([int]$owner)))
			}
		}
	}

	$leftover = @($leftover | Select-Object -Unique)
	$foreign = @($foreign | Select-Object -Unique)
	if ($leftover.Count -eq 0) {
		Write-Log ("本项目端口 {0} 已全部释放" -f ($ManagedPorts -join "/")) "OK"
	} else {
		Write-Log ("端口 {0} 仍被本项目进程占用，请手动检查" -f ($leftover -join "/")) "WARN"
	}
	if ($foreign.Count -gt 0) {
		Write-Log ("以下端口由外部程序持有，已跳过不干预：{0}" -f ($foreign -join "；"))
	}

	if (-not $Quiet) {
		Write-Log "3 秒后自动关闭本窗口" "STEP"
		Start-Sleep -Seconds 3
	}
}

# ---------------------------------------------------------------------------
# 运行器解析
# ---------------------------------------------------------------------------

function Resolve-BossHunterRunner {
	if ($PythonPath) {
		if (-not (Test-Path -LiteralPath $PythonPath)) {
			throw "指定的 Python 不存在：$PythonPath"
		}
		Write-Log ("使用 -PythonPath 指定的解释器：{0}" -f (Resolve-Path -LiteralPath $PythonPath).Path) "OK"
		return @{
			Path   = (Resolve-Path -LiteralPath $PythonPath).Path
			Prefix = @("-m", "bosshunter.main")
			Label  = "explicit python"
		}
	}

	$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
	if (Test-Path -LiteralPath $VenvPython) {
		Write-Log ("使用仓库虚拟环境：{0}" -f $VenvPython) "OK"
		return @{ Path = $VenvPython; Prefix = @("-m", "bosshunter.main"); Label = "venv" }
	}

	$Bosshunter = Get-Command "bosshunter" -ErrorAction SilentlyContinue
	if ($Bosshunter) {
		Write-Log ("使用 PATH 中的 bosshunter：{0}" -f $Bosshunter.Source) "OK"
		return @{ Path = $Bosshunter.Source; Prefix = @(); Label = "console-script" }
	}

	$Python = Get-Command "py" -ErrorAction SilentlyContinue
	if (-not $Python) {
		$Python = Get-Command "python" -ErrorAction SilentlyContinue
	}
	if (-not $Python) {
		throw "未找到可用的 BossHunter 或 Python。请先在仓库根目录执行：pip install -e ."
	}
	Write-Log ("使用 PATH 解释器：{0}" -f $Python.Source) "OK"
	return @{ Path = $Python.Source; Prefix = @("-m", "bosshunter.main"); Label = "path-python" }
}

function Resolve-ChromePath {
	$candidates = @()
	if ($env:ProgramFiles) { $candidates += Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe" }
	if (${env:ProgramFiles(x86)}) { $candidates += Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe" }
	if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe" }

	$hit = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
	if (-not $hit) {
		throw "未找到 Google Chrome。请安装 Chrome，或使用 -SkipChrome 手动准备浏览器。"
	}
	return $hit
}

function Sync-ChromePortsConfig {
	param([Parameter(Mandatory)][int]$Port)

	# BossHunter 的 CDP 代理按 config.yaml 的 browser.chrome_ports 顺序探测端口。
	# 把本次真正使用的端口放到首位，避免代理先去撞被别的程序占用的旧端口；
	# 旧端口保留在后面，用户自己开 Chrome 调试时仍能被发现。
	$configPath = Join-Path $RepoRoot "config.yaml"
	if (-not (Test-Path -LiteralPath $configPath)) {
		Write-Log "未找到 config.yaml，跳过调试端口同步" "WARN"
		return
	}

	# 注意 Where-Object 只能作用在 LegacyChromePorts 上：写成
	# `@($Port) + @($LegacyChromePorts) | Where-Object ...` 会把 $Port 自己也过滤掉。
	$portOrder = @(@($Port) + @($LegacyChromePorts | Where-Object { $_ -ne $Port }) | Select-Object -Unique)
	try {
		$raw = Get-Content -LiteralPath $configPath -Raw -Encoding utf8
	} catch {
		Write-Log ("读取 config.yaml 失败：{0}" -f $_.Exception.Message) "WARN"
		return
	}

	$newline = if ($raw.Contains("`r`n")) { "`r`n" } else { "`n" }
	$block = "  chrome_ports:" + $newline + (($portOrder | ForEach-Object { "  - $_" }) -join $newline) + $newline
	$pattern = "(?m)^  chrome_ports:\r?\n(?:  - \d+\r?\n?)+"
	if (-not [regex]::IsMatch($raw, $pattern)) {
		Write-Log "config.yaml 中没有可替换的 browser.chrome_ports 区块，未改动" "WARN"
		return
	}

	$updated = [regex]::Replace($raw, $pattern, $block)
	if ($updated -eq $raw) {
		Write-Log ("config.yaml 的浏览器调试端口已是：{0}" -f ($portOrder -join ", "))
		return
	}
	Set-Content -LiteralPath $configPath -Value $updated -Encoding utf8 -NoNewline
	Write-Log ("已把调试端口写回 config.yaml：{0}" -f ($portOrder -join ", ")) "OK"
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

$transcriptPath = Join-Path $LogDir ("bosshunter_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

try {
	try {
		Start-Transcript -Path $transcriptPath | Out-Null
		$script:TranscriptStarted = $true
	} catch {
		$script:TranscriptStarted = $false
	}

	# 调试端口要在 banner 之前定下来（banner 会打印它）。被占用就顺延到下一个空闲端口。
	$portRejections = @()
	if (-not $SkipChrome) {
		$portResolution = Resolve-ChromeDebugPort -PreferredPort $ChromeDebugPort
		$script:ResolvedChromeDebugPort = $portResolution.Port
		$portRejections = $portResolution.Rejected
		if (-not $script:ResolvedChromeDebugPort) {
			throw ("从 {0} 起连续 50 个端口都不可用，无法启动 Chrome 远程调试。" -f $ChromeDebugPort)
		}
		$ManagedPorts = @(@($ManagedPorts) + @($script:ResolvedChromeDebugPort) + @($LegacyChromePorts) |
			Select-Object -Unique)
	}

	Write-Banner
	if ($script:TranscriptStarted) {
		Write-Log ("日志将写入 {0}" -f $transcriptPath)
	} else {
		Write-Log "当前宿主不支持 Start-Transcript，本次仅输出到控制台" "WARN"
	}
	foreach ($rejection in $portRejections) {
		Write-Log ("调试端口 {0}，顺延检查下一个" -f $rejection) "WARN"
	}
	if ($script:ResolvedChromeDebugPort -and $portRejections.Count -gt 0) {
		Write-Log ("最终使用空闲调试端口 {0}" -f $script:ResolvedChromeDebugPort) "OK"
	}

	$Runner = Resolve-BossHunterRunner
	$RunnerPrefix = $Runner.Prefix

	$Chrome = $null
	$ChromeArguments = @()
	if (-not $SkipChrome) {
		$Chrome = Resolve-ChromePath
		Write-Log ("Chrome：{0}" -f $Chrome) "OK"
		$ChromeArguments = @(
			"--remote-debugging-port=$($script:ResolvedChromeDebugPort)",
			"--user-data-dir=$ChromeProfile",
			"--disable-session-crashed-bubble",
			"--no-first-run",
			"--no-default-browser-check"
		)

		# 让 CDP 代理优先探测这个端口，否则它可能先撞上 9222 上别的程序。
		Sync-ChromePortsConfig -Port $script:ResolvedChromeDebugPort

		Write-Log ("启动独立 profile 的 Chrome 并打开 {0} 个平台标签页" -f $PlatformUrls.Count) "STEP"
		$script:ChromeProcess = Start-Process -FilePath $Chrome -ArgumentList ($ChromeArguments + $PlatformUrls) -PassThru

		$chromeReady = $false
		for ($i = 0; $i -lt 20; $i++) {
			Start-Sleep -Milliseconds 500
			if (Test-ChromeDebugReady -Port $script:ResolvedChromeDebugPort) { $chromeReady = $true; break }
		}
		if ($chromeReady) {
			Write-Log ("Chrome 远程调试 {0} 已就绪" -f $script:ResolvedChromeDebugPort) "OK"
		} else {
			Write-Log ("Chrome 远程调试 {0} 在 10 秒内未就绪，后续 connect 自检可能失败" -f $script:ResolvedChromeDebugPort) "WARN"
		}
	} else {
		Write-Log "已指定 -SkipChrome，跳过 Chrome 启动" "WARN"
	}

	Write-Log "执行 connect 自检（浏览器运行时 + 登录态）" "STEP"
	& $Runner.Path @RunnerPrefix "connect"
	$connectExit = $LASTEXITCODE
	if ($null -eq $connectExit) { $connectExit = "未知" }
	if ($connectExit -eq 0) {
		Write-Log "connect 自检通过" "OK"
	} else {
		Write-Log ("connect 自检返回退出码 {0}，工作台仍会启动，可稍后在面板中重试" -f $connectExit) "WARN"
	}

	Write-Log "启动本地工作台" "STEP"
	$webArgs = @($RunnerPrefix) + @("web", "--no-open", "--port", "$WorkbenchPort")
	$script:WebProcess = Start-Process -FilePath $Runner.Path -ArgumentList $webArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru

	$webReady = $false
	for ($i = 0; $i -lt 40; $i++) {
		Start-Sleep -Milliseconds 500
		if (Test-WorkbenchReady) { $webReady = $true; break }
	}
	if ($webReady) {
		Write-Log ("工作台已就绪：{0}" -f $WorkbenchUrl) "OK"
	} else {
		Write-Log "工作台 20 秒内未响应，请查看同目录日志" "WARN"
	}

	if ($Chrome) {
		Write-Log "在同一个 Chrome 窗口打开工作台标签页" "STEP"
		Start-Process -FilePath $Chrome -ArgumentList ($ChromeArguments + $WorkbenchUrl) | Out-Null
	}

	Write-Log "BossHunter 已就绪。首次使用请在专属 Chrome 窗口内手动登录招聘平台。" "OK"
	Show-KeyHelp

	$deadline = $null
	if ($AutoExitSeconds -gt 0) {
		$deadline = (Get-Date).AddSeconds($AutoExitSeconds)
		Write-Log ("已启用自动退出：{0} 秒后关闭" -f $AutoExitSeconds) "WARN"
	}

	$exitRequested = $false
	while (-not $exitRequested) {
		if ($null -ne $deadline -and (Get-Date) -gt $deadline) {
			Write-Log "自动退出计时到点" "STEP"
			break
		}

		# 无控制台（重定向 / 非交互宿主）时降级为纯等待，避免报错。
		$keyAvailable = $false
		try { $keyAvailable = [Console]::KeyAvailable } catch { $keyAvailable = $false }

		if ($keyAvailable) {
			$key = [Console]::ReadKey($true)
			switch ([char]::ToUpper($key.KeyChar)) {
				"E" {
					Write-Log "收到退出指令 [E]" "STEP"
					$exitRequested = $true
				}
				"R" {
					Write-Log "刷新状态 [R]" "STEP"
					$webOnline = Test-WorkbenchReady
					Write-Log ("工作台 {0}：{1}" -f $WorkbenchUrl, $(if ($webOnline) { "在线" } else { "离线" })) $(if ($webOnline) { "OK" } else { "WARN" })
					if (-not $SkipChrome) {
						$cdpOnline = Test-ChromeDebugReady -Port $script:ResolvedChromeDebugPort
						Write-Log ("Chrome 调试 {0}：{1}" -f $script:ResolvedChromeDebugPort, $(if ($cdpOnline) { "在线" } else { "离线" })) $(if ($cdpOnline) { "OK" } else { "WARN" })
					}
					$busy = @($ManagedPorts | Where-Object { @(Get-PortOwner -Port $_).Count -gt 0 })
					Write-Log ("监听中的端口：{0}" -f $(if ($busy) { $busy -join "/" } else { "无" }))
				}
				"T" {
					Write-Log "重开工作台标签 [T]" "STEP"
					if ($Chrome) {
						Start-Process -FilePath $Chrome -ArgumentList ($ChromeArguments + $WorkbenchUrl) | Out-Null
						Write-Log "已请求打开工作台标签" "OK"
					} else {
						Write-Log "当前为 -SkipChrome 模式，无法打开标签页" "WARN"
					}
				}
				default {
					if ($key.Key -eq [ConsoleKey]::Escape) {
						$exitRequested = $true
					}
				}
			}
		}
		Start-Sleep -Milliseconds 120
	}
} finally {
	Invoke-Cleanup

	if ($script:TranscriptStarted) {
		try { Stop-Transcript | Out-Null } catch { }
	}
	Write-Host ""
	Write-Host "  BossHunter 已退出。" -ForegroundColor DarkGray
	Write-Host ""
}
