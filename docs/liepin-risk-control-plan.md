# 猎聘风控治理方案

> 依据两个参考项目的实战记录：
> - **JobKoi-AI**（MIT，求职者端，BOSS/猎聘/51job）——`src/boss_zhipin/config/protection.py`、`src/liepin/driver.py`
> - **liepin-cli**（GPL-3.0，招聘者端）——`src/browser/cdp_browser.ts`、`src/common/lpt-utils.ts`
>
> 两者都记录了真实的封号/风控事故和对应的修正，本方案只取其方法论，不复用其代码。

## 0. 结论

猎聘风控的主因不是浏览器指纹——`start_bosshunter.ps1` 已经是独立 profile + 有头 + 无 `--enable-automation`，这一层已经合格。

主因是**访问量与节奏**：改造前 `daily_detail_page_limit` 实际取到 **900**（全局 `collection` 默认是 BOSS 口径，猎聘跟着用），且每个岗位单独开一个标签页完整加载。JobKoi-AI 的猎聘日额是 **15**，并分三个时段投放。两者相差一个数量级，且都集中在一次运行里跑完。

JobKoi-AI `protection.py` 的原话：

> 封号风险 = 日总量 × 时间分布 × 内容相似度，日总量是最强杠杆，「一口气跑满」是典型机器画像。

改造后猎聘的实际节奏（`collection_delay_multiplier` 默认 1.5）：

| 项 | 改造后 |
| --- | --- |
| 日详情页上限 | 60（平台级，独立于 BOSS 的 900） |
| 单轮详情页上限 | 30 |
| 详情页间隔 | 15~27 秒，高斯分布 |
| 翻页间隔 | 30~52.5 秒 |
| 批次休息 | 每 5~8 个详情页歇 3~8 分钟 |
| 换词/换城市 | 40~100 秒高斯停顿 |
| 列表页 | 加载后 3.5~7 秒 + 1~2 次小步滚动 |

## 1. 参考项目的七条做法

| 维度 | 做法 | 出处 |
| --- | --- | --- |
| 日总量 | 猎聘 15/日、BOSS 30/日、51job 10/日 | JobKoi `protection.py` |
| 时间分布 | 三时段 10:00 / 15:00 / 20:00，每轮 `ceil(日额/3)` | 同上 |
| 间隔形状 | 高斯钟形而非均匀分布；档位 15-30 / 20-40 / 30-45 秒 | JobKoi `human_pause` |
| 批次休息 | 连发 5~8 个后歇 3~8 分钟 | JobKoi `apply.py` |
| 换词停顿 | 关键词之间 40~100 秒高斯（旧版 15~30 秒被封过） | 同上 |
| 页面行为 | 加载后 3.5~7 秒高斯 + 随机 1~2 次微滚动 | JobKoi `driver.py` |
| 会话复用 | 一次导航 + 页面内 fetch，不逐页开详情（请求量降一个数量级） | liepin-cli `lpt-utils.ts` |

## 2. 风控信号：四种形态，现有代码只识别一种

| 形态 | 特征 | 现有代码 | 后果 |
| --- | --- | --- | --- |
| 验证/限流文案 | "滑动验证""访问过于频繁" | ✅ 已识别 | 停止并加锁 |
| "账号行为异常"封禁页 | 文案含 `账号行为异常` / `异常访问行为` / `暂时禁止访问`，约 2 小时自动解锁 | ❌ 未识别 | 静默空转，误以为无岗位 |
| 302 到安全验证页 | URL 含 `safe.liepin.com` / `captchaPage` | ❌ 未识别 | 详情页解析失败 |
| 页面被清空 | `about:blank`，body 为空 | ❌ 未识别，**被误判为 `login_required`** | 连续 3 次后进入"只存列表信息"分支，**继续采集** |

最后一条最危险：`JS_EXTRACT_DETAIL` 在 `document.title === '猎聘' && body.length < 400` 时返回 `login_required`，清页正好满足这个条件，于是风控被当成登录墙降级处理，采集继续进行。

## 3. 改造方案

### P0 · 信号补全与正确熔断（✅ 已实施）

1. 风控正则补入封禁页关键词（`账号行为异常` / `异常访问行为` / `暂时禁止访问`）与 `验证码`。
2. 列表页/详情页 JS 增加 URL 判定（`safe.liepin.com` / `captchaPage`）。
3. 增加清页判定（`blanked`）：body 去空白后 < 20 字符；与 `login_required` 区分，
   渲染竞态由既有轮询兜底，重试耗尽后才判 `blocked`。
4. `blocked` / `blanked` → 直接停止 + 写 `risk_event` + 加持久锁。

### P1 · 节奏改造（✅ 已实施）

5. `uniform` 默认实现换成 `_gaussian_delay`（中点均值、1/4 区间宽为标准差，截断保界）。
   测试仍可注入固定 `uniform` 函数。
6. 连开 5~8 个详情页后休息 3~8 分钟。
7. 城市/关键词切换之间插入 40~100 秒高斯停顿（原为 0）。
8. 搜索页加载后 3.5~7 秒高斯停顿 + 1~2 次 700~1200px 小步滚动，替换固定
   `scroll(y=1800)`。

### P2 · 结构降量（✅ 已实施）

9. **详情页标签复用**：`navigate_action` 可用时，一个关键词内所有详情页共用同一个标签，
   不再每岗位 `new_tab` + `close_tab`；导航失败或异常时丢弃重建。
   `navigate_action` 为空时自动退回旧行为（`new_tab(url)` 是唯一导航方式）。
10. **限额改为平台级 + 单轮上限**：
    - `platforms.liepin.search.daily_detail_page_limit`：40（原全局默认 900 是 BOSS 口径，
      对猎聘是 15 倍超发；猎聘现在**不读**全局 `collection` 限额）。
      2026-11-12：60 档实测仍在第 2 页触发风控，降为 40。
    - `platforms.liepin.search.daily_search_page_limit`：40。
    - `platforms.liepin.search.run_detail_page_limit`：30（单轮上限，超出返回
      `completed_with_shortage`）。

### P3 · 详情页 API 化（✅ 2026-11-12 已实施）

**抓包结论**（2026-11-12，CDP Network 被动监听 + 匿名直连验证）：

1. **详情 JD 就在服务端渲染的 HTML 首包里**，不需要任何 XHR 接口。
   锚点是 `dd[data-selector="job-intro-content"]`。
   原假设要逆向 `api-lpt` 那类内部接口是错的——详情页根本没有对应的
   JD 接口，这也解释了浏览器渲染为何能拿到（首包就带）。
2. **未登录的纯 HTTP GET 就能拿到完整 JD**：8/8 成功，无验证页，
   不需要 cookie、XSRF、浏览器。

**实现**（`src/bosshunter/collection/platforms/liepin_detail_http.py`）：

| 项 | 取值 |
| --- | --- |
| 详情页 | `GET https://www.liepin.com/job/<id>.shtml`，纯 HTTP |
| 节奏 | 串行，1.5~3 秒高斯延迟（受 `collection_delay_multiplier` 放大） |
| 搜索页 | **保留浏览器**（登录态 534 条 vs 匿名 494 条，登录态更自然） |
| 失败回退 | `DetailFetchFallback` → 原浏览器流程 |
| 熔断 | HTTP 连续失败 3 次后本轮剩余岗位直接走浏览器 |
| 开关 | `platforms.liepin.search.http_detail_enabled`（默认 true） |

**风控信号不降级**：HTTP 路径看到验证/限流文案仍返回 `blocked` +
`risk_event` + 持久锁，**不**回退——回退会把风控信号吞掉，这是 P0 已修的缺陷。

**未验证的风险**：搜索接口（`pc-search-job`）匿名也能调（494 条 / 10 页，
每页 40），但当前**没有**改用它，因为：
- 登录态数据更全（534 vs 494）；
- 完全脱离浏览器是更强的爬虫画像；
- 搜索只占请求量小头（每词每页 1 次），详情页才是大头（每岗位 1 次）。

若后续详情页 HTTP 稳定且 `risk_events` 干净，可再评估搜索接口化。

## 4. 关键词配置

关键词数量直接决定请求量：`城市数 × 关键词数 = 搜索页请求数`，每个关键词一页约 20 个岗位进详情页。

按当前限额（日 40 / 单轮 30），**单轮实际只能扫 2 个关键词**。配 6 个词时后 4 个永远排队，等于白配。

### 按历史高分率选词

用本地 `jobs` 表按 `source_keyword` 统计（高分 = `score >= 70`）：

| 关键词 | 采到 | 高分 | 高分率 | 结论 |
| --- | --- | --- | --- | --- |
| 文员 | 85 | 60 | 71% | 保留，主力 |
| AI内容运营 | 43 | 18 | 42% | 保留 |
| 行政 | 104 | 34 | 33% | 保留 |
| 运营 | 81 | 20 | 25% | 与内容运营重叠，可合并 |
| AI产品运营 | 25 | 4 | 16% | 降级 |
| AI训练师 | 35 | 6 | 17% | 砍 |
| 公共事务 | 26 | 2 | 8% | 砍 |
| AI测试 | 21 | 1 | 5% | 砍，纯烧额度 |
| pr / 直播中控 | 0 | 0 | — | 猎聘搜不到，留给 BOSS |

### 默认配置

```yaml
platforms:
  liepin:
    search:
      keywords: [文员, 行政, AI内容运营]
      max_pages: 1
```

三条原则：

1. **不堆同义词**。猎聘搜索有模糊匹配：搜「文员」已带出文职/前台文员/办公室文职；搜「AI内容运营」已带出新媒体运营/内容编辑。堆「内容运营/运营/AI产品运营」只是重复覆盖同一批岗位。
2. **一个词覆盖一个岗位族**。文员（文职）、行政（行政后勤）、AI内容运营（内容运营）互不重叠。
3. **宽泛词与精准词按平台分工**。搜索命中率低的词（AI测试、AI训练师）留给 `max_pages` 更大的 BOSS，猎聘只用高分率 ≥30% 的短词。

### 配套：过滤前移

把 `profile.salary_min`、`deal_breakers` 配到位，列表阶段就筛掉不匹配岗位，省下详情页额度——这些岗位本来也不会进详情页。

## 5. 许可证

- JobKoi-AI：**MIT**，可参考、可吸收实现思路。
- liepin-cli：**GPL-3.0**，只可借鉴设计，不得复制源码（本仓库为 PolyForm Noncommercial）。

## 6. 验证方式

1. 单城市单关键词跑一轮，确认 `detail_page` 计数与实际打开数一致。
2. 手工制造清页/封禁页（或注入文案），确认返回 `blocked` 而非 `login_required`。
3. 连续运行 3 天，观察 `risk_event` 表是否新增 `collection_liepin_rate_limit`。
4. **P3 后新增**：确认日志里出现「详情页 HTTP 间隔」，且浏览器标签页不再
   逐个打开详情页；`risk_events` 若仍出现 `collection_liepin_rate_limit`，
   看 detail 是否带 `(HTTP)` 后缀以区分是哪条路径触发。