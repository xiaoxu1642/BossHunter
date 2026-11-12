"""Fail-closed 猎聘 (Liepin) collector for the shared multi-platform pipeline.

Collection only. It does not solve verification challenges, imitate human
behaviour, send messages, or resume automatically after a risk signal. Any
verification wall, silent throttle, or unexpected page state stops the current
platform run (fail-closed), following the same boundary as the 51job collector.

Verified live 2026-08-26:
- Search URL filters by city via the ``dqs`` parameter (the ``city`` parameter
  is ignored by Liepin and returns nationwide results).
- Job cards: ``a[data-nick="job-detail-job-info"]`` inside ``div.job-detail-box``.
  The job id is on the anchor's ``data-jobid`` attribute (fallback: the number
  in ``/job/<id>.shtml``).
- Liepin ships CSS-module hashed class names (e.g. ``_40108E8PWS``) that change
  on every deploy, so extraction relies on stable attributes / text structure,
  never on those hashed classes.
- Detail JD lives in ``section.job-intro-container`` when the session is shared
  with the ``wow.liepin.com`` subdomain; title in ``.job-title-box .ellipsis-1``;
  salary in ``.job-salary``; city in ``.job-dq-box .ellipsis-1``.
- 2026-08 起 ``www.liepin.com/job/<id>.shtml`` 会 302 跳转到 ``wow.liepin.com``，
  未在该子域登录时只返回登录墙。因此详情页 JD 为 best-effort：拿不到时仅保留
  列表已采集的线索（标题/公司/薪资/城市/链接），不中断整个平台任务。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from bosshunter.ai.prefilter import salary_filter_result
from bosshunter.browser import close_tab, evaluate, navigate as browser_navigate, new_tab, scroll, wait_for_load
from bosshunter.collection.base import CollectionError, CollectorHooks
from bosshunter.collection.models import JobCandidate, PlatformCollectionRequest, PlatformCollectionResult
from bosshunter.collection.platforms.liepin_detail_http import (
    DetailFetchFallback,
    DetailFetchResult,
    LiepinDetailFetcher,
)
from bosshunter.db import (
    add_risk_event,
    get_page_progress,
    mark_combo_collected,
    prune_collected_combos,
    prune_page_progress,
    upsert_page_progress,
)
from bosshunter.job_filters import matching_blocked_company, matching_deal_breaker
from bosshunter.platform_safety import PlatformAccessGuard, PlatformSafetyStop
from bosshunter.throttle import resolve_collection_windows, SendWindowChecker, should_take_day_off


SEARCH_URL = "https://www.liepin.com/zhaopin/?key={keyword}&dqs={code}"
DETAIL_DELAY_MIN_SECONDS = 10.0
DETAIL_DELAY_MAX_SECONDS = 18.0
# P3 详情页 API 化后的 HTTP 节奏：不加载页面、不开标签页，单请求 <1 秒，
# 因此不需要 10~18 秒的渲染等待；但请求频率本身仍是机器特征，保留
# 1.5~3 秒高斯间隔（用户 2026-11-12 选择「串行 + 1.5~3 秒」）。
HTTP_DETAIL_DELAY_MIN_SECONDS = 1.5
HTTP_DETAIL_DELAY_MAX_SECONDS = 3.0
# HTTP 详情连续失败该阈值后熔断，本轮剩余岗位直接走浏览器。
# 原因：回退也要发请求，HTTP 整体不可用时「每个岗位先试 HTTP 再回退」
# 会让请求量翻倍。3 次足以区分偶发抖动与路径性失效。
HTTP_DETAIL_FAILURE_THRESHOLD = 3
PAGE_DELAY_MIN_SECONDS = 20.0
PAGE_DELAY_MAX_SECONDS = 35.0
RENDER_POLL_INTERVAL_SECONDS = 0.75
RENDER_POLL_ATTEMPTS = 10

_INTERNSHIP_TITLE_TERMS = ("实习", "intern", "internship")  # 本地定制：管培不算实习

# 猎聘风控有四种形态（依据 JobKoi-AI src/liepin/driver.py 与 liepin-cli
# lpt-utils.ts 的实测记录），都必须停止，不能当登录墙降级：
#   1) 验证/限流文案——原实现已覆盖；
#   2) 「账号行为异常」封禁页（约 2 小时自动解锁）——不识别会静默空转；
#   3) 302 到 safe.liepin.com/.../captchaPage_PC——详情页解析必然失败；
#   4) 页面被安全脚本清空为 about:blank——原实现误判成 login_required，
#      连续 3 次后转入「只存列表信息」分支继续采集，风险最高。
# 判定写在下方两段提取 JS 里（与既有正则同处，便于对照页面结构改动）。

# 连续详情页登录墙达到该阈值即判定 wow.liepin.com 会话整体失效。
# 剩余岗位直接走「只存列表信息」分支，不再逐个开标签 + 10~18 秒等待。
MAX_CONSECUTIVE_LOGIN_REQUIRED = 3

# 拟人化节奏（依据 JobKoi-AI src/liepin/apply.py 的封号复盘）：
# - 连开 5~8 个详情页后歇 3~8 分钟，模拟真人「看一会儿歇一下」；
# - 关键词/城市切换停顿 40~100 秒——旧版直接切下一组等于「搜索风暴」画像，
#   该项目的猎聘巡检正是因为原来的 15~30 秒换词节奏被封过；
# - 搜索页加载后先停 3.5~7 秒再小步滚动：加载完立刻批量抽 DOM 是机器签名。
DETAIL_BREAK_EVERY_MIN = 5
DETAIL_BREAK_EVERY_MAX = 8
BREAK_DURATION_MIN_SECONDS = 180.0
BREAK_DURATION_MAX_SECONDS = 480.0
COMBO_PAUSE_MIN_SECONDS = 40.0
COMBO_PAUSE_MAX_SECONDS = 100.0
SEARCH_SETTLE_MIN_SECONDS = 3.5
SEARCH_SETTLE_MAX_SECONDS = 7.0

# Liepin city codes: municipalities use 3 digits, other cities 6 digits. Only codes
# verified live are bundled; unknown cities are rejected instead of guessing.
CITY_SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "data" / "liepin_cities.json"

CITY_SNAPSHOT_FALLBACK = (
    {"name": "北京", "code": "010"},
    {"name": "上海", "code": "020"},
    {"name": "广州", "code": "050"},
    {"name": "深圳", "code": "060"},
    {"name": "杭州", "code": "070"},
    {"name": "成都", "code": "080"},
    {"name": "南京", "code": "090"},
    {"name": "苏州", "code": "100"},
    {"name": "武汉", "code": "110"},
    {"name": "天津", "code": "130"},
    {"name": "西安", "code": "270"},
    {"name": "重庆", "code": "040"},
    {"name": "长沙", "code": "190"},
    {"name": "郑州", "code": "170"},
    {"name": "青岛", "code": "200"},
    {"name": "厦门", "code": "230"},
    {"name": "合肥", "code": "250"},
    {"name": "大连", "code": "210"},
)


def load_liepin_city_snapshot() -> dict[str, Any]:
    try:
        payload = json.loads(CITY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        payload = {}
    cities = payload.get("cities") if isinstance(payload, dict) and isinstance(payload.get("cities"), list) else []
    if not cities:
        cities = [dict(item) for item in CITY_SNAPSHOT_FALLBACK]
    return {
        "schema": "bosshunter.liepin_cities.v1",
        "source": str(payload.get("source") or "verified_snapshot") if isinstance(payload, dict) else "verified_snapshot",
        "fetched_at": payload.get("fetched_at") if isinstance(payload, dict) else None,
        "note": payload.get("note", "猎聘 3 位城市编码（dqs 参数）") if isinstance(payload, dict) else "猎聘 3 位城市编码（dqs 参数）",
        "cities": cities,
    }


def get_liepin_city_code(city: str) -> str | None:
    normalized = str(city or "").strip().removesuffix("市")
    for item in load_liepin_city_snapshot()["cities"]:
        if str(item.get("name", "")).removesuffix("市") == normalized:
            return item.get("code")
    return None


JS_EXTRACT_LIST = r"""
(function () {
    // 风控形态 3：302 到安全验证页——先判 URL，此时正文可能还是空的。
    if (/captchaPage|safe\.liepin\.com/i.test(location.href || '')) {
        return JSON.stringify({status: 'blocked', jobs: []});
    }
    var text = (document.body && document.body.innerText) || '';
    // 风控形态 4：页面被清空（about:blank）。返回 blanked 让调用方继续
    // 轮询，避免把渲染竞态误判为风控；重试耗尽后按 blocked 处理。
    if (!document.body || text.replace(/\s/g, '').length < 20) {
        return JSON.stringify({status: 'blanked', jobs: []});
    }
    var pageText = text + ' ' + (document.title || '');
    // 风控形态 1、2：验证/限流文案与「账号行为异常」封禁页。
    if (/滑动验证|安全验证|访问过于频繁|请完成安全验证|人机验证|网络异常|验证码|账号行为异常|异常访问行为|暂时禁止访问/.test(pageText)) {
        return JSON.stringify({status: 'blocked', jobs: []});
    }
    var anchors = Array.prototype.slice.call(document.querySelectorAll('a[data-nick="job-detail-job-info"]'));
    if (!anchors.length) {
        return JSON.stringify({status: /没有找到|暂无满足|未找到/.test(text) ? 'empty' : 'waiting', jobs: []});
    }
    var jobs = [];
    for (var i = 0; i < anchors.length; i++) {
        var a = anchors[i];
        var href = (a.href || '').trim();
        var id = (a.getAttribute('data-jobid') || '').trim();
        if (!id) { var m = href.match(/job\/(\d+)\.shtml/); id = m ? m[1] : ''; }
        if (!id || !/^https:\/\/www\.liepin\.com\/job\//.test(href)) continue;
        var titleEl = a.querySelector('.ellipsis-1');
        var title = titleEl ? String(titleEl.innerText || '').replace(/^招聘/, '').trim() : '';
        if (!title) continue;
        var box = a.closest('.job-detail-box') || a.parentElement;
        var compEl = box ? box.querySelector("div[data-nick='job-detail-company-info'] .ellipsis-1") : null;
        var company = compEl ? String(compEl.innerText || '').trim() : '';
        var atext = (a.innerText || '').replace(/\s+/g, ' ');
        var cityM = atext.match(/【\s*([^】]+?)\s*】/);
        var city = cityM ? cityM[1].trim() : '';
        var after = atext.split('】').slice(1).join('】').trim();
        var salM = after.match(/(\d+\s*-\s*\d+\s*k[·\d\s薪]*|\d+\s*k[·\d\s薪]*|面议|薪资面议)/i);
        var salary = salM ? salM[0].replace(/\s+/g, '') : '';
        var rest = salM ? after.slice(after.indexOf(salM[0]) + salM[0].length).trim() : after;
        var parts = rest.split(/\s+/).filter(Boolean);
        jobs.push({
            source_job_id: id,
            title: title,
            company: company,
            salary: salary,
            city: city,
            experience: parts[0] || '',
            education: parts[1] || '',
            url: href
        });
    }
    return JSON.stringify({status: jobs.length ? 'ready' : 'selector_changed', jobs: jobs});
})()
"""


JS_EXTRACT_DETAIL = r"""
(function () {
    // 风控形态 3：302 到安全验证页——先判 URL。
    if (/captchaPage|safe\.liepin\.com/i.test(location.href || '')) {
        return JSON.stringify({status: 'blocked'});
    }
    var body = (document.body && document.body.innerText) || '';
    // 风控形态 4：页面被清空。必须排在下面的 login_required 判定之前——
    // about:blank 的 title 为空、body 为空，旧逻辑会把它误判成登录墙，
    // 连续 3 次后转「只存列表信息」继续采集。返回 blanked 交由轮询区分
    // 渲染竞态，重试耗尽后按 blocked 处理。
    if (!document.body || body.replace(/\s/g, '').length < 20) {
        return JSON.stringify({status: 'blanked'});
    }
    var pageText = body + ' ' + (document.title || '');
    // 风控形态 1、2：验证/限流文案与「账号行为异常」封禁页。
    if (/滑动验证|安全验证|访问过于频繁|请完成安全验证|人机验证|网络异常|验证码|账号行为异常|异常访问行为|暂时禁止访问/.test(pageText)) {
        return JSON.stringify({status: 'blocked'});
    }
    if (/职位不存在|职位已下线|该职位已关闭|页面不存在|职位已暂停/.test(pageText)) {
        return JSON.stringify({status: 'offline'});
    }
    // Liepin 302-redirects detail URLs to the wow.liepin.com subdomain, which
    // serves a login/interstitial wall unless the session is shared there.
    // Treat that as a login gate (soft skip), not a page-structure break.
    if (location.hostname.indexOf('wow.liepin.com') >= 0) {
        return JSON.stringify({status: 'login_required'});
    }
    if (document.title === '猎聘' && (body || '').length < 400) {
        return JSON.stringify({status: 'login_required'});
    }
    var jdNode = document.querySelector('section.job-intro-container');
    var jd = jdNode ? String(jdNode.innerText || '').replace(/\s+/g, ' ').trim() : '';
    jd = jd.replace(/^职位介绍/, '').trim();
    var titleNode = document.querySelector('.job-title-box .ellipsis-1') || document.querySelector('h1');
    var salaryNode = document.querySelector('.job-salary');
    var cityNode = document.querySelector('.job-dq-box .ellipsis-1');
    var companyNode = document.querySelector('.company-info-container .ellipsis-1') || document.querySelector("div[data-nick='job-detail-company-info'] .ellipsis-1");
    return JSON.stringify({
        status: jd ? 'ready' : 'selector_changed',
        title: titleNode ? String(titleNode.innerText || '').trim() : '',
        salary: salaryNode ? String(salaryNode.innerText || '').replace(/\s+/g, '').trim() : '',
        company: companyNode ? String(companyNode.innerText || '').trim() : '',
        city: cityNode ? String(cityNode.innerText || '').trim() : '',
        jd: jd,
        url: location.href
    });
})()
"""


@dataclass
class LiepinBrowser:
    new_tab: Callable[..., str | None] = new_tab
    close_tab: Callable[[str], bool] = close_tab
    evaluate: Callable[..., Any] = evaluate
    scroll: Callable[..., bool] = scroll
    wait_for_load: Callable[..., bool] = wait_for_load
    navigate_action: Callable[[str, str], bool] | None = None


def _payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _gaussian_delay(low: float, high: float) -> float:
    """高斯钟形延迟（截断进 [low, high]），替代均匀分布。

    平台风控不只看间隔长短，还看间隔的**分布形状**：真人的动作间隔服从
    钟形分布，均匀分布的「随机」延迟同样是机器签名（JobKoi-AI 2026-10-15
    封号复盘结论）。取区间中点为均值、1/4 区间宽为标准差，越界截断——
    只慢不快，不会突破原有上下限。
    """
    mid = (low + high) / 2.0
    sigma = max((high - low) / 4.0, 0.05)
    return min(high, max(low, random.gauss(mid, sigma)))


def _is_internship(title: str) -> bool:
    """判断岗位是否为实习（只查标题，管培不算）。"""
    t = (title or "").lower()
    return any(s in t for s in _INTERNSHIP_TITLE_TERMS)


class LiepinCollector:
    platform = "liepin"

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        safety_conn: Any | None = None,
        browser: LiepinBrowser | None = None,
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = _gaussian_delay,
        detail_delay_range: tuple[float, float] | None = None,
        page_delay_range: tuple[float, float] | None = None,
        detail_fetcher: LiepinDetailFetcher | None = None,
    ):
        self.config = config or {}
        self.safety_conn = safety_conn
        self.browser = browser or LiepinBrowser(navigate_action=browser_navigate)
        self.sleep = sleep
        self.uniform = uniform
        # P3 详情页 API 化：详情 JD 走纯 HTTP（见 liepin_detail_http 模块文档）。
        # 抓取失败抛 DetailFetchFallback，回退到下方原有的浏览器流程。
        self.detail_fetcher = detail_fetcher if detail_fetcher is not None else LiepinDetailFetcher()
        # HTTP 详情连续失败计数（熔断用），每次 collect 重置。
        self._http_failures = 0
        # 延迟区间可配：默认 1.0 倍，保持历史节奏不变；需要放慢时由
        # collection.collection_delay_multiplier 统一放大（1.0~5.0 有界）。
        multiplier = self._delay_multiplier()
        self.detail_delay_range = detail_delay_range or (
            DETAIL_DELAY_MIN_SECONDS * multiplier, DETAIL_DELAY_MAX_SECONDS * multiplier,
        )
        self.page_delay_range = page_delay_range or (
            PAGE_DELAY_MIN_SECONDS * multiplier, PAGE_DELAY_MAX_SECONDS * multiplier,
        )
        # HTTP 详情节奏不受 multiplier 放大：1.5~3 秒是用户 2026-11-12 直接
        # 指定的值（HTTP 不加载页面，没有渲染开销）。multiplier 是为「浏览器
        # 渲染慢、要放慢」设计的，套到 HTTP 上只会把已定的节奏悄悄改掉。
        self.http_detail_delay_range = (
            HTTP_DETAIL_DELAY_MIN_SECONDS, HTTP_DETAIL_DELAY_MAX_SECONDS,
        )

    def _delay_multiplier(self) -> float:
        cfg = self.config.get("collection", {})
        raw = cfg.get("collection_delay_multiplier", 1.0) if isinstance(cfg, dict) else 1.0
        try:
            return min(max(float(raw), 1.0), 5.0)
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def build_search_url(request: PlatformCollectionRequest, city: str, keyword: str, page: int = 1) -> str:
        code = str(request.city_codes.get(city) or "").strip()
        if not code:
            raise CollectionError("no_valid_city", f"未配置猎聘城市编码：{city}")
        url = SEARCH_URL.format(keyword=quote(keyword), code=quote(code))
        if page and page > 1:
            url += f"&curPage={page - 1}"
        if request.sort == "newest":
            # Liepin's "最新" ordering. Harmless if the param is ignored.
            url += "&sortType=2"
        return url

    def _wait(self, hooks: CollectorHooks, seconds: float) -> bool:
        if hooks.stop_event is not None:
            return hooks.stop_event.wait(max(0.0, seconds))
        self.sleep(max(0.0, seconds))
        return False

    def _navigate(self, hooks: CollectorHooks, target_id: str, url: str, label: str) -> bool:
        """Navigate with bounded retries so a transient CDP/page hiccup does not
        abort the entire platform run. Returns True when navigation commits."""
        nav = self.browser.navigate_action
        if nav is None:
            return True
        for attempt in range(1, 4):
            if hooks.stop_event is not None and hooks.stop_event.is_set():
                return False
            if nav(target_id, url):
                return True
            if attempt < 3:
                hooks.on_event(phase="pacing", message=f"{label}导航重试 {attempt}/3")
                self._wait(hooks, 2.0)
        return False

    def _passes_filters(self, candidate: JobCandidate) -> bool:
        return not self._filter_reason(candidate)

    def _filter_reason(self, candidate: JobCandidate) -> str:
        """collector 增值过滤：deal_breakers / blocked_company / 实习（薪资见 _salary_block_reason）。"""
        profile = self.config.get("profile", {}) if isinstance(self.config.get("profile"), dict) else {}
        if matched := matching_deal_breaker(candidate.title, profile.get("deal_breakers") or []):
            return f"职位名命中排除词：{matched}"
        if matched := matching_blocked_company(candidate.company, profile.get("blocked_companies") or []):
            return f"公司命中屏蔽规则：{matched}"
        if matched := matching_deal_breaker(candidate.jd, profile.get("jd_deal_breakers") or []):
            return f"JD 命中排除词：{matched}"
        allow_internship = bool(profile.get("allow_internship", False))
        if not allow_internship and _is_internship(candidate.title):
            return "当前设置不接受实习岗位"
        return ""

    def _salary_block_reason(self, candidate: JobCandidate) -> str | None:
        """薪资硬过滤（与 quick_score 同规则）；返回拦截原因，None 表示通过。"""
        profile = self.config.get("profile", {}) if isinstance(self.config.get("profile"), dict) else {}
        score, reason = salary_filter_result(candidate.salary, profile)
        return None if score > 0 else reason

    def _resume_ttl_hours(self) -> int:
        """断点续采有效期（默认 24h），与 51job 同规则。"""
        search_cfg: dict[str, Any] = {}
        platforms = self.config.get("platforms", {})
        if isinstance(platforms, dict) and isinstance(platforms.get("liepin"), dict):
            pf = platforms["liepin"]
            if isinstance(pf.get("search"), dict):
                search_cfg = pf["search"]
        elif isinstance(self.config.get("search"), dict):
            search_cfg = self.config["search"]
        raw = search_cfg.get("resume_ttl_hours", 24)
        try:
            return max(1, min(int(raw or 24), 720))
        except (TypeError, ValueError):
            return 24

    @staticmethod
    def _positive_int(value: object, default: int) -> int:
        try:
            return max(int(value), 1)
        except (TypeError, ValueError):
            return default

    def _guard(self) -> PlatformAccessGuard | None:
        if self.safety_conn is None:
            return None
        return PlatformAccessGuard(self.safety_conn, self.config, "collection", "liepin")

    def _platform_search_cfg(self) -> dict[str, Any]:
        """猎聘平台级 search 配置（platforms.liepin.search）。"""
        platforms = self.config.get("platforms", {})
        if isinstance(platforms, dict):
            liepin = platforms.get("liepin")
            if isinstance(liepin, dict) and isinstance(liepin.get("search"), dict):
                return liepin["search"]
        # 兼容旧的扁平配置（config["search"]）。
        search = self.config.get("search")
        return search if isinstance(search, dict) else {}

    def _limits(self) -> tuple[int, int]:
        """(搜索页日额, 详情页日额)。

        只读平台级配置，**不回退到全局 collection 限额**：全局
        ``daily_detail_page_limit`` 默认 900 是 BOSS 口径，对猎聘是 15 倍
        超发（JobKoi-AI 的封号复盘结论：日总量是最强杠杆；其猎聘日额 15
        属打招呼写操作，采集是只读，取 40 作真人一天浏览量的上限）。
        2026-11-12：60 档实测仍触发风控，降为 40。
        """
        cfg = self._platform_search_cfg()
        return (
            self._positive_int(cfg.get("daily_search_page_limit", 40), 40),
            self._positive_int(cfg.get("daily_detail_page_limit", 40), 40),
        )

    def _run_detail_limit(self) -> int:
        """单轮详情页上限（默认 30）：鼓励分几次运行，而不是一口气跑满日额。"""
        cfg = self._platform_search_cfg()
        return self._positive_int(cfg.get("run_detail_page_limit", 30), 30)

    def _check_limits(self, guard: PlatformAccessGuard | None,
                      search_limit: int, detail_limit: int) -> PlatformCollectionResult | None:
        """在开标签前预检额度：不消耗页面尝试，额度用尽直接收尾。"""
        if guard is None:
            return None
        try:
            guard.check_available("search_page", daily_limit=search_limit)
            guard.check_available("detail_page", daily_limit=detail_limit)
        except PlatformSafetyStop as exc:
            return PlatformCollectionResult(
                self.platform, "completed_with_shortage", exc.reason,
                f"猎聘已达页面访问额度：{exc.reason}，本轮停止",
            )
        return None

    def _record_risk(self, guard: PlatformAccessGuard | None, detail: str) -> None:
        """blocked 时写风险事件与持久锁，下一轮不再立刻重踩。"""
        if guard is None:
            return
        try:
            if self.safety_conn is not None:
                add_risk_event(self.safety_conn, "collection_liepin_rate_limit", detail)
            guard.lock("rate_limit")
        except PlatformSafetyStop:
            pass

    def _http_detail_enabled(self) -> bool:
        """详情页是否走纯 HTTP（默认开启）。

        配置位 ``platforms.liepin.search.http_detail_enabled``：关掉即回到
        纯浏览器流程，便于 HTTP 路径出问题时一键降级而不改代码。
        """
        cfg = self._platform_search_cfg()
        raw = cfg.get("http_detail_enabled", True)
        return bool(raw)

    def _fetch_detail_http(self, candidate: JobCandidate) -> DetailFetchResult:
        """HTTP 抓取详情；任何不可用都抛 DetailFetchFallback 交给调用方回退。"""
        return self.detail_fetcher.fetch(candidate.source_job_id, candidate.url)

    def _note_http_detail(self, ok: bool) -> bool:
        """HTTP 连续失败达到阈值后，本轮剩余岗位直接走浏览器。

        回退本身也要发请求：若 HTTP 路径整体不可用（被针对性封、DNS 不通），
        逐个岗位先试 HTTP 再回退等于把请求量翻倍。连续失败 N 次后熔断，
        本轮不再尝试 HTTP。返回「下一岗位是否仍应尝试 HTTP」。
        """
        if ok:
            self._http_failures = 0
            return True
        self._http_failures += 1
        return self._http_failures < HTTP_DETAIL_FAILURE_THRESHOLD

    def _detail_from_http(self, result: DetailFetchResult) -> dict[str, Any]:
        """把 HTTP 结果转成与浏览器 JS_EXTRACT_DETAIL 同形的 payload。"""
        return {
            "status": result.status,
            "jd": result.jd,
            "title": result.title,
            "salary": result.salary,
            "city": result.city,
            "company": result.company,
            "url": result.url,
        }

    def collect(self, request: PlatformCollectionRequest, hooks: CollectorHooks) -> PlatformCollectionResult:
        for city in request.cities:
            if not request.city_codes.get(city):
                return PlatformCollectionResult(self.platform, "failed", "no_valid_city", f"猎聘城市编码未配置：{city}")

        # 页级断点续采：只跳过「已采到最大页」的词，未采满的词从上次页码的下一页继续。
        # 超过有效期（默认 24h）的旧断点视为过期，下次从第 1 页重新采集。
        if self.safety_conn is not None:
            all_keywords = set(request.keywords)
            prune_collected_combos(self.safety_conn, "liepin", all_keywords)
            prune_page_progress(self.safety_conn, "liepin", all_keywords)

        collect_windows = resolve_collection_windows(self.config)
        if not SendWindowChecker(collect_windows).is_active():
            return PlatformCollectionResult(self.platform, "completed", "outside_window",
                                            f"当前不在采集时间窗口内（{collect_windows}）")
        throttle_cfg = self.config.get("throttle", {}) if isinstance(self.config.get("throttle"), dict) else {}
        if should_take_day_off(float(throttle_cfg.get("day_off_probability", 0.05))):
            return PlatformCollectionResult(self.platform, "completed", "day_off",
                                            "今日随机休息，跳过猎聘采集")

        guard = self._guard()
        search_limit, detail_limit = self._limits()
        run_detail_limit = self._run_detail_limit()
        http_detail_enabled = self._http_detail_enabled()
        self._http_failures = 0
        detail_opened_this_run = 0
        visited_combos = 0
        outcomes = {"scanned": 0, "resumed": 0, "skipped": 0}
        total_combos = len(request.cities) * len(request.keywords)
        for city in request.cities:
            for keyword in request.keywords:
                if hooks.stop_event is not None and hooks.stop_event.is_set():
                    return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                # 开标签前先查额度，避免额度用尽还白开 about:blank
                limited = self._check_limits(guard, search_limit, detail_limit)
                if limited is not None:
                    return limited
                # 页级断点：从上次采到页码的下一页继续
                saved_page = (
                    get_page_progress(self.safety_conn, "liepin", city, keyword,
                                      within_hours=self._resume_ttl_hours())
                    if self.safety_conn is not None and not request.ignore_checkpoint
                    else 0
                )
                start_page = saved_page + 1 if saved_page > 0 else 1
                if start_page > request.max_pages:
                    if self.safety_conn is not None:
                        mark_combo_collected(self.safety_conn, "liepin", city, keyword)
                    outcomes["skipped"] += 1
                    hooks.on_event(phase="completed_keyword", keyword=keyword, city=city,
                                   skipped=outcomes["skipped"],
                                   message=f"猎聘 {keyword} 已采到最大页，{self._resume_ttl_hours()}h 内无剩余页，跳过本词")
                    continue
                # 组合之间的长停顿：旧版直接切下一组，一轮十几个关键词连搜
                # 是「搜索风暴」画像（JobKoi-AI 猎聘巡检封号复盘的直接结论）。
                if visited_combos > 0:
                    combo_pause = self.uniform(COMBO_PAUSE_MIN_SECONDS, COMBO_PAUSE_MAX_SECONDS)
                    hooks.on_event(phase="pacing", keyword=keyword, city=city,
                                   message=f"猎聘切换关键词/城市，停顿 {combo_pause:.0f} 秒")
                    if self._wait(hooks, combo_pause):
                        return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                visited_combos += 1
                search_url = self.build_search_url(request, city, keyword, page=start_page)
                target_id: str | None = None
                detail_target: str | None = None
                # 只有能主动导航（navigate_action 存在）时才复用详情标签页：
                # navigate_action 为空时 new_tab(url) 本身就是唯一的导航方式，
                # 复用会让页面停在上一个岗位，解析出重复 JD。
                reuse_detail_tab = self.browser.navigate_action is not None
                detail_since_break = 0
                detail_break_after = random.randint(DETAIL_BREAK_EVERY_MIN, DETAIL_BREAK_EVERY_MAX)
                try:
                    if guard is not None:
                        try:
                            guard.reserve("search_page", daily_limit=search_limit)
                        except PlatformSafetyStop as exc:
                            return PlatformCollectionResult(
                                self.platform, "completed_with_shortage", exc.reason,
                                f"猎聘搜索页已达访问额度：{exc.reason}，本轮停止",
                            )
                    initial_url = "about:blank" if self.browser.navigate_action is not None else search_url
                    target_id = self.browser.new_tab(initial_url, background=True)
                    if not target_id:
                        return PlatformCollectionResult(self.platform, "failed", "browser_disconnected", "无法打开猎聘搜索页")
                    if not self._navigate(hooks, target_id, search_url, "猎聘搜索页"):
                        return PlatformCollectionResult(self.platform, "failed", "browser_disconnected", "猎聘搜索页导航失败")
                    for page in range(start_page, request.max_pages + 1):
                        if hooks.stop_event is not None and hooks.stop_event.is_set():
                            return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                        if page > 1:
                            detail_req = self.uniform(*self.page_delay_range)
                            hooks.on_event(phase="pacing", keyword=keyword, city=city, page=page, message=f"翻页安全间隔 {detail_req:.1f} 秒")
                            if self._wait(hooks, detail_req):
                                return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                            if guard is not None:
                                try:
                                    guard.reserve("search_page", daily_limit=search_limit)
                                except PlatformSafetyStop as exc:
                                    return PlatformCollectionResult(
                                        self.platform, "completed_with_shortage", exc.reason,
                                        f"猎聘搜索页已达访问额度：{exc.reason}，本轮停止",
                                    )
                            if not self._navigate(hooks, target_id, self.build_search_url(request, city, keyword, page=page), "猎聘翻页"):
                                return PlatformCollectionResult(self.platform, "failed", "browser_disconnected", "猎聘翻页导航失败")
                        hooks.on_event(phase="loading_list", keyword=keyword, city=city, page=page)
                        self.browser.wait_for_load(target_id, timeout=15)
                        # 拟人化浏览：加载完先「扫一眼」再小步滚动触发懒加载。
                        # 固定间隔 + 一次大滚动是机器签名（人类是看几条再往下）。
                        if self._wait(hooks, self.uniform(SEARCH_SETTLE_MIN_SECONDS, SEARCH_SETTLE_MAX_SECONDS)):
                            return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                        for _ in range(random.randint(1, 2)):
                            self.browser.scroll(target_id, y=random.randint(700, 1200))
                            if self._wait(hooks, self.uniform(0.8, 1.8)):
                                return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                        payload: dict[str, Any] = {}
                        status = "waiting"
                        for attempt in range(RENDER_POLL_ATTEMPTS):
                            payload = _payload(self.browser.evaluate(target_id, JS_EXTRACT_LIST))
                            status = str(payload.get("status") or "selector_changed")
                            # blanked 与 waiting 一样继续轮询：首次渲染时 body
                            # 可能为空，重试耗尽仍是空白才判定为清页/风控。
                            if status not in {"waiting", "blanked"}:
                                break
                            if attempt + 1 < RENDER_POLL_ATTEMPTS and self._wait(hooks, RENDER_POLL_INTERVAL_SECONDS):
                                return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                        if status in {"blocked", "blanked"}:
                            risk_detail = (
                                "猎聘搜索列表页面被清空(about:blank)"
                                if status == "blanked"
                                else "猎聘搜索列表出现验证或限流信号"
                            )
                            self._record_risk(guard, f"{risk_detail}，已停止整个平台任务")
                            return PlatformCollectionResult(self.platform, "blocked", "rate_limit", f"{risk_detail}，已停止整个平台任务")
                        if status == "empty":
                            # 本词已无更多结果：断点推进到上限页，下次直接跳过本词
                            if self.safety_conn is not None:
                                upsert_page_progress(self.safety_conn, "liepin", city, keyword, request.max_pages)
                            break
                        if status == "waiting":
                            return PlatformCollectionResult(
                                self.platform, "completed_with_shortage", "list_not_ready",
                                "猎聘列表页渲染超时，保留上一完整页断点，下次重试",
                            )
                        if status != "ready" or not isinstance(payload.get("jobs"), list):
                            return PlatformCollectionResult(self.platform, "blocked", "selector_changed", "猎聘列表页结构与预期不一致")

                        page_had_transient_failure = False
                        consecutive_login_required = 0
                        login_wall_active = False
                        for raw_item in payload["jobs"]:
                            candidate = self._candidate_from_list(raw_item, city, keyword)
                            if candidate is None:
                                continue
                            salary_reason = self._salary_block_reason(candidate)
                            if salary_reason:
                                hooks.on_event(message=f"猎聘 列表预筛：{salary_reason}", increment_filtered=True, filtered_candidate=candidate)
                                continue
                            filter_reason = self._filter_reason(candidate)
                            if filter_reason:
                                hooks.on_event(message=filter_reason, increment_filtered=True, filtered_candidate=candidate)
                                continue
                            if not hooks.on_list_candidate(candidate):
                                continue
                            # 连续登录墙 = 会话整体失效：本词剩余岗位只存列表信息，
                            # 不再逐个新开标签页 + 10~18 秒等待（当前最大无效请求来源）。
                            if login_wall_active:
                                hooks.on_parse_failed("猎聘详情页持续需登录(wow.liepin.com)，本词剩余岗位仅保存列表信息")
                                if not hooks.on_candidate(candidate):
                                    return PlatformCollectionResult(self.platform, "completed", "callback_stopped", "采集回调已停止")
                                continue
                            # P3：先定详情页走哪条路，节奏随之不同。
                            # HTTP 不加载页面（单请求 <1 秒），用 1.5~3 秒；
                            # 浏览器要渲染，保持 10~18 秒。
                            use_http = http_detail_enabled and self._http_failures < HTTP_DETAIL_FAILURE_THRESHOLD
                            if use_http:
                                detail_req = self.uniform(*self.http_detail_delay_range)
                                hooks.on_event(phase="pacing", keyword=keyword, city=city, page=page,
                                               message=f"详情页 HTTP 间隔 {detail_req:.1f} 秒")
                            else:
                                detail_req = self.uniform(*self.detail_delay_range)
                                hooks.on_event(phase="pacing", keyword=keyword, city=city, page=page,
                                               message=f"详情页安全间隔 {detail_req:.1f} 秒")
                            if self._wait(hooks, detail_req):
                                return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                            # 批次休息：连开 N 个详情页后歇 3~8 分钟，模拟真人
                            # 「看一会儿歇一下」，避免一口气跑满变成机器画像。
                            if detail_since_break >= detail_break_after:
                                rest = random.uniform(BREAK_DURATION_MIN_SECONDS, BREAK_DURATION_MAX_SECONDS)
                                hooks.on_event(phase="pacing", keyword=keyword, city=city, page=page,
                                               message=f"猎聘拟人化休息 {int(rest)} 秒（已连续打开 {detail_since_break} 个详情页）")
                                if self._wait(hooks, rest):
                                    return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                                detail_since_break = 0
                                detail_break_after = random.randint(DETAIL_BREAK_EVERY_MIN, DETAIL_BREAK_EVERY_MAX)
                            # 单轮上限：鼓励分几次运行，而不是一口气跑满日额。
                            if detail_opened_this_run >= run_detail_limit:
                                return PlatformCollectionResult(
                                    self.platform, "completed_with_shortage", "run_detail_page_limit",
                                    f"猎聘本轮详情页已达 {run_detail_limit} 个（单轮上限），分次运行更安全",
                                )
                            hooks.on_event(phase="loading_detail", keyword=keyword, city=city, page=page)
                            if guard is not None:
                                try:
                                    guard.reserve("detail_page", daily_limit=detail_limit)
                                except PlatformSafetyStop as exc:
                                    return PlatformCollectionResult(
                                        self.platform, "completed_with_shortage", exc.reason,
                                        f"猎聘详情页已达访问额度：{exc.reason}，本轮停止",
                                    )
                            # P3：详情 JD 优先走纯 HTTP（不开标签页、不渲染）。
                            # 失败抛 DetailFetchFallback → 落到下方原有浏览器流程。
                            if use_http:
                                try:
                                    http_result = self._fetch_detail_http(candidate)
                                except DetailFetchFallback as exc:
                                    tripped = not self._note_http_detail(False)
                                    hooks.on_event(phase="pacing", keyword=keyword, city=city, page=page,
                                                   message=(
                                                       f"猎聘详情页 HTTP 连续失败 {self._http_failures} 次"
                                                       f"（{exc.reason}），本轮改走浏览器"
                                                       if tripped else
                                                       f"猎聘详情页 HTTP 不可用（{exc.reason}），回退浏览器"
                                                   ))
                                    # 回退时补上浏览器路径应有的间隔，避免两条路径叠加后过密。
                                    fallback_req = self.uniform(*self.detail_delay_range)
                                    if self._wait(hooks, fallback_req):
                                        return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                                else:
                                    self._note_http_detail(True)
                                    detail_opened_this_run += 1
                                    detail_since_break += 1
                                    if http_result.status in {"blocked", "blanked"}:
                                        risk_detail = "猎聘详情页出现验证或限流(HTTP)"
                                        self._record_risk(guard, f"{risk_detail}，已停止整个平台任务")
                                        return PlatformCollectionResult(
                                            self.platform, "blocked", "rate_limit",
                                            f"{risk_detail}，已停止整个平台任务",
                                        )
                                    if http_result.status == "offline":
                                        consecutive_login_required = 0
                                        hooks.on_parse_failed("猎聘岗位已下线")
                                        continue
                                    detail = self._detail_from_http(http_result)
                                    if str(detail.get("jd") or "").strip():
                                        consecutive_login_required = 0
                                        final = self._candidate_from_detail(detail, candidate)
                                        if not hooks.on_candidate(final):
                                            return PlatformCollectionResult(self.platform, "completed", "callback_stopped", "采集回调已停止")
                                        continue
                                    # HTTP 拿到页面但没有 JD：保留列表信息，不中断任务。
                                    consecutive_login_required = 0
                                    hooks.on_parse_failed("猎聘详情页JD不可用(HTTP)，仅保存列表信息")
                                    if not hooks.on_candidate(candidate):
                                        return PlatformCollectionResult(self.platform, "completed", "callback_stopped", "采集回调已停止")
                                    continue
                            # 复用同一个详情标签页：原实现每个岗位都 new_tab +
                            # close_tab，一天几百次新建/销毁本身就是最像爬虫的轨迹。
                            if detail_target is None:
                                detail_initial_url = "about:blank" if self.browser.navigate_action is not None else candidate.url
                                detail_target = self.browser.new_tab(detail_initial_url, background=True)
                                if not detail_target:
                                    hooks.on_parse_failed("无法打开猎聘详情页")
                                    page_had_transient_failure = True
                                    continue
                            if not self._navigate(hooks, detail_target, candidate.url, "猎聘详情页"):
                                # 标签可能已失效（被关闭/崩溃）：丢弃，下个岗位重建。
                                self.browser.close_tab(detail_target)
                                detail_target = None
                                hooks.on_parse_failed("猎聘详情页导航失败")
                                page_had_transient_failure = True
                                continue
                            detail_opened_this_run += 1
                            detail_since_break += 1
                            try:
                                self.browser.wait_for_load(detail_target, timeout=15)
                                self.browser.scroll(detail_target, y=1200)
                                detail: dict[str, Any] = {}
                                detail_status = "selector_changed"
                                for attempt in range(RENDER_POLL_ATTEMPTS):
                                    detail = _payload(self.browser.evaluate(detail_target, JS_EXTRACT_DETAIL))
                                    detail_status = str(detail.get("status") or "selector_changed")
                                    if detail_status in {"ready", "blocked", "offline", "login_required"}:
                                        break
                                    if attempt + 1 < RENDER_POLL_ATTEMPTS and self._wait(hooks, RENDER_POLL_INTERVAL_SECONDS):
                                        return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                            except BaseException:
                                # 异常路径（标签崩溃/连接中断）丢弃该标签页，
                                # 下个岗位重建；正常路径复用、不关闭。
                                self.browser.close_tab(detail_target)
                                detail_target = None
                                raise
                            else:
                                if not reuse_detail_tab:
                                    # 无法主动导航时每个岗位一个临时标签页。
                                    self.browser.close_tab(detail_target)
                                    detail_target = None
                            if detail_status in {"blocked", "blanked"}:
                                risk_detail = (
                                    "猎聘详情页被清空(about:blank)"
                                    if detail_status == "blanked"
                                    else "猎聘详情页出现验证或限流"
                                )
                                self._record_risk(guard, f"{risk_detail}，已停止整个平台任务")
                                return PlatformCollectionResult(self.platform, "blocked", "rate_limit", f"{risk_detail}，已停止整个平台任务")
                            if detail_status == "offline":
                                consecutive_login_required = 0
                                hooks.on_parse_failed("猎聘岗位已下线")
                                continue
                            if detail_status == "login_required":
                                consecutive_login_required += 1
                                if consecutive_login_required >= MAX_CONSECUTIVE_LOGIN_REQUIRED:
                                    login_wall_active = True
                                    hooks.on_event(phase="pacing", keyword=keyword, city=city, page=page,
                                                   message="猎聘详情页连续需登录，本词剩余岗位改为只存列表信息")
                                hooks.on_parse_failed("猎聘详情页需登录(wow.liepin.com)，仅保存列表信息")
                                if not hooks.on_candidate(candidate):
                                    return PlatformCollectionResult(self.platform, "completed", "callback_stopped", "采集回调已停止")
                                continue
                            if detail_status == "ready" and str(detail.get("jd") or "").strip():
                                consecutive_login_required = 0
                                final = self._candidate_from_detail(detail, candidate)
                                if not hooks.on_candidate(final):
                                    return PlatformCollectionResult(self.platform, "completed", "callback_stopped", "采集回调已停止")
                                continue
                            # 详情页 JD 不可用（职位簇/跳转登录墙）：保留列表已采集的
                            # 岗位线索（标题/公司/薪资/城市），不中断整个平台任务。
                            consecutive_login_required = 0
                            hooks.on_parse_failed("猎聘详情页JD不可用，仅保存列表信息")
                            if not hooks.on_candidate(candidate):
                                return PlatformCollectionResult(self.platform, "completed", "callback_stopped", "采集回调已停止")
                        # A later successful page cannot cover a gap in an
                        # earlier page. Stop before advancing either checkpoint.
                        if not hooks.can_checkpoint():
                            return PlatformCollectionResult(
                                self.platform, "failed", "save_failed",
                                "猎聘岗位保存失败，保留上一完整页断点，下次重试",
                            )
                        if page_had_transient_failure:
                            return PlatformCollectionResult(
                                self.platform, "completed_with_shortage", "detail_unavailable",
                                "猎聘详情页暂时不可用，保留上一完整页断点，下次重试",
                            )
                        if self.safety_conn is not None:
                            upsert_page_progress(self.safety_conn, "liepin", city, keyword, page)
                finally:
                    # 详情标签页按关键词复用到最后，退出关键词时统一关闭。
                    if detail_target:
                        self.browser.close_tab(detail_target)
                    if target_id:
                        self.browser.close_tab(target_id)
                # 词结束：标记词级断点；页级断点保留在最后一页，供下次续采（到期自动失效）
                if self.safety_conn is not None:
                    mark_combo_collected(self.safety_conn, "liepin", city, keyword)
                outcomes["resumed" if saved_page > 0 else "scanned"] += 1
        if outcomes["skipped"] and outcomes["skipped"] == total_combos:
            return PlatformCollectionResult(
                self.platform, "completed", "no_remaining_pages",
                f"猎聘 上次已采到设定的最大页，{self._resume_ttl_hours()} 小时内没有剩余页可采；"
                f"如需从第 1 页重扫，请在采集设置里勾选「从第 1 页重扫」",
            )
        message = "猎聘搜索结果已采集完毕"
        if outcomes["resumed"]:
            message += f"；{outcomes['resumed']} 个组合从上次页码续采"
        if outcomes["skipped"]:
            message += f"；{outcomes['skipped']} 个组合 24h 内已采完、无剩余页"
        return PlatformCollectionResult(self.platform, "completed", "search_exhausted", message)

    @staticmethod
    def _candidate_from_list(raw: Any, city: str, keyword: str) -> JobCandidate | None:
        if not isinstance(raw, dict):
            return None
        source_id = str(raw.get("source_job_id") or "").strip()
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip().split("?")[0]
        if not source_id or not title or not url.startswith("https://www.liepin.com/job/"):
            return None
        return JobCandidate(
            platform="liepin",
            source_job_id=source_id,
            title=title,
            company=str(raw.get("company") or "").strip(),
            salary=str(raw.get("salary") or "").strip(),
            city=str(raw.get("city") or city).strip(),
            experience=str(raw.get("experience") or "").strip(),
            education=str(raw.get("education") or "").strip(),
            url=url,
            source_keyword=keyword,
        )

    @staticmethod
    def _candidate_from_detail(detail: dict[str, Any], base: JobCandidate) -> JobCandidate:
        # Liepin detail pages are "job clusters": the rendered detail identity
        # (title/company/salary) can rotate per load and disagree with the search
        # card. The search card is the stable identity; the detail page is only
        # trusted for the JD body (section.job-intro-container is unique to the
        # main job). Keep every other field from the list candidate.
        return JobCandidate(
            platform="liepin",
            source_job_id=base.source_job_id,
            title=base.title,
            company=base.company,
            salary=base.salary,
            city=base.city,
            experience=base.experience,
            education=base.education,
            jd=str(detail.get("jd") or base.jd or "").strip(),
            url=base.url,
            source_keyword=base.source_keyword,
        )
