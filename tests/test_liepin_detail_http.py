"""猎聘详情页 HTTP 抓取（P3 API 化）的回归测试。

所有用例都用注入的假 transport，**不访问网络**。真实页面结构由
2026-11-12 的抓包样本（tests/fixtures 内联的最小 HTML）覆盖。
"""

from __future__ import annotations

import json
from unittest import TestCase
from unittest.mock import patch

from bosshunter.collection.base import CollectorHooks
from bosshunter.collection.models import PlatformCollectionRequest
from bosshunter.collection.platforms.liepin import (
    HTTP_DETAIL_FAILURE_THRESHOLD,
    LiepinBrowser,
    LiepinCollector,
)
from bosshunter.collection.platforms.liepin_detail_http import (
    DetailFetchFallback,
    LiepinDetailFetcher,
    parse_detail_html,
)


# 依据 2026-11-12 抓包的真实结构裁剪：JD 锚点是 dd[data-selector=
# "job-intro-content"]，标题/薪资/城市在 .job-apply-content 卡片里，
# 公司名在 company 链接里。hashed class 一律不依赖。
DETAIL_HTML = """
<html><head><title>【南京 文员招聘】-某公司招聘信息-猎聘</title></head><body>
<section class="job-apply-container">
  <div class="job-apply-content">
    <div class="name-box">
      <span class="name ellipsis-2">文员</span>
      <span class="salary">5-6k</span>
    </div>
    <div class="job-properties">
      <span>南京-江宁区</span>
      <span class="split"></span>
      <span>经验不限</span>
    </div>
    <div class="company-info">
      <span><a href="https://www.liepin.com/company/21555095/" target="_blank"> · 南京抖金文化传媒有限公司</a></span>
    </div>
  </div>
</section>
<section class="job-intro-container">
  <dl class="paragraph">
    <dt>职位介绍</dt>
    <dd data-selector="job-intro-content">1、负责线上平台信息整理；<br/>2、对接咨询用户，解答基础业务问题；</dd>
  </dl>
</section>
</body></html>
"""

BLOCKED_HTML = """<html><head><title>安全验证</title></head><body>
<div>请完成安全验证</div></body></html>
"""

OFFLINE_HTML = """<html><head><title>猎聘</title></head><body>
<div>该职位已关闭</div></body></html>
"""

NO_JD_HTML = """<html><head><title>猎聘</title></head><body>
<div>投递了该职位的人还查看了以下职位</div></body></html>
"""


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, url: str = "") -> None:
        self.text = text
        self.status_code = status_code
        self.url = url


def _fetcher(html: str = DETAIL_HTML, *, status: int = 200, calls: list[str] | None = None):
    """构造注入了假 transport 的 fetcher。"""
    def fake_get(url: str) -> FakeResponse:
        if calls is not None:
            calls.append(url)
        return FakeResponse(html, status_code=status, url=url)

    return LiepinDetailFetcher(get=fake_get)


class ParseDetailHtmlTests(TestCase):
    def test_parses_jd_and_metadata(self):
        result = parse_detail_html(DETAIL_HTML, url="https://www.liepin.com/job/1.shtml")
        self.assertEqual(result.status, "ready")
        self.assertIn("负责线上平台信息整理", result.jd)
        self.assertIn("对接咨询用户", result.jd)
        self.assertEqual(result.title, "文员")
        self.assertEqual(result.salary, "5-6k")
        self.assertEqual(result.city, "南京-江宁区")
        self.assertEqual(result.company, "南京抖金文化传媒有限公司")

    def test_blocked_takes_precedence_over_structure(self):
        """验证页可能同时缺少 JD 锚点，必须先判风控，否则会被当成结构变化。"""
        self.assertEqual(parse_detail_html(BLOCKED_HTML).status, "blocked")

    def test_offline_page(self):
        self.assertEqual(parse_detail_html(OFFLINE_HTML).status, "offline")

    def test_missing_jd_reports_no_jd(self):
        self.assertEqual(parse_detail_html(NO_JD_HTML).status, "no_jd")

    def test_html_entities_are_unescaped(self):
        html = DETAIL_HTML.replace(
            "1、负责线上平台信息整理；", "1、负责 A&amp;B 业务；"
        )
        result = parse_detail_html(html)
        self.assertIn("A&B", result.jd)

    def test_script_content_does_not_trigger_blocked(self):
        """script 里出现同名 token 不应被当成风控文案。"""
        html = DETAIL_HTML.replace(
            "<body>", "<body><script>var t='安全验证';</script>"
        )
        self.assertEqual(parse_detail_html(html).status, "ready")


class LiepinDetailFetcherTests(TestCase):
    def test_fetch_returns_ready_for_normal_page(self):
        calls: list[str] = []
        result = _fetcher(calls=calls).fetch("1986629723")
        self.assertEqual(result.status, "ready")
        self.assertIn("负责线上平台信息整理", result.jd)
        self.assertEqual(calls, ["https://www.liepin.com/job/1986629723.shtml"])

    def test_fetch_uses_given_url(self):
        calls: list[str] = []
        _fetcher(calls=calls).fetch("1", "https://www.liepin.com/job/999.shtml")
        self.assertEqual(calls, ["https://www.liepin.com/job/999.shtml"])

    def test_blocked_is_returned_not_raised(self):
        """风控要上报，不能静默回退浏览器。"""
        result = _fetcher(BLOCKED_HTML).fetch("1")
        self.assertEqual(result.status, "blocked")

    def test_offline_is_returned(self):
        self.assertEqual(_fetcher(OFFLINE_HTML).fetch("1").status, "offline")

    def test_missing_jd_falls_back_to_browser(self):
        with self.assertRaises(DetailFetchFallback) as ctx:
            _fetcher(NO_JD_HTML).fetch("1")
        self.assertEqual(ctx.exception.reason, "no_jd_in_html")

    def test_non_200_falls_back(self):
        with self.assertRaises(DetailFetchFallback) as ctx:
            _fetcher(DETAIL_HTML, status=403).fetch("1")
        self.assertEqual(ctx.exception.reason, "http_status")

    def test_network_error_falls_back(self):
        def boom(_url):
            raise OSError("connection reset")

        with self.assertRaises(DetailFetchFallback) as ctx:
            LiepinDetailFetcher(get=boom).fetch("1")
        self.assertEqual(ctx.exception.reason, "network_error")

    def test_empty_body_falls_back(self):
        with self.assertRaises(DetailFetchFallback) as ctx:
            _fetcher("   ").fetch("1")
        self.assertEqual(ctx.exception.reason, "empty_body")

    def test_missing_job_id_falls_back(self):
        with self.assertRaises(DetailFetchFallback) as ctx:
            _fetcher().fetch("")
        self.assertEqual(ctx.exception.reason, "missing_job_id")


def _list_payload(jobs: list[dict]) -> str:
    return json.dumps({"status": "ready", "jobs": jobs}, ensure_ascii=False)


def _job(n: int) -> dict:
    return {
        "source_job_id": f"job-{n}",
        "title": f"文员{n}",
        "company": "示例公司",
        "salary": "5-6k",
        "city": "南京-江宁区",
        "url": f"https://www.liepin.com/job/{n}.shtml",
    }


def _browser(list_payload: str, detail_payload: str | None = None):
    """浏览器桩：列表返回给定 payload，详情返回 detail_payload（用于验证回退）。"""
    detail_calls: list[str] = []

    def evaluate(target, script):
        if "job-detail-job-info" in script:
            return list_payload
        detail_calls.append(target)
        return detail_payload or json.dumps({"status": "ready", "jd": "浏览器抓到的 JD"})

    surface = LiepinBrowser(
        new_tab=lambda url, **_kwargs: url,
        close_tab=lambda _target: True,
        evaluate=evaluate,
        scroll=lambda *_args, **_kwargs: True,
        wait_for_load=lambda *_args, **_kwargs: True,
    )
    return surface, detail_calls


class LiepinHttpDetailCollectorTests(TestCase):
    """HTTP 详情与浏览器回退的集成行为。"""

    def setUp(self):
        self._patches = [
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _hooks(collected: list, parse_failures: list | None = None) -> CollectorHooks:
        return CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _c: True,
            on_candidate=lambda c: collected.append(c) or True,
            on_parse_failed=lambda r: (parse_failures.append(r) if parse_failures is not None else None),
            on_event=lambda **_kwargs: None,
        )

    @staticmethod
    def _request() -> PlatformCollectionRequest:
        return PlatformCollectionRequest("liepin", ["文员"], ["南京"], {"南京": "060020"}, max_pages=1)

    def _collect(self, *, fetcher, browser, collected, parse_failures=None):
        return LiepinCollector(
            browser=browser,
            detail_fetcher=fetcher,
            sleep=lambda _s: None,
            uniform=lambda _a, _b: 0.0,
        ).collect(self._request(), self._hooks(collected, parse_failures))

    def test_http_detail_supplies_jd_without_opening_browser_detail(self):
        """HTTP 成功时不应再走浏览器详情（不开标签、不 evaluate 详情页）。"""
        browser, detail_calls = _browser(_list_payload([_job(1)]))
        collected: list = []
        result = self._collect(fetcher=_fetcher(), browser=browser, collected=collected)

        self.assertEqual(result.reason_code, "search_exhausted")
        self.assertEqual(len(collected), 1)
        self.assertIn("负责线上平台信息整理", collected[0].jd)
        self.assertEqual(detail_calls, [], "HTTP 成功却仍打开了浏览器详情页")

    def test_http_failure_falls_back_to_browser(self):
        """HTTP 不可用时回退浏览器，岗位仍然入库。"""
        browser, detail_calls = _browser(_list_payload([_job(1)]))
        collected: list = []
        failing = LiepinDetailFetcher(get=lambda _u: FakeResponse(NO_JD_HTML))
        result = self._collect(fetcher=failing, browser=browser, collected=collected)

        self.assertEqual(result.reason_code, "search_exhausted")
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].jd, "浏览器抓到的 JD")
        self.assertEqual(len(detail_calls), 1, "回退时没有打开浏览器详情页")

    def test_http_blocked_skips_liepin(self):
        """HTTP 看到验证页必须记录风险并只跳过猎聘，不能回退静默继续。"""
        browser, detail_calls = _browser(_list_payload([_job(1)]))
        collected: list = []
        blocked = LiepinDetailFetcher(get=lambda _u: FakeResponse(BLOCKED_HTML))
        result = self._collect(fetcher=blocked, browser=browser, collected=collected)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason_code, "liepin_risk_skipped")
        self.assertEqual(detail_calls, [], "blocked 后仍继续打开浏览器详情页")

    def test_http_offline_skips_job_without_browser(self):
        browser, detail_calls = _browser(_list_payload([_job(1), _job(2)]))
        collected: list = []
        offline = LiepinDetailFetcher(get=lambda _u: FakeResponse(OFFLINE_HTML))
        result = self._collect(fetcher=offline, browser=browser, collected=collected)

        self.assertEqual(result.reason_code, "search_exhausted")
        self.assertEqual(collected, [], "已下线岗位不应入库")
        self.assertEqual(detail_calls, [], "已下线岗位不应再开浏览器详情页")

    def test_consecutive_http_failures_trip_breaker(self):
        """连续失败达阈值后不再尝试 HTTP，避免回退让请求量翻倍。"""
        jobs = [_job(i) for i in range(1, 6)]
        browser, detail_calls = _browser(_list_payload(jobs))
        collected: list = []
        http_calls: list[str] = []

        def failing_get(url: str) -> FakeResponse:
            http_calls.append(url)
            return FakeResponse(NO_JD_HTML)

        fetcher = LiepinDetailFetcher(get=failing_get)
        self._collect(fetcher=fetcher, browser=browser, collected=collected)

        self.assertEqual(len(collected), 5)
        self.assertEqual(
            len(http_calls), HTTP_DETAIL_FAILURE_THRESHOLD,
            f"熔断后仍在尝试 HTTP：{len(http_calls)} 次",
        )
        # 熔断后剩余岗位直接走浏览器
        self.assertEqual(len(detail_calls), 5)

    def test_http_success_resets_failure_counter(self):
        """成功一次后计数归零，偶发抖动不会累积到熔断。"""
        jobs = [_job(i) for i in range(1, 6)]
        browser, _detail_calls = _browser(_list_payload(jobs))
        collected: list = []
        sequence = [NO_JD_HTML, NO_JD_HTML, DETAIL_HTML, NO_JD_HTML, NO_JD_HTML]
        calls = {"n": 0}

        def alternating_get(_url: str) -> FakeResponse:
            html = sequence[min(calls["n"], len(sequence) - 1)]
            calls["n"] += 1
            return FakeResponse(html)

        fetcher = LiepinDetailFetcher(get=alternating_get)
        result = self._collect(fetcher=fetcher, browser=browser, collected=collected)

        self.assertEqual(result.reason_code, "search_exhausted")
        # 第 3 个成功 → 计数归零，第 4、5 个仍会尝试 HTTP
        self.assertEqual(calls["n"], 5)
        self.assertEqual(len(collected), 5)

    def test_http_detail_disabled_uses_browser_only(self):
        """配置关掉 HTTP 时完全回到浏览器路径。"""
        browser, detail_calls = _browser(_list_payload([_job(1)]))
        collected: list = []
        http_calls: list[str] = []

        def spy_get(url: str) -> FakeResponse:
            http_calls.append(url)
            return FakeResponse(DETAIL_HTML)

        config = {"platforms": {"liepin": {"search": {"http_detail_enabled": False}}}}
        result = LiepinCollector(
            config=config,
            browser=browser,
            detail_fetcher=LiepinDetailFetcher(get=spy_get),
            sleep=lambda _s: None,
            uniform=lambda _a, _b: 0.0,
        ).collect(self._request(), self._hooks(collected))

        self.assertEqual(result.reason_code, "search_exhausted")
        self.assertEqual(http_calls, [], "配置关闭后仍发起了 HTTP 请求")
        self.assertEqual(len(detail_calls), 1)
        self.assertEqual(collected[0].jd, "浏览器抓到的 JD")
