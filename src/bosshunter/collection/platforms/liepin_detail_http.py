"""猎聘详情页纯 HTTP 抓取（P3 API 化）。

2026-11-12 实测结论（抓包 + 匿名直连验证）：

1. **详情 JD 就在服务端渲染的 HTML 里**，不需要任何 XHR 接口。
   锚点是 ``dd[data-selector="job-intro-content"]``。
   原 docs/liepin-risk-control-plan.md 假设要逆向 ``api-lpt`` 那类内部接口，
   实际完全不需要——这也解释了为什么浏览器渲染能拿到 JD：HTML 首包就带。

2. **未登录的纯 HTTP GET 就能拿到完整 JD**（8/8 成功，无验证页）。
   不需要 cookie、不需要 XSRF、不需要浏览器。

这消除了风控的最大来源：原实现每个岗位开一个标签页 + 完整页面渲染 +
10~18 秒等待。改为 HTTP 后单请求约 200KB、耗时 <1 秒，没有标签页开关轨迹、
没有渲染指纹、没有 DOM 查询。

失败一律抛 ``DetailFetchFallback``，由调用方回退到原浏览器流程——
HTTP 路径若被针对性封，回退能保住可用性（用户 2026-11-12 明确要求）。
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from typing import Any, Callable

import httpx

DETAIL_HTML_URL = "https://www.liepin.com/job/{job_id}.shtml"

# 与浏览器版 JS_EXTRACT_DETAIL 的判定词表保持一致：HTTP 路径若看到这些
# 文案，说明风控已经介入，必须按 blocked 上报而不是回退。
_BLOCKED_PATTERNS = re.compile(
    r"滑动验证|安全验证|访问过于频繁|请完成安全验证|人机验证|验证码|"
    r"账号行为异常|异常访问行为|暂时禁止访问"
)
_OFFLINE_PATTERNS = re.compile(r"职位不存在|职位已下线|该职位已关闭|页面不存在|职位已暂停")

# JD 正文锚点。dd 的 data-selector 是猎聘自己给前端用的稳定钩子，
# 比 hashed class 稳（见模块文档 2026-08-26 的既有结论）。
_JD_RE = re.compile(
    r'data-selector="job-intro-content"[^>]*>([\s\S]*?)</dd>',
    re.IGNORECASE,
)
# 主岗位标题：应聘卡片里的 name-box > span.name（不是推荐卡片里的 ellipsis-1）。
_TITLE_RE = re.compile(
    r'class="job-apply-content">[\s\S]{0,600}?<span class="name[^"]*"[^>]*>([\s\S]*?)</span>',
    re.IGNORECASE,
)
_SALARY_RE = re.compile(
    r'class="job-apply-content">[\s\S]{0,1200}?<span class="salary"[^>]*>([\s\S]*?)</span>',
    re.IGNORECASE,
)
_CITY_RE = re.compile(
    r'class="job-properties">[\s\S]{0,200}?<span>([^<]{1,40})</span>',
    re.IGNORECASE,
)
_COMPANY_RE = re.compile(
    r'href="https://www\.liepin\.com/company/\d+/"[^>]*>\s*·?\s*([^<]{1,80})</a>',
    re.IGNORECASE,
)

# 浏览器 UA 用固定 Chrome 版本，和抓包时的一致；不带 UA 会被 CDN 判为脚本。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


class DetailFetchFallback(RuntimeError):
    """HTTP 详情抓取不可用，调用方应回退到浏览器流程。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass
class DetailFetchResult:
    """一次 HTTP 详情抓取的结果。

    ``status`` 取值与浏览器版 JS_EXTRACT_DETAIL 对齐：
    ``ready`` / ``offline`` / ``blocked``。
    """

    status: str
    jd: str = ""
    title: str = ""
    salary: str = ""
    city: str = ""
    company: str = ""
    url: str = ""


def _clean_text(value: str) -> str:
    """去标签、反转义、压空白——与浏览器 innerText 口径对齐。"""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|li|dd|dt)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = text.replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_detail_html(body: str, url: str = "") -> DetailFetchResult:
    """从详情页 HTML 解析 JD 与元信息。

    判定顺序与浏览器版一致：先看风控文案，再看下线文案，最后才解析 JD。
    风控/下线优先，避免把验证页当成「结构变化」静默吞掉。
    """
    # 只看可见文本区做判定，避免 script/CSS 里的同名 token 误触发。
    visible = re.sub(r"<script[\s\S]*?</script>", " ", body, flags=re.IGNORECASE)
    visible = re.sub(r"<style[\s\S]*?</style>", " ", visible, flags=re.IGNORECASE)
    plain = _clean_text(visible)

    if _BLOCKED_PATTERNS.search(plain):
        return DetailFetchResult(status="blocked", url=url)
    if _OFFLINE_PATTERNS.search(plain):
        return DetailFetchResult(status="offline", url=url)

    jd_match = _JD_RE.search(body)
    jd = _clean_text(jd_match.group(1)) if jd_match else ""
    if not jd:
        # 拿不到 JD 但页面正常（例如岗位簇/推荐页）：交给调用方按
        # 「JD 不可用」处理，保留列表信息。
        return DetailFetchResult(status="no_jd", url=url)

    def pick(pattern: re.Pattern[str]) -> str:
        match = pattern.search(body)
        return _clean_text(match.group(1)) if match else ""

    return DetailFetchResult(
        status="ready",
        jd=jd,
        title=pick(_TITLE_RE),
        salary=pick(_SALARY_RE),
        city=pick(_CITY_RE),
        company=pick(_COMPANY_RE),
        url=url,
    )


class LiepinDetailFetcher:
    """纯 HTTP 拉取猎聘详情页。

    ``get`` 可注入以便测试；默认用 httpx，且 ``trust_env=False``
    避免本机代理环境变量把请求导向代理（与项目其他 HTTP 调用一致）。
    """

    def __init__(
        self,
        *,
        get: Callable[[str], Any] | None = None,
        timeout: float = 15.0,
        max_html_bytes: int = 2_000_000,
    ) -> None:
        self.timeout = timeout
        self.max_html_bytes = max_html_bytes
        self._get = get or self._default_get

    def _default_get(self, url: str) -> Any:
        return httpx.get(
            url,
            headers=_HEADERS,
            timeout=self.timeout,
            follow_redirects=True,
            trust_env=False,
        )

    def fetch(self, job_id: str, url: str = "") -> DetailFetchResult:
        """抓取一个岗位详情。

        任何不可用情形都抛 ``DetailFetchFallback``，让调用方回退浏览器；
        只有明确的 ``blocked`` 由返回值表达（风控要上报，不能静默回退）。
        """
        target = url or DETAIL_HTML_URL.format(job_id=job_id)
        if not str(job_id or "").strip():
            raise DetailFetchFallback("missing_job_id", "猎聘详情页缺少岗位 ID")

        try:
            response = self._get(target)
        except Exception as exc:  # noqa: BLE001 - 网络异常一律回退
            raise DetailFetchFallback("network_error", f"猎聘详情页 HTTP 请求失败：{exc}") from exc

        status_code = getattr(response, "status_code", 0)
        if status_code != 200:
            raise DetailFetchFallback(
                "http_status",
                f"猎聘详情页 HTTP {status_code}",
            )

        try:
            body = response.text
        except Exception as exc:  # noqa: BLE001
            raise DetailFetchFallback("decode_error", f"猎聘详情页解码失败：{exc}") from exc

        if not isinstance(body, str) or not body.strip():
            raise DetailFetchFallback("empty_body", "猎聘详情页返回空内容")
        if len(body) > self.max_html_bytes:
            raise DetailFetchFallback("oversized_body", "猎聘详情页体积异常，回退浏览器")

        result = parse_detail_html(body, url=str(getattr(response, "url", target) or target))
        if result.status == "no_jd":
            # 结构变化或被替换成推荐页：HTTP 路径判不准，交浏览器确认，
            # 避免把真实岗位误判成「JD 不可用」。
            raise DetailFetchFallback("no_jd_in_html", "猎聘详情页 HTML 未含 JD 锚点，回退浏览器")
        return result


__all__ = [
    "DETAIL_HTML_URL",
    "DetailFetchFallback",
    "DetailFetchResult",
    "LiepinDetailFetcher",
    "parse_detail_html",
]
