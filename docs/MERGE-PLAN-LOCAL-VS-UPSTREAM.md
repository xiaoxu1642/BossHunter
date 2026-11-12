# 本地定制 vs 上游 main 合并清单

> 生成时间：2026-11-12
> 本地：`main` @ `2e5f4f8`（10-13）+ 未提交工作区改动
> 上游：`origin/main` @ `12d6891`（11-06），落后 86 个提交
> 共同祖先：`3997b99`（09-09）
> 方法：在临时仓库对「工作区快照」与 `origin/main` 做真实 `git merge`，不修改本仓库

## 一、总量

| 项 | 数量 |
|---|---|
| 本地相对共同祖先改过/新增的代码与测试文件 | 104 |
| 自动合并成功 | 79 |
| **真实冲突（必须人工决定）** | **25** |

两个口径不要混淆：

- **104** 是"相对共同祖先 `3997b99` 有差异"的文件，用来算合并冲突。
- **53** 是"上游任何历史版本里都没有过的内容"，用来算真正的本地定制。

差额来自：有些文件本地改成了上游某个较旧提交的版本（同步过、但没跟到最新）。它们对合并有影响，但不是你的原创定制。

自动合并成功不代表语义正确：上游同时改过的文件，即使文本没冲突，也可能出现“上游新逻辑 + 本地旧假设”的组合错误，需要跑测试验证。

## 二、25 个真实冲突与处置建议

### A. 建议保留本地版本（上游改动已被本地实现覆盖或本地更强）

| 文件 | 冲突原因 | 建议 |
|---|---|---|
| `src/bosshunter/collection/platforms/boss.py` | 本地把风险信号扩成 `risk/evidence/url/severity`，soft 只暂停本轮；上游用 `risk/evidence` 二字段 | **保留本地**。本地是上游逻辑的超集，`trigger_risk` 已兼容上游字段。合并后需确认上游新增的 `blockedPath/blockedStatus` 判定仍在 |
| `src/bosshunter/web/frontend/src/pages/DashboardPage.tsx` | 本地抽出 `useBlockCompany` + 行内提示；上游仍是内联实现 + `window.confirm` | **保留本地**。本地修掉了“评分运行时点屏蔽无反应”和“提示看不到”两个问题 |
| `src/bosshunter/web/frontend/src/components/dashboard/JobsTable.tsx` | 本地加了 `blockNotice/blockNoticeJobId` 并改用 `findBlockingRule` | **保留本地**。上游的 `blockedCompanies.find(...)` 与本地 `findBlockingRule` 等价但更弱 |
| `src/bosshunter/web/server.py` | 本地允许“只改屏蔽名单”在任务运行中保存 | **保留本地**。这正是本次修的 bug |
| `src/bosshunter/web/preflight.py` | 本地自检会真的发一条最小生成请求；上游只查模型列表 | **保留本地**。上游的“仅检测模型列表”会复现 `u2-flash` 假绿 |
| `tests/test_web_preflight.py` | 断言随之变化 | **保留本地** |
| `tests/test_web_api_routes.py` | 本地新增两条屏蔽名单测试 | **两边都要**：保留本地新增测试，同时保留上游新增的测试 |
| `tests/test_ai_credentials.py` | 本地新增 `model_not_found` 测试 | **保留本地** |
| `tests/test_boss_risk_detection.py` | 本地脚本多返回 `url/severity`，测试改为子集比对 | **保留本地**（配合 `boss.py` 的扩展） |
| `tests/test_boss_collector.py` | 本地显式 `encoding="utf-8"`，修复 Windows cp936 吞 stdout | **保留本地**。上游缺这个修复，Windows 上会失败 |
| `tests/test_task_start_and_scoring_limits.py` | 本地显式 UTF-8 写配置 | **保留本地**，同上 |
| `tests/test_macos_launcher.py` | 本地加 `skipIf(os.name == "nt")` | **保留本地**。上游在 Windows 上会因 chmod/bash 失败 |
| `scripts/windows/start_bosshunter.ps1` | 本地重写了整个启动器（+521/−71）；上游只加了 venv 回退 | **保留本地**。本地顺序是 `-PythonPath` > venv > PATH bosshunter > py/python，比上游（bosshunter > venv > py/python）更稳，无需改 |
| `tests/test_windows_launcher.py` | 跟随启动器契约 | **保留本地**。本地断言已覆盖 venv 优先于 `Get-Command "bosshunter"` 和 `python`，比上游更严格 |

### B. 建议以上游为底、再补本地增量

| 文件 | 冲突原因 | 建议 |
|---|---|---|
| `src/bosshunter/browser/runtime/cdp-proxy.mjs` | 上游新增“仅 WebSocket 的调试端点”支持 + 新 tab 立即绑 session；本地只加了 Electron 过滤 | **以上游为底，只补 `foreignBrowserLabel`**。本地“about:blank 不绑 session”只是省一次握手，但会重新引入上游已修的竞态（新后台标签被 Chrome 冻结后 navigate 失败），应放弃本地这条 |
| `src/bosshunter/collection/orchestrator.py` | 本地 `ignore_checkpoint` 透传；上游改成 `city_yield_policy=...` 显式传参 | **以上游为底，补 `ignore_checkpoint` 三处透传**（`normalize_collection_options`、`validate_collection_options`、构造 `PlatformCollectionRequest`） |
| `src/bosshunter/collection/models.py` | 本地多了 `ignore_checkpoint`、`skipped` 字段 | **以上游为底，补这两个字段**。`ignore_checkpoint` 支撑“从第 1 页重扫”，`skipped` 被本地 orchestrator 和三个平台采集器写入，缺了会 `TypeError` |
| `docs/QUICKSTART.md` | 本地补了“两类时间窗口”说明；上游补了 Windows 首次安装章节 | **两边都要**：保留上游的 Windows 安装章节 + 本地的双窗口说明 |

### C. 建议保留本地版本（其余）

| 文件 | 冲突原因 | 建议 |
|---|---|---|
| `src/bosshunter/throttle.py` | 本地重构成通用 `validate_time_windows(field=...)` + `TimeWindowChecker`；上游只有 `validate_send_windows` | **保留本地**（本地支持采集窗口，是上游的超集），且本地已保留 `validate_send_windows` 兼容上游 |
| `src/bosshunter/config.py` | 本地校验采集窗口；上游只校验发送窗口 | **保留本地**。`remove_retired_collection_settings` 两边都有，无额外工作 |
| `src/bosshunter/web/config_schema.json` | 本地“管培始终放行”；上游“过滤管培 + 实习开关联动薪资过滤” | **保留本地描述**。上游新增字段本地已全部具备，只需保留 `allow_internship` 的本地文案 |
| `src/bosshunter/web/frontend/src/components/dashboard/CollectJobsDialog.tsx` | 本地有“从第 1 页重扫”开关 | **保留本地** |
| `src/bosshunter/web/frontend/src/pages/ConfigPage.tsx` | 本地采集窗口校验 + “接受实习岗位”文案 | **保留本地**。上游新增字段本地已具备 |
| `tests/test_liepin_collector.py` | 本地有 10+ 条采集窗口/断点测试；上游新增 2 条薪资测试 | **保留本地**（本地已含 `test_salary_within_range_collected`，上游那两条本地已有等价版本） |
| `tests/test_boss_search_regressions.py` | 本地新增两条用 `closing()`；上游新增用 `ExitStack()` | **保留本地**，顺手删掉重复的 `from contextlib import closing` |

## 三、自动合并但需要复核的文件（79 个中的重点）

这些文件文本没冲突，但上游同时改了，合并后必须跑测试确认语义：

- `src/bosshunter/ai/greeter.py`（上游 +282/−183）
- `src/bosshunter/ai/resume.py`（上游 +320/−45）
- `src/bosshunter/db.py`（上游 +580/−18）
- `src/bosshunter/executor/sender.py`（上游 +163/−96）
- `src/bosshunter/collection/platforms/job51.py`、`zhilian.py`、`liepin.py`
- `src/bosshunter/web/frontend/src/pages/DashboardPage.tsx`（已列冲突）
- `src/bosshunter/web/frontend/src/globals.css`（上游 +114/−7）
- `tests/test_ai_token_resilience.py`（上游 +345/−76）

## 四、本地独有的 4 个文件（上游完全没有，直接保留）

- `src/bosshunter/collection/platforms/liepin_detail_http.py`
- `src/bosshunter/web/frontend/src/hooks/useBlockCompany.ts`
- `src/bosshunter/web/frontend/src/lib/blockedCompanies.ts`
- `tests/test_liepin_detail_http.py`

## 五、执行顺序建议

1. 先备份当前工作区（`git stash create` 已有快照，另建议复制整个目录）。
2. `git fetch` 后基于 `origin/main` 建分支，把 25 个冲突按上表逐条处理。
3. 每处理完一组就跑对应测试：`pytest tests/test_web_api_routes.py tests/test_web_preflight.py tests/test_boss_collector.py`。
4. 全量跑 `pytest -q` 与前端 `npx vitest run`，再 `npm run build`。
5. 最后单独验证本次两个修复：AI 自检能拦住 404 模型、评分运行时能屏蔽公司。

## 六、风险提示

- `tests/test_macos_launcher.py` 若采用上游版本，在 Windows 上会直接失败（无 chmod/bash）。这是本地必须保留的理由。
- `tests/test_boss_collector.py` 若采用上游版本，Windows 上 Node 子进程会按 cp936 解码，stdout 被吞。同理必须保留本地。
- `throttle.py` 若采用上游版本，会丢失 `collect_windows`（采集时间窗口）能力，配置里的 `collection.collect_windows` 将失效。
