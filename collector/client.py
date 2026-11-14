"""Playwright 采集客户端。

所有平台（含纯 JSON 接口）都通过 Playwright 发起访问：
- HTML 平台：page.goto 后取渲染后的页面文本；
- JSON 平台：浏览器上下文内 APIRequestContext 发请求，自动携带登录 Cookie/Token。
内置 429 限流退避（Retry-After）与登录过期自动重登，单平台故障向上抛 PlatformError，
由调度器隔离，不影响其他平台。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from playwright.sync_api import sync_playwright

from .parsers import ParseError


class PlatformError(Exception):
    """平台不可用（连续失败/连接拒绝等），区别于解析失败。"""


class AuthExpired(Exception):
    """登录态失效，需要重新登录。"""


@dataclass
class FetchResult:
    url: str
    status: int
    text: str


class PlatformClient:
    def __init__(self, cfg: dict, log=print, max_retries: int = 4):
        self.cfg = cfg
        self.log = log
        self.max_retries = max_retries
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._token: str | None = None
        self.base = cfg["base_url"].rstrip("/")
        # 客户端侧节流：按平台限流窗口留出安全余量，主动避免 429
        rw = float(cfg.get("rate_window_s", 1))
        rmax = max(1, int(cfg.get("rate_max", 5)) - 1)
        self.min_interval = rw / rmax
        self._last_send = 0.0
        self._throttle_lock = threading.Lock()

    # ---------- 生命周期 ----------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> None:
        self._pw = sync_playwright().start()
        # 优先用系统安装的 Chrome/Edge（无需额外下载浏览器），
        # 都不可用时回退到 Playwright 自带 chromium
        self._browser = None
        last_err = None
        for kwargs in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
            try:
                self._browser = self._pw.chromium.launch(
                    headless=True, **kwargs)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
        if self._browser is None:
            raise PlatformError(f"无法启动任何浏览器: {last_err}")
        self._ctx = self._browser.new_context()
        self._page = self._ctx.new_page()

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._browser = self._pw = self._ctx = self._page = None

    # ---------- 登录 ----------
    def login(self) -> None:
        auth = self.cfg.get("auth")
        if not auth:
            return
        if auth.get("type") == "api":  # mixed_login：JSON 登录拿 token
            resp = self._raw_request("POST", self.base + auth["login_path"],
                                     json_body={"username": auth["username"],
                                                "password": auth["password"]},
                                     allow_redirects=False)
            if resp.status != 200:
                raise PlatformError(f"接口登录失败 status={resp.status}")
            import json
            self._token = json.loads(resp.text)["access_token"]
            self.log(f"[{self.cfg['code']}] API 登录成功")
        else:  # 表单登录
            url = self.base + auth["login_path"]
            self._page.goto(url, wait_until="domcontentloaded")
            self._page.fill("input[name='username']", auth["username"])
            self._page.fill("input[name='password']", auth["password"])
            self._page.click("button")
            self._page.wait_for_load_state("domcontentloaded")
            if "/login" in self._page.url:
                raise PlatformError("表单登录失败，仍停留在登录页")
            self.log(f"[{self.cfg['code']}] 表单登录成功")

    # ---------- 底层请求 ----------
    def _throttle(self) -> None:
        with self._throttle_lock:
            wait = self._last_send + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_send = time.monotonic()

    def _headers(self) -> dict:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _raw_request(self, method: str, url: str, json_body=None,
                     allow_redirects: bool = True) -> FetchResult:
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self._ctx.request.fetch(
                    url, method=method, data=json_body,
                    headers=self._headers(),
                    max_redirects=0 if not allow_redirects else 20)
            except Exception as e:  # 连接类错误，退避重试
                if attempt + 1 >= self.max_retries:
                    raise PlatformError(f"请求 {url} 连续失败: {e}") from e
                wait = 0.5 * (attempt + 1)
                self.log(f"[{self.cfg['code']}] 连接异常 {e}，{wait}s 后重试")
                time.sleep(wait)
                continue
            if resp.status == 429:
                retry_after = float(resp.headers.get("retry-after", 1))
                self.log(f"[{self.cfg['code']}] 触发限流 429，等待 {retry_after}s")
                time.sleep(max(retry_after, 0.2))
                continue
            return FetchResult(url, resp.status, resp.text())
        raise PlatformError(f"请求 {url} 超过最大重试次数")

    def _is_login_redirect(self, result: FetchResult) -> bool:
        return result.status in (301, 302) and "/login" in (
            result.text or "")

    # ---------- 业务取数（带登录过期重登，各一次） ----------
    def fetch_html(self, path: str, relogin_on_expire: bool = True) -> str:
        url = self.base + path
        for _ in range(2):
            self._throttle()
            try:
                resp = self._ctx.request.fetch(url, headers=self._headers(),
                                               max_redirects=0)
            except Exception as e:
                raise PlatformError(f"HTML 请求失败 {url}: {e}") from e
            if resp.status == 429:  # fetch 内部未走 _raw_request，这里补退避
                time.sleep(float(resp.headers.get("retry-after", 1)))
                continue
            if resp.status in (301, 302) and "/login" in (
                    resp.headers.get("location", "")):
                if relogin_on_expire and self.cfg.get("auth"):
                    self.log(f"[{self.cfg['code']}] 登录过期，重新登录")
                    self.login()
                    continue
                raise AuthExpired("未登录或会话已过期")
            if resp.status == 401:
                if relogin_on_expire and self.cfg.get("auth"):
                    self.login()
                    continue
                raise AuthExpired("会话已过期")
            if resp.status >= 500:
                raise PlatformError(f"平台服务异常 status={resp.status}")
            if resp.status != 200:
                raise PlatformError(f"取 HTML 失败 status={resp.status}")
            return resp.text()
        raise PlatformError(f"HTML 取数重试耗尽 {url}")

    def fetch_json(self, path: str, relogin_on_expire: bool = True) -> str:
        url = self.base + path
        for _ in range(2):
            result = self._raw_request("GET", url)
            if result.status == 401:
                if relogin_on_expire and self.cfg.get("auth"):
                    self.log(f"[{self.cfg['code']}] Token 过期，重新登录")
                    self.login()
                    continue
                raise AuthExpired("Token 已过期")
            if result.status >= 500:
                raise PlatformError(f"平台服务异常 status={result.status}")
            if result.status != 200:
                raise PlatformError(f"取 JSON 失败 status={result.status}")
            return result.text
        raise PlatformError(f"JSON 取数重试耗尽 {url}")
