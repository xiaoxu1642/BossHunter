import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from bosshunter.collection.base import CollectorHooks
from bosshunter.collection.models import PlatformCollectionRequest
from bosshunter.collection.orchestrator import normalize_collection_options
from bosshunter.collection.platforms.liepin import (
    JS_EXTRACT_DETAIL,
    JS_EXTRACT_LIST,
    LiepinBrowser,
    LiepinCollector,
    get_liepin_city_code,
)
from bosshunter.collection.text import clean_job_description


class LiepinTestBase(TestCase):
    """统一关闭 HTTP 详情路径。

    本文件的用例都验证浏览器流程；HTTP 详情若不关闭，会向 liepin.com
    发真实请求，让测试依赖网络且结果不确定。HTTP 路径由
    tests/test_liepin_detail_http.py 用注入的假 transport 覆盖。
    """

    def setUp(self):
        self._patches = [
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.LiepinCollector._http_detail_enabled",
                  return_value=False),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


class LiepinCollectorTests(LiepinTestBase):
    def test_list_script_uses_stable_attribute_selector(self):
        self.assertIn('a[data-nick="job-detail-job-info"]', JS_EXTRACT_LIST)
        self.assertIn("liepin", JS_EXTRACT_LIST)
        self.assertIn("job", JS_EXTRACT_LIST)

    def test_detail_script_targets_job_description(self):
        self.assertIn("job-intro-container", JS_EXTRACT_DETAIL)
        self.assertIn("job-title-box", JS_EXTRACT_DETAIL)

    def test_city_and_option_defaults_are_fail_closed(self):
        self.assertEqual(get_liepin_city_code("北京市"), "010")
        self.assertEqual(get_liepin_city_code("上海市"), "020")
        self.assertEqual(get_liepin_city_code("广州市"), "050020")
        self.assertIsNone(get_liepin_city_code("未知城市"))
        options = normalize_collection_options({}, {
            "platform_order": ["liepin"],
            "platforms": {"liepin": {"keywords": ["AI 产品"], "cities": ["上海"]}},
        })
        search = options["platforms"]["liepin"]
        self.assertEqual(search["city_codes"], {"上海": "020"})
        self.assertEqual(search["max_pages"], 1)
        self.assertNotIn("target_count", search)

    def test_unknown_city_cannot_bypass_snapshot_with_external_code(self):
        with self.assertRaises(ValueError):
            normalize_collection_options({}, {
                "platform_order": ["liepin"],
                "platforms": {"liepin": {
                    "keywords": ["AI 产品"],
                    "cities": ["未知城市"],
                    "city_codes": {"未知城市": "999"},
                }},
            })

    def test_beijing_search_uses_verified_liepin_city_code(self):
        request = PlatformCollectionRequest(
            "liepin",
            ["AI 产品"],
            ["北京"],
            {"北京": "010"},
            max_pages=1,
        )

        url = LiepinCollector.build_search_url(request, "北京", "AI 产品")

        self.assertIn("dqs=010", url)
        self.assertIn("key=AI%20%E4%BA%A7%E5%93%81", url)

    def test_collection_uses_platform_identity_and_rate_limit(self):
        list_payload = json.dumps({"status": "ready", "jobs": [
            {
                "source_job_id": "job-1",
                "salary": "15-25K",
                "title": "AI 产品经理",
                "company": "示例公司",
                "city": "上海",
                "url": "https://www.liepin.com/job/1001.shtml",
            },
            {
                "source_job_id": "job-2",
                "salary": "15-25K",
                "title": "AI 产品运营",
                "company": "示例公司",
                "city": "上海",
                "url": "https://www.liepin.com/job/1002.shtml",
            },
        ]}, ensure_ascii=False)
        detail_payload = json.dumps({
            "status": "ready",
            "title": "AI 产品",
            "company": "示例公司",
            "city": "上海",
            "jd": "负责需求分析，来自BOSS直聘要求会SQL。",
        }, ensure_ascii=False)
        sleeps: list[float] = []

        def evaluate(_target, script):
            return list_payload if "job-detail-job-info" in script else detail_payload

        browser = LiepinBrowser(
            new_tab=lambda url, **_kwargs: url,
            close_tab=lambda _target: True,
            evaluate=evaluate,
            scroll=lambda *_args, **_kwargs: True,
            wait_for_load=lambda *_args, **_kwargs: True,
        )
        collected = []
        hooks = CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _candidate: True,
            on_candidate=lambda candidate: collected.append(candidate) or len(collected) < 2,
            on_parse_failed=lambda reason: self.fail(reason),
            on_event=lambda **_kwargs: None,
        )
        result = LiepinCollector(
            browser=browser,
            sleep=sleeps.append,
            uniform=lambda _low, _high: 13.0,
        ).collect(
            PlatformCollectionRequest("liepin", ["AI 产品"], ["上海"], {"上海": "020"}, max_pages=1),
            hooks,
        )

        self.assertEqual(result.reason_code, "callback_stopped")
        self.assertEqual([candidate.storage_id for candidate in collected], ["liepin:job-1", "liepin:job-2"])
        # 列表页现在还有 settle + 1~2 次滚动停顿，只断言末尾两个详情页间隔。
        self.assertEqual(sleeps[-2:], [13.0, 13.0])
        self.assertIn("需求分析", collected[0].jd)

    def test_verification_page_stops_platform(self):
        browser = LiepinBrowser(
            new_tab=lambda url, **_kwargs: url,
            close_tab=lambda _target: True,
            evaluate=lambda _target, _script: json.dumps({"status": "blocked", "jobs": []}),
            scroll=lambda *_args, **_kwargs: True,
            wait_for_load=lambda *_args, **_kwargs: True,
        )
        hooks = CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _candidate: True,
            on_candidate=lambda _candidate: True,
            on_parse_failed=lambda _reason: None,
            on_event=lambda **_kwargs: None,
        )
        result = LiepinCollector(browser=browser).collect(
            PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
            hooks,
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason_code, "rate_limit")

    def test_collection_waits_for_spa_list_render(self):
        evaluations = 0
        sleeps = []

        def evaluate(_target, script):
            nonlocal evaluations
            if "job-detail-job-info" in script:
                evaluations += 1
                if evaluations < 3:
                    return json.dumps({"status": "waiting", "jobs": []})
                return json.dumps({"status": "ready", "jobs": [{
                    "source_job_id": "job-spa",
                    "salary": "15-25K",
                    "title": "AI 运营",
                    "company": "示例公司",
                    "city": "上海",
                    "url": "https://www.liepin.com/job/2001.shtml",
                }]})
            return json.dumps({
                "status": "ready", "title": "AI 运营", "company": "示例公司",
                "city": "上海", "jd": "负责 AI 产品运营与数据分析。",
            })

        browser = LiepinBrowser(
            new_tab=lambda url, **_kwargs: url,
            close_tab=lambda _target: True,
            evaluate=evaluate,
            scroll=lambda *_args, **_kwargs: True,
            wait_for_load=lambda *_args, **_kwargs: True,
        )
        hooks = CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _candidate: True,
            on_candidate=lambda _candidate: False,
            on_parse_failed=lambda reason: self.fail(reason),
            on_event=lambda **_kwargs: None,
        )

        result = LiepinCollector(browser=browser, sleep=sleeps.append).collect(
            PlatformCollectionRequest("liepin", ["AI运营"], ["上海"], {"上海": "020"}, max_pages=1),
            hooks,
        )

        self.assertEqual(result.reason_code, "callback_stopped")
        self.assertEqual(evaluations, 3)
        # 列表页新增 settle/滚动停顿，只断言两次渲染轮询间隔。
        self.assertEqual(sleeps.count(0.75), 2)

    def test_multi_page_collection_uses_url_pagination_and_pacing(self):
        list_calls = {"n": 0}
        navigation_urls = []
        sleeps = []

        def evaluate(_target, script):
            if "job-detail-job-info" in script:
                list_calls["n"] += 1
                if list_calls["n"] == 1:
                    return json.dumps({"status": "ready", "jobs": [{
                        "source_job_id": "job-p1",
                        "salary": "15-25K",
                        "title": "AI 资深工程师",
                        "company": "示例公司",
                        "city": "上海",
                        "url": "https://www.liepin.com/job/3001.shtml",
                    }]})
                return json.dumps({"status": "ready", "jobs": [{
                    "source_job_id": "job-p2",
                    "salary": "15-25K",
                    "title": "AI 数据工程师",
                    "company": "示例公司",
                    "city": "上海",
                    "url": "https://www.liepin.com/job/3002.shtml",
                }]})
            return json.dumps({
                "status": "ready", "title": "AI 工程师", "company": "示例公司",
                "city": "上海", "jd": "负责 AI 平台研发。",
            })

        browser = LiepinBrowser(
            new_tab=lambda url, **_kwargs: url,
            close_tab=lambda _target: True,
            evaluate=evaluate,
            scroll=lambda *_args, **_kwargs: True,
            wait_for_load=lambda *_args, **_kwargs: True,
            navigate_action=lambda _target, url: navigation_urls.append(url) or True,
        )
        collected = []
        hooks = CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _candidate: True,
            on_candidate=lambda candidate: collected.append(candidate) or True,
            on_parse_failed=lambda _reason: None,
            on_event=lambda **_kwargs: None,
        )
        result = LiepinCollector(
            browser=browser,
            sleep=sleeps.append,
            uniform=lambda _low, _high: 33.0,
        ).collect(
            PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=2),
            hooks,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reason_code, "search_exhausted")
        self.assertEqual(
            [c.storage_id for c in collected],
            ["liepin:job-p1", "liepin:job-p2"],
        )
        self.assertIn("dqs=020", navigation_urls[0])
        self.assertTrue(any("curPage=1" in url for url in navigation_urls))
        # 每页列表都有 settle + 1~2 次滚动停顿，不再断言精确序列；
        # 验证所有停顿都走了注入的节流函数，且至少覆盖翻页与两个详情页。
        self.assertEqual(set(sleeps), {33.0})
        self.assertGreaterEqual(len(sleeps), 3)

    def test_empty_next_page_completes_gracefully(self):
        list_calls = {"n": 0}

        def evaluate(_target, script):
            if "job-detail-job-info" in script:
                list_calls["n"] += 1
                if list_calls["n"] > 1:
                    return json.dumps({"status": "empty", "jobs": []})
                return json.dumps({"status": "ready", "jobs": [{
                    "source_job_id": f"job-{list_calls['n']}",
                    "salary": "15-25K",
                    "title": "AI 工程师",
                    "company": "示例公司",
                    "city": "上海",
                    "url": f"https://www.liepin.com/job/{4000 + list_calls['n']}.shtml",
                }]})
            return json.dumps({
                "status": "ready", "title": "AI 工程师", "company": "示例公司",
                "city": "上海", "jd": "负责 AI 研发。",
            })

        browser = LiepinBrowser(
            new_tab=lambda url, **_kwargs: url,
            close_tab=lambda _target: True,
            evaluate=evaluate,
            scroll=lambda *_args, **_kwargs: True,
            wait_for_load=lambda *_args, **_kwargs: True,
        )
        collected = []
        hooks = CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _candidate: True,
            on_candidate=lambda candidate: collected.append(candidate) or True,
            on_parse_failed=lambda _reason: None,
            on_event=lambda **_kwargs: None,
        )
        result = LiepinCollector(
            browser=browser,
            sleep=lambda _s: None,
            uniform=lambda _low, _high: 33.0,
        ).collect(
            PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=3),
            hooks,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reason_code, "search_exhausted")
        self.assertEqual(len(collected), 1)
        self.assertEqual(list_calls["n"], 2)

    def test_login_required_detail_keeps_list_info(self):
        list_payload = json.dumps({"status": "ready", "jobs": [{
            "source_job_id": "job-login",
            "salary": "15-25K",
            "title": "AI 架构师",
            "company": "示例公司",
            "city": "上海",
            "url": "https://www.liepin.com/job/5001.shtml",
        }]}, ensure_ascii=False)

        detail_evaluations = 0
        sleeps = []

        def evaluate(_target, script):
            nonlocal detail_evaluations
            if "job-detail-job-info" in script:
                return list_payload
            detail_evaluations += 1
            return json.dumps({"status": "login_required"})

        browser = LiepinBrowser(
            new_tab=lambda url, **_kwargs: url,
            close_tab=lambda _target: True,
            evaluate=evaluate,
            scroll=lambda *_args, **_kwargs: True,
            wait_for_load=lambda *_args, **_kwargs: True,
        )
        collected = []
        parse_failures = []
        hooks = CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _candidate: True,
            on_candidate=lambda candidate: collected.append(candidate) or True,
            on_parse_failed=lambda reason: parse_failures.append(reason),
            on_event=lambda **_kwargs: None,
        )
        result = LiepinCollector(
            browser=browser,
            sleep=sleeps.append,
            uniform=lambda _low, _high: 13.0,
        ).collect(
            PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
            hooks,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].title, "AI 架构师")
        self.assertEqual(detail_evaluations, 1)
        self.assertEqual(sleeps[-1], 13.0)
        self.assertTrue(any("登录" in f or "wow.liepin" in f for f in parse_failures))

    def test_offline_detail_skips_gracefully(self):
        list_payload = json.dumps({"status": "ready", "jobs": [{
            "source_job_id": "job-offline",
            "title": "AI 研究员",
            "company": "示例公司",
            "city": "上海",
            "url": "https://www.liepin.com/job/6001.shtml",
        }]}, ensure_ascii=False)

        def evaluate(_target, script):
            if "job-detail-job-info" in script:
                return list_payload
            return json.dumps({"status": "offline"})

        browser = LiepinBrowser(
            new_tab=lambda url, **_kwargs: url,
            close_tab=lambda _target: True,
            evaluate=evaluate,
            scroll=lambda *_args, **_kwargs: True,
            wait_for_load=lambda *_args, **_kwargs: True,
        )
        collected = []
        hooks = CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _candidate: True,
            on_candidate=lambda candidate: collected.append(candidate) or True,
            on_parse_failed=lambda _reason: None,
            on_event=lambda **_kwargs: None,
        )
        result = LiepinCollector(
            browser=browser,
            sleep=lambda _s: None,
            uniform=lambda _low, _high: 13.0,
        ).collect(
            PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
            hooks,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(collected), 0)


class LiepinEnhancedTests(LiepinTestBase):
    """猎聘采集器增强：时间窗口 / 过滤链 / config 集成。"""

    def _hooks(self, collected=None):
        collected = collected if collected is not None else []
        return CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _c: True,
            on_candidate=lambda c: collected.append(c) or True,
            on_parse_failed=lambda _r: None,
            on_event=lambda **_: None,
        )

    def _browser_with_list(self, jobs):
        list_payload = json.dumps({"status": "ready", "jobs": jobs}, ensure_ascii=False)
        detail_payload = json.dumps({"status": "login_required"})
        return LiepinBrowser(
            new_tab=lambda url, **_kw: url,
            close_tab=lambda _t: True,
            evaluate=lambda _t, s: list_payload if "job-detail-job-info" in s else detail_payload,
            scroll=lambda *_a, **_kw: True,
            wait_for_load=lambda *_a, **_kw: True,
        )

    def test_outside_send_window_skips_collection(self):
        browser = self._browser_with_list([])
        collector = LiepinCollector(browser=browser)
        with patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=False):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(),
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reason_code, "outside_window")

    def test_collection_window_is_independent_of_send_window(self):
        collector = LiepinCollector(config={
            "throttle": {"send_windows": ["08:30-12:30", "13:30-18:30"]},
            "collection": {"collect_windows": ["06:00-11:30", "13:30-22:30"]},
        }, browser=self._browser_with_list([]))
        with patch("bosshunter.collection.platforms.liepin.SendWindowChecker") as checker:
            checker.return_value.is_active.return_value = False
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(),
            )
        checker.assert_called_once_with(["06:00-11:30", "13:30-22:30"])
        self.assertEqual(result.reason_code, "outside_window")
        self.assertIn("06:00-11:30", result.message)

    def test_explicitly_empty_collection_windows_keep_legacy_behavior(self):
        collector = LiepinCollector(config={
            "throttle": {"send_windows": ["08:30-12:30"]},
            "collection": {"collect_windows": []},
        }, browser=self._browser_with_list([]))
        with patch("bosshunter.collection.platforms.liepin.SendWindowChecker") as checker, \
                patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=True):
            checker.return_value.is_active.return_value = True
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(),
            )
        checker.assert_called_once_with([])
        self.assertEqual(result.reason_code, "day_off")

    def test_day_off_skips_collection(self):
        browser = self._browser_with_list([])
        collector = LiepinCollector(browser=browser)
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=True),
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(),
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reason_code, "day_off")

    def test_deal_breaker_in_title_filtered(self):
        jobs = [{"source_job_id": "1", "title": "外包AI", "company": "公司", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_list(jobs)
        collected = []
        collector = LiepinCollector(
            browser=browser, sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
            config={"profile": {"deal_breakers": ["外包"]}},
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(collected),
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(collected), 0)

    def test_blocked_company_filtered(self):
        jobs = [{"source_job_id": "1", "title": "AI工程师", "company": "黑名单公司", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_list(jobs)
        collected = []
        collector = LiepinCollector(
            browser=browser, sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
            config={"profile": {"blocked_companies": ["黑名单公司"]}},
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
        ):
            collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(collected),
            )
        self.assertEqual(len(collected), 0)

    def test_internship_filtered_when_disallowed(self):
        jobs = [{"source_job_id": "1", "title": "AI实习工程师", "company": "公司", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_list(jobs)
        collected = []
        collector = LiepinCollector(
            browser=browser, sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
            config={"profile": {"allow_internship": False}},
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
        ):
            collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(collected),
            )
        self.assertEqual(len(collected), 0)

    def test_internship_allowed_when_explicitly_enabled(self):
        jobs = [{"source_job_id": "1", "title": "AI实习工程师", "company": "公司", "city": "上海",
                 "salary": "15-25K", "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_list(jobs)
        collected = []
        collector = LiepinCollector(
            browser=browser, sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
            config={"profile": {"allow_internship": True}},
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
        ):
            collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(collected),
            )
        self.assertEqual(len(collected), 1)

    def test_no_filter_config_passes_all(self):
        jobs = [{"source_job_id": "1", "title": "AI工程师", "company": "公司", "city": "上海",
                 "salary": "15-25K", "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_list(jobs)
        collected = []
        collector = LiepinCollector(
            browser=browser, sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
        ):
            collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(collected),
            )
        self.assertEqual(len(collected), 1)

    def test_salary_below_min_filtered_with_event(self):
        jobs = [{"source_job_id": "1", "title": "AI工程师", "company": "公司", "city": "上海",
                 "salary": "5-8K", "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_list(jobs)
        collected = []
        events = []
        hooks = CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _c: True,
            on_candidate=lambda c: collected.append(c) or True,
            on_parse_failed=lambda _r: None,
            on_event=lambda **kw: events.append(kw),
        )
        collector = LiepinCollector(
            browser=browser, sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
            config={"profile": {"salary_min": 10, "salary_max": 20}},
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
        ):
            collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                hooks,
            )
        self.assertEqual(len(collected), 0)
        filtered_events = [e for e in events if e.get("increment_filtered")]
        self.assertEqual(len(filtered_events), 1)
        self.assertIn("薪资低于硬性要求", filtered_events[0].get("message", ""))

    def test_salary_within_range_collected(self):
        jobs = [{"source_job_id": "1", "title": "AI工程师", "company": "公司", "city": "上海",
                 "salary": "1-2万", "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_list(jobs)
        collected = []
        collector = LiepinCollector(
            browser=browser, sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
            config={"profile": {"salary_min": 10, "salary_max": 20}},
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
        ):
            collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(collected),
            )
        self.assertEqual(len(collected), 1)


class LiepinResumeCheckpointTests(LiepinTestBase):
    """猎聘断点续采：词级跳过 / 页级恢复 / checkpoint 记录 / 完成标记。"""

    def setUp(self):
        super().setUp()
        # 这一组只验证断点逻辑；页面访问守卫需要真实 sqlite 连接，
        # 这里统一关掉，避免安全网升级后占位 conn 被当成真连接调用。
        self._guard_patch = patch.object(LiepinCollector, "_guard", return_value=None)
        self._guard_patch.start()
        self.addCleanup(self._guard_patch.stop)

    def _hooks(self, collected=None, events=None):
        collected = collected if collected is not None else []
        events = events if events is not None else []
        return CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _c: True,
            on_candidate=lambda c: collected.append(c) or True,
            on_parse_failed=lambda _r: None,
            on_event=lambda **kw: events.append(kw),
        )

    def _browser_with_pages(self, pages_jobs, detail_status="login_required"):
        """pages_jobs: dict[int, list[dict]] — page number to list jobs."""
        call_state = {"page": 0}

        def evaluate(_target, script):
            if "job-detail-job-info" in script:
                call_state["page"] += 1
                page_num = call_state["page"]
                jobs = pages_jobs.get(page_num, [])
                if jobs is None:
                    return json.dumps({"status": "empty", "jobs": []})
                return json.dumps({"status": "ready", "jobs": jobs}, ensure_ascii=False)
            return json.dumps({"status": detail_status})

        return LiepinBrowser(
            new_tab=lambda url, **_kw: url,
            close_tab=lambda _t: True,
            evaluate=evaluate,
            scroll=lambda *_a, **_kw: True,
            wait_for_load=lambda *_a, **_kw: True,
        )

    def _resume_patches(self, saved_page=0):
        return (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos"),
            patch("bosshunter.collection.platforms.liepin.prune_page_progress"),
            patch("bosshunter.collection.platforms.liepin.get_page_progress", return_value=saved_page),
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress"),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected"),
        )

    def test_full_page_checkpoint_skips_keyword(self):
        jobs = [{"source_job_id": "1", "title": "AI", "company": "c", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_pages({1: jobs})
        collected = []
        events = []
        collector = LiepinCollector(
            browser=browser, safety_conn=object(),
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos"),
            patch("bosshunter.collection.platforms.liepin.prune_page_progress"),
            patch("bosshunter.collection.platforms.liepin.get_page_progress", return_value=1),
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress"),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected"),
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=1),
                self._hooks(collected, events),
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reason_code, "no_remaining_pages")
        self.assertEqual(len(collected), 0)
        skip_events = [e for e in events if e.get("phase") == "completed_keyword"]
        self.assertTrue(any("已采到最大页" in str(e.get("message", "")) for e in skip_events))

    def test_page_progress_resumes_from_saved_page(self):
        jobs_p3 = [{"source_job_id": "j3", "title": "AI", "company": "c", "city": "上海",
                    "url": "https://www.liepin.com/job/3001.shtml"}]
        browser = self._browser_with_pages({3: jobs_p3, 4: None})
        collected = []
        collector = LiepinCollector(
            browser=browser, safety_conn=object(),
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        navigation_urls = []
        browser.navigate_action = lambda _t, url: navigation_urls.append(url) or True
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos"),
            patch("bosshunter.collection.platforms.liepin.prune_page_progress"),
            patch("bosshunter.collection.platforms.liepin.get_page_progress", return_value=2),
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress"),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected"),
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=4),
                self._hooks(collected),
            )
        self.assertEqual(result.status, "completed")
        self.assertTrue(any("curPage=2" in url for url in navigation_urls))

    def test_pages_are_checkpointed_in_ascending_order(self):
        jobs = [{"source_job_id": "j", "title": "AI", "company": "c", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_pages({1: jobs, 2: jobs, 3: None})
        collected = []
        checkpoints = []
        collector = LiepinCollector(
            browser=browser, safety_conn=object(),
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos"),
            patch("bosshunter.collection.platforms.liepin.prune_page_progress"),
            patch("bosshunter.collection.platforms.liepin.get_page_progress", return_value=0),
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress",
                  side_effect=lambda _c, _s, _ci, _k, page: checkpoints.append(page)),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected"),
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=3),
                self._hooks(collected),
            )
        self.assertEqual(result.status, "completed")
        # 空页把断点推进到上限页，避免下次重复探测同一空页
        self.assertEqual(checkpoints, [1, 2, 3])

    def test_word_completion_marks_combo_and_keeps_page_progress(self):
        jobs = [{"source_job_id": "j", "title": "AI", "company": "c", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_pages({1: jobs, 2: None})
        collected = []
        checkpoints = []
        collector = LiepinCollector(
            browser=browser, safety_conn=object(),
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos"),
            patch("bosshunter.collection.platforms.liepin.prune_page_progress"),
            patch("bosshunter.collection.platforms.liepin.get_page_progress", return_value=0),
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress",
                  side_effect=lambda _c, _s, _ci, _k, page: checkpoints.append(page)),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected") as mark_complete,
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=2),
                self._hooks(collected),
            )
        self.assertEqual(result.status, "completed")
        mark_complete.assert_called_once()
        # 页级断点在空页后推进到上限页，下次直接跳过本词
        self.assertEqual(checkpoints, [1, 2])

    def test_blocked_page_does_not_mark_combo(self):
        browser = LiepinBrowser(
            new_tab=lambda url, **_kw: url,
            close_tab=lambda _t: True,
            evaluate=lambda _t, _s: json.dumps({"status": "blocked", "jobs": []}),
            scroll=lambda *_a, **_kw: True,
            wait_for_load=lambda *_a, **_kw: True,
        )
        collector = LiepinCollector(
            browser=browser, safety_conn=object(),
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos"),
            patch("bosshunter.collection.platforms.liepin.prune_page_progress"),
            patch("bosshunter.collection.platforms.liepin.get_page_progress", return_value=0),
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress"),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected") as mark_complete,
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=3),
                self._hooks(),
            )
        self.assertEqual(result.status, "blocked")
        mark_complete.assert_not_called()

    def test_saved_page_exceeds_max_pages_skips_keyword(self):
        jobs = [{"source_job_id": "j", "title": "AI", "company": "c", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_pages({1: jobs})
        collected = []
        events = []
        collector = LiepinCollector(
            browser=browser, safety_conn=object(),
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos"),
            patch("bosshunter.collection.platforms.liepin.prune_page_progress"),
            patch("bosshunter.collection.platforms.liepin.get_page_progress", return_value=5),
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress"),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected") as mark_complete,
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=3),
                self._hooks(collected, events),
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reason_code, "no_remaining_pages")
        self.assertEqual(len(collected), 0)
        mark_complete.assert_called_once()
        skip_events = [e for e in events if e.get("phase") == "completed_keyword"]
        self.assertTrue(any("已采到最大页" in str(e.get("message", "")) for e in skip_events))

    def test_ignore_checkpoint_restarts_from_first_page(self):
        jobs = [{"source_job_id": "j", "title": "AI", "company": "c", "city": "上海",
                 "salary": "15-25K",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_pages({1: jobs, 2: None})
        collected = []
        collector = LiepinCollector(
            browser=browser, safety_conn=object(),
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos"),
            patch("bosshunter.collection.platforms.liepin.prune_page_progress"),
            patch("bosshunter.collection.platforms.liepin.get_page_progress") as get_progress,
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress"),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected"),
        ):
            result = collector.collect(
                PlatformCollectionRequest(
                    "liepin", ["AI"], ["上海"], {"上海": "020"},
                    max_pages=3, ignore_checkpoint=True,
                ),
                self._hooks(collected),
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(collected), 1)
        get_progress.assert_not_called()

    def test_prune_called_on_collect_start(self):
        jobs = [{"source_job_id": "j", "title": "AI", "company": "c", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_pages({1: jobs, 2: None})
        collector = LiepinCollector(
            browser=browser, safety_conn=object(),
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos") as prune_combos,
            patch("bosshunter.collection.platforms.liepin.prune_page_progress") as prune_pages,
            patch("bosshunter.collection.platforms.liepin.get_page_progress", return_value=0),
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress"),
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected"),
        ):
            collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=2),
                self._hooks(),
            )
        prune_combos.assert_called_once()
        prune_pages.assert_called_once()

    def test_resume_ttl_hours_from_config(self):
        collector = LiepinCollector(
            config={"platforms": {"liepin": {"search": {"resume_ttl_hours": 48}}}},
        )
        self.assertEqual(collector._resume_ttl_hours(), 48)

    def test_resume_ttl_hours_clamped_to_range(self):
        collector_low = LiepinCollector(
            config={"platforms": {"liepin": {"search": {"resume_ttl_hours": -5}}}},
        )
        self.assertEqual(collector_low._resume_ttl_hours(), 1)
        collector_high = LiepinCollector(
            config={"platforms": {"liepin": {"search": {"resume_ttl_hours": 9999}}}},
        )
        self.assertEqual(collector_high._resume_ttl_hours(), 720)

    def test_resume_ttl_hours_default_when_missing(self):
        collector = LiepinCollector()
        self.assertEqual(collector._resume_ttl_hours(), 24)

    def test_resume_ttl_hours_invalid_falls_back_to_default(self):
        collector = LiepinCollector(
            config={"platforms": {"liepin": {"search": {"resume_ttl_hours": "invalid"}}}},
        )
        self.assertEqual(collector._resume_ttl_hours(), 24)

    def test_no_safety_conn_skips_resume_logic(self):
        jobs = [{"source_job_id": "j", "title": "AI", "company": "c", "city": "上海",
                 "url": "https://www.liepin.com/job/1001.shtml"}]
        browser = self._browser_with_pages({1: jobs, 2: None})
        collected = []
        collector = LiepinCollector(
            browser=browser, safety_conn=None,
            sleep=lambda _s: None, uniform=lambda _a, _b: 10.0,
        )
        with (
            patch("bosshunter.collection.platforms.liepin.SendWindowChecker.is_active", return_value=True),
            patch("bosshunter.collection.platforms.liepin.should_take_day_off", return_value=False),
            patch("bosshunter.collection.platforms.liepin.prune_collected_combos") as prune_combos,
            patch("bosshunter.collection.platforms.liepin.get_page_progress") as get_progress,
            patch("bosshunter.collection.platforms.liepin.upsert_page_progress") as upsert,
            patch("bosshunter.collection.platforms.liepin.mark_combo_collected") as mark,
        ):
            result = collector.collect(
                PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=2),
                self._hooks(collected),
            )
        self.assertEqual(result.status, "completed")
        prune_combos.assert_not_called()
        get_progress.assert_not_called()
        upsert.assert_not_called()
        mark.assert_not_called()


class LiepinRiskControlTests(LiepinTestBase):
    """猎聘 P0 风控升级：额度守卫 / 风险锁与事件 / 登录墙熔断 / 连续失败熔断 / 延迟可配。"""

    @staticmethod
    def _request(max_pages=1):
        return PlatformCollectionRequest("liepin", ["AI"], ["上海"], {"上海": "020"}, max_pages=max_pages)

    def _hooks(self, collected=None, events=None, parse_failures=None):
        collected = collected if collected is not None else []
        events = events if events is not None else []
        parse_failures = parse_failures if parse_failures is not None else []
        return CollectorHooks(
            stop_event=None,
            on_list_candidate=lambda _c: True,
            on_candidate=lambda c: collected.append(c) or True,
            on_parse_failed=lambda r: parse_failures.append(r),
            on_event=lambda **kw: events.append(kw),
        )

    def _browser(self, list_status="ready", jobs=None, detail_status="login_required"):
        jobs = jobs or []
        def evaluate(_target, script):
            if "job-detail-job-info" in script:
                return json.dumps({"status": list_status, "jobs": jobs}, ensure_ascii=False)
            return json.dumps({"status": detail_status})
        return LiepinBrowser(
            new_tab=lambda url, **_kw: url,
            close_tab=lambda _t: True,
            evaluate=evaluate,
            scroll=lambda *_a, **_kw: True,
            wait_for_load=lambda *_a, **_kw: True,
        )

    def _db(self):
        tmp = tempfile.TemporaryDirectory()
        from bosshunter.db import get_db
        conn = get_db(Path(tmp.name) / "jobs.db")
        # addCleanup 是 LIFO：先注册目录清理，再注册连接关闭，
        # 执行时连接先关，Windows 才不会因句柄未释放删不掉（WinError 32）。
        self.addCleanup(tmp.cleanup)
        self.addCleanup(conn.close)
        return conn

    def test_guard_reserves_search_and_detail_page_attempts(self):
        conn = self._db()
        from bosshunter.db import count_platform_access_today
        jobs = [{"source_job_id": "j1", "title": "AI 产品", "company": "c", "city": "上海",
                 "salary": "15-25K", "url": "https://www.liepin.com/job/1001.shtml"}]
        collector = LiepinCollector(
            browser=self._browser(jobs=jobs), safety_conn=conn,
            sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks())
        self.assertEqual(result.status, "completed")
        self.assertEqual(count_platform_access_today(conn, platform="liepin", stage="collection", action="search_page"), 1)
        self.assertEqual(count_platform_access_today(conn, platform="liepin", stage="collection", action="detail_page"), 1)

    def test_quota_exhausted_stops_before_opening_tabs(self):
        conn = self._db()
        from bosshunter.db import add_platform_access, count_platform_access_today
        for _ in range(60):
            add_platform_access(conn, "collection", "search_page", platform="liepin")
        opened = []
        browser = self._browser()
        browser.new_tab = lambda url, **_kw: opened.append(url) or url
        collector = LiepinCollector(
            browser=browser, safety_conn=conn,
            config={"platforms": {"liepin": {"search": {"daily_search_page_limit": 1}}}},
            sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks())
        self.assertEqual(result.status, "completed_with_shortage")
        self.assertIn("daily_search_page_limit", result.reason_code)
        self.assertEqual(opened, [])
        self.assertEqual(count_platform_access_today(conn, platform="liepin", stage="collection", action="search_page"), 60)

    def test_blocked_list_writes_risk_lock_and_event(self):
        conn = self._db()
        from bosshunter.db import get_active_platform_safety_lock
        collector = LiepinCollector(
            browser=self._browser(list_status="blocked"), safety_conn=conn,
            sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason_code, "rate_limit")
        lock = get_active_platform_safety_lock(conn)
        self.assertIsNotNone(lock)
        self.assertEqual(lock["reason"], "rate_limit")
        rows = conn.execute("SELECT event_type FROM risk_events WHERE event_type LIKE 'collection_liepin%'").fetchall()
        self.assertTrue(any(r[0] == "collection_liepin_rate_limit" for r in rows))

    def test_detail_script_detects_network_anomaly_as_blocked(self):
        self.assertIn("网络异常", JS_EXTRACT_DETAIL)

    def test_blanked_list_stops_and_writes_risk_lock(self):
        """页面被清空(about:blank)按风控停机，不能当登录墙继续采集。"""
        conn = self._db()
        from bosshunter.db import get_active_platform_safety_lock
        collector = LiepinCollector(
            browser=self._browser(list_status="blanked"), safety_conn=conn,
            sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason_code, "rate_limit")
        lock = get_active_platform_safety_lock(conn)
        self.assertIsNotNone(lock)
        self.assertEqual(lock["reason"], "rate_limit")

    def test_blanked_detail_stops_platform(self):
        jobs = [{"source_job_id": "j1", "title": "AI 产品", "company": "c", "city": "上海",
                 "salary": "15-25K", "url": "https://www.liepin.com/job/1001.shtml"}]
        collector = LiepinCollector(
            browser=self._browser(jobs=jobs, detail_status="blanked"),
            sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason_code, "rate_limit")

    def test_risk_scripts_detect_ban_page_and_risk_url(self):
        for script in (JS_EXTRACT_LIST, JS_EXTRACT_DETAIL):
            self.assertIn("账号行为异常", script)
            self.assertIn("captchaPage", script)

    def test_detail_tab_reused_when_navigation_available(self):
        """能主动导航时，多个岗位复用同一个详情标签页。"""
        jobs = [
            {"source_job_id": f"j{i}", "title": "AI 产品", "company": "c", "city": "上海",
             "salary": "15-25K", "url": f"https://www.liepin.com/job/100{i}.shtml"}
            for i in range(1, 4)
        ]
        opened = []
        browser = self._browser(jobs=jobs)
        original = browser.new_tab
        browser.new_tab = lambda url, **kw: opened.append(url) or original(url, **kw)
        browser.navigate_action = lambda _target, _url: True
        collector = LiepinCollector(
            browser=browser, sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks())
        self.assertEqual(result.status, "completed")
        # 1 个搜索页 + 1 个复用的详情页（3 个岗位共用）。
        self.assertEqual(len(opened), 2)

    def test_run_detail_limit_stops_run(self):
        """单轮详情页上限：鼓励分次运行，而不是一口气跑满日额。"""
        jobs = [
            {"source_job_id": f"j{i}", "title": "AI 产品", "company": "c", "city": "上海",
             "salary": "15-25K", "url": f"https://www.liepin.com/job/100{i}.shtml"}
            for i in range(1, 5)
        ]
        collector = LiepinCollector(
            browser=self._browser(jobs=jobs),
            config={"platforms": {"liepin": {"search": {"run_detail_page_limit": 2}}}},
            sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks())
        self.assertEqual(result.status, "completed_with_shortage")
        self.assertEqual(result.reason_code, "run_detail_page_limit")

    def test_consecutive_login_wall_stops_opening_detail_tabs(self):
        conn = self._db()
        jobs = [
            {"source_job_id": f"j{i}", "title": "AI 产品", "company": "c", "city": "上海",
             "salary": "15-25K", "url": f"https://www.liepin.com/job/100{i}.shtml"}
            for i in range(1, 7)
        ]
        opened = []
        browser = self._browser(jobs=jobs, detail_status="login_required")
        original = browser.new_tab
        def counting_new_tab(url, **kw):
            opened.append(url)
            return original(url, **kw)
        browser.new_tab = counting_new_tab
        collected, events = [], []
        collector = LiepinCollector(
            browser=browser, safety_conn=conn,
            sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks(collected, events))
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(collected), 6)
        # 1 个搜索页 + 前 3 个详情页，第 4 个起不再开标签
        self.assertEqual(len(opened), 4)
        self.assertTrue(any("连续需登录" in str(e.get("message", "")) for e in events))

    def test_waiting_list_keeps_existing_fail_closed_contract(self):
        # 连续失败熔断需要重构「单词失败即停」控制流，本轮未改；
        # 保留既有 fail-closed 返回值，避免擅自改变生产行为。
        conn = self._db()
        collector = LiepinCollector(
            browser=self._browser(list_status="waiting"), safety_conn=conn,
            sleep=lambda _s: None, uniform=lambda _a, _b: 0.0,
        )
        result = collector.collect(self._request(), self._hooks())
        self.assertEqual(result.status, "completed_with_shortage")
        self.assertEqual(result.reason_code, "list_not_ready")

    def test_delay_multiplier_scales_ranges_but_default_is_unchanged(self):
        default = LiepinCollector()
        self.assertEqual(default.detail_delay_range, (10.0, 18.0))
        self.assertEqual(default.page_delay_range, (20.0, 35.0))
        scaled = LiepinCollector(config={"collection": {"collection_delay_multiplier": 2.0}})
        self.assertEqual(scaled.detail_delay_range, (20.0, 36.0))
        self.assertEqual(scaled.page_delay_range, (40.0, 70.0))
        clamped = LiepinCollector(config={"collection": {"collection_delay_multiplier": 99}})
        self.assertEqual(clamped.detail_delay_range, (50.0, 90.0))
        explicit = LiepinCollector(detail_delay_range=(1.0, 2.0), page_delay_range=(3.0, 4.0))
        self.assertEqual(explicit.detail_delay_range, (1.0, 2.0))
        self.assertEqual(explicit.page_delay_range, (3.0, 4.0))
