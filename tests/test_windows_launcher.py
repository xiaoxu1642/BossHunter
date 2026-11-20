from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "windows" / "start_bosshunter.ps1"
INSTALLER = REPO_ROOT / "scripts" / "windows" / "install_desktop_shortcut.ps1"


class WindowsLauncherTests(unittest.TestCase):
	def test_launcher_is_portable_and_opens_both_services(self):
		text = LAUNCHER.read_text(encoding="utf-8")
		self.assertIn("$PSScriptRoot", text)
		self.assertIn("bosshunter.main", text)
		self.assertIn("$PythonPath", text)
		self.assertIn("Get-Command", text)
		# 2026-09-15 起调试端口不再写死 9222：从 -ChromeDebugPort 起顺延到空闲端口。
		# 只校验真正拼进 Chrome 启动参数的那一行，避免被注释文案误伤。
		chrome_arg_lines = [
			line for line in text.splitlines()
			if "remote-debugging-port=" in line and line.strip().startswith('"')
		]
		self.assertEqual(len(chrome_arg_lines), 1)
		self.assertIn("$($script:ResolvedChromeDebugPort)", chrome_arg_lines[0])
		self.assertNotIn("9222", chrome_arg_lines[0])
		self.assertIn("http://127.0.0.1:8686", text)
		self.assertNotIn("C:\\Users\\123", text)

	def test_launcher_prefers_repo_venv_runner(self):
		text = LAUNCHER.read_text(encoding="utf-8")
		# 运行器优先级：-PythonPath > .venv\Scripts\python.exe > PATH bosshunter > py/python
		self.assertIn(".venv\\Scripts\\python.exe", text)
		self.assertIn('Join-Path $RepoRoot ".venv\\Scripts\\python.exe"', text)
		self.assertLess(text.index("$VenvPython"), text.index('Get-Command "bosshunter"'))
		# 上游 #164 的不变量：仓库 venv 必须排在 PATH 上的 py/python 之前，
		# 否则只装在 venv 里的 bosshunter 会导入失败。
		self.assertLess(text.index("$VenvPython"), text.index('Get-Command "python"'))

	def test_launcher_keeps_console_visible_and_logs_to_file(self):
		text = LAUNCHER.read_text(encoding="utf-8")
		# 可视化过程：日志走 Start-Transcript 落盘，控制台只做彩色输出。
		self.assertIn("Start-Transcript", text)
		self.assertIn("logs", text)
		# 契约：Chrome / 控制台本身必须可见，-WindowStyle Hidden 只允许用在后台子进程上。
		chrome_start = [line for line in text.splitlines() if "Start-Process -FilePath $Chrome" in line]
		self.assertTrue(chrome_start)
		for line in chrome_start:
			self.assertNotIn("-WindowStyle Hidden", line)
		self.assertNotIn("-NoExit", text)

	def test_launcher_suppresses_chrome_restore_bubble(self):
		text = LAUNCHER.read_text(encoding="utf-8")
		self.assertIn("--disable-session-crashed-bubble", text)
		self.assertIn("--no-first-run", text)
		self.assertIn("--no-default-browser-check", text)
		self.assertIn("zhipin.com", text)
		self.assertIn("zhaopin.com", text)
		self.assertIn("51job.com", text)
		self.assertIn("liepin.com", text)
		self.assertIn('"https://www.zhaopin.com/nanjing/"', text)
		self.assertIn('"http://51job.com/nanjing"', text)
		# 平台 URL 必须真的随 Chrome 一起启动，而不是只写在参数默认值里。
		chrome_args = [line for line in text.splitlines() if "$ChromeArguments + $PlatformUrls" in line]
		self.assertTrue(chrome_args, "Chrome 启动参数必须拼接 $PlatformUrls")

	def test_launcher_supports_interactive_keys_and_port_cleanup(self):
		text = LAUNCHER.read_text(encoding="utf-8")
		for key in ('"E"', '"R"', '"T"'):
			self.assertIn(key, text)
		# 清理函数要覆盖工作台 / Chrome 进程树与全部受管端口。
		for port in ("8686", "9222", "9229", "9333", "3456"):
			self.assertIn(port, text)
		self.assertIn("Stop-ProcessTree", text)
		self.assertIn("Start-Sleep -Seconds 3", text)

	def test_installer_creates_a_shortcut_to_the_launcher(self):
		text = INSTALLER.read_text(encoding="utf-8")
		self.assertIn("CreateShortcut", text)
		self.assertIn("start_bosshunter.ps1", text)
		self.assertIn('GetFolderPath("Desktop")', text)
		self.assertNotIn("C:\\Users\\123", text)

	def test_installer_targets_pwsh7_without_hiding_the_console(self):
		text = INSTALLER.read_text(encoding="utf-8")
		self.assertIn("pwsh", text)
		self.assertIn("bosshunter.ico", text)
		# 只校验真正写进快捷方式的参数字符串，避免被注释文案误伤。
		argument_lines = [line for line in text.splitlines() if line.strip().startswith("$Arguments")]
		self.assertEqual(len(argument_lines), 1)
		arguments = argument_lines[0]
		self.assertIn("-NoLogo", arguments)
		self.assertIn("-ExecutionPolicy Bypass", arguments)
		self.assertIn("start_bosshunter.ps1", text)
		# 反向约束：-NoExit 会让窗口永不自动关；-WindowStyle Hidden 会隐藏控制台。
		self.assertNotIn("-NoExit", arguments)
		self.assertNotIn("-WindowStyle", arguments)
		self.assertNotIn("-NoProfile", arguments)


	def test_launcher_resolves_free_debug_port_and_ignores_foreign_endpoints(self):
		text = LAUNCHER.read_text(encoding="utf-8")
		# 端口被占用要顺延，而不是写死 9222（工作台 IDE 会长期占用 9222）。
		self.assertIn("[int]$ChromeDebugPort = 9333", text)
		self.assertIn("Resolve-ChromeDebugPort", text)
		self.assertIn("Test-PortAvailable", text)
		# 不能只按「端口有响应」判定 Chrome：Electron 应用同样上报 Browser = Chrome/xxx。
		self.assertIn("Electron/", text)
		# 清理不得强杀非本项目进程（旧逻辑会连带杀掉占用 9222 的 IDE）。
		self.assertIn("Test-OwnedProcess", text)
		# 解析出的端口要写回 config.yaml，供 CDP 代理优先探测。
		self.assertIn("Sync-ChromePortsConfig", text)


if __name__ == "__main__":
	unittest.main()
