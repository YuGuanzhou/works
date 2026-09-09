"""BOSS login/navigation regressions; recruiting sites are never contacted."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from jobs_app.browser import BrowserAutomation, PLATFORMS, SPEC


class SessionFrame:
    def __init__(self, url):
        self.url = url


class SessionPage:
    def __init__(self, url="about:blank"):
        self.main_frame = SessionFrame(url)
        self.handlers = {}
        self.goto = AsyncMock(side_effect=self._goto)
        self.wait_for_timeout = AsyncMock()
        self.bring_to_front = AsyncMock()

    @property
    def url(self):
        return self.main_frame.url

    async def _goto(self, url, **kwargs):
        self.main_frame.url = url

    def is_closed(self):
        return False

    def on(self, event, callback):
        self.handlers[event] = callback


def session(tmp_path, page, body="", logged_out=False):
    adapter = BrowserAutomation(tmp_path, lambda *args: None)
    adapter._pages["boss"] = page
    adapter._page = AsyncMock(return_value=page)
    adapter._context = AsyncMock(return_value=SimpleNamespace())
    adapter._body = AsyncMock(return_value=body)
    adapter._visible = AsyncMock(side_effect=lambda _, selector: (
        object() if logged_out and selector == SPEC["boss"]["out"] else None
    ))
    return adapter


def test_repeated_login_preserves_existing_qr_page(tmp_path):
    async def check():
        page = SessionPage(PLATFORMS["boss"]["login"] + "?fixture=qr")
        adapter = session(tmp_path, page, logged_out=True)
        for _ in range(2):
            assert (await adapter.open_login("boss"))["state"] == "login_required"
        page.goto.assert_not_awaited()
        assert page.bring_to_front.await_count == 2

    asyncio.run(check())


@pytest.mark.parametrize("body,state", [
    ("正在加载", "unknown"), ("请完成安全验证", "verification_required"),
])
def test_first_open_does_not_redirect_unknown_or_challenge(tmp_path, body, state):
    async def check():
        page = SessionPage()
        adapter = session(tmp_path, page, body=body)
        assert (await adapter.open_login("boss"))["state"] == state
        page.goto.assert_awaited_once_with(PLATFORMS["boss"]["home"], wait_until="domcontentloaded")

    asyncio.run(check())


def test_first_open_uses_login_url_only_after_positive_logged_out_marker(tmp_path):
    async def check():
        page = SessionPage()
        adapter = session(tmp_path, page, logged_out=True)
        await adapter.open_login("boss")
        await adapter.open_login("boss")
        assert page.goto.await_args_list == [
            call(PLATFORMS["boss"]["home"], wait_until="domcontentloaded"),
            call(PLATFORMS["boss"]["login"], wait_until="domcontentloaded"),
        ]

    asyncio.run(check())


def test_navigation_monitor_ignores_iframes_and_detects_main_frame_loop(tmp_path):
    async def check():
        page = SessionPage(PLATFORMS["boss"]["home"])
        adapter = session(tmp_path, page)
        adapter._watch_navigation(page)
        for _ in range(8):
            page.handlers["framenavigated"](SessionFrame(page.url))
        assert not adapter._unstable(page)
        for index in range(4):
            page.main_frame.url = PLATFORMS["boss"]["home"] + f"?fixture={index}"
            page.handlers["framenavigated"](page.main_frame)
        result = await adapter.status("boss")
        assert result["state"] == result["status"] == "browser_blocked"
        assert not result["logged_in"]
        assert all("?" not in path for _, path in adapter._navigation[page])
        adapter._body.assert_not_awaited()
        page.handlers["close"]()
        assert page not in adapter._navigation

    asyncio.run(check())


def test_initial_blank_is_allowed_but_returned_blank_is_blocked(tmp_path):
    async def check():
        page = SessionPage()
        adapter = session(tmp_path, page)
        adapter._watch_navigation(page)
        page.handlers["framenavigated"](page.main_frame)
        await adapter._before_navigation(page, "boss")
        adapter._body.assert_not_awaited()
        assert not adapter._returned_blank(page)
        page.main_frame.url = PLATFORMS["boss"]["home"]
        page.handlers["framenavigated"](page.main_frame)
        page.main_frame.url = "about:blank"
        page.handlers["framenavigated"](page.main_frame)
        with pytest.raises(RuntimeError, match="退回空白页"):
            await adapter._before_navigation(page, "boss")
        assert (await adapter.status("boss"))["state"] == "browser_blocked"
        adapter._body.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["search", "rejected_companies"])
@pytest.mark.parametrize("blocked", [False, True])
def test_task_preserves_challenge_or_loop_before_navigation(tmp_path, operation, blocked):
    async def check():
        page = SessionPage(PLATFORMS["boss"]["home"])
        adapter = session(tmp_path, page, body="" if blocked else "请完成安全验证")
        if blocked:
            adapter._watch_navigation(page)
            for _ in range(4):
                page.handlers["framenavigated"](page.main_frame)
        with pytest.raises(RuntimeError, match="刷新|重定向|安全验证"):
            if operation == "search":
                results = adapter.search("boss", {"cities": ["北京"], "keywords": ["Python"]}, lambda: False)
                try:
                    await anext(results)
                finally:
                    await results.aclose()
            else:
                await adapter.rejected_companies("boss")
        page.goto.assert_not_awaited()

    asyncio.run(check())


def test_offline_patchright_context_and_dom(tmp_path, monkeypatch):
    """Launch an isolated headless Chrome profile and block all page requests."""
    pytest.importorskip("patchright.async_api")

    async def check():
        adapter = BrowserAutomation(tmp_path, lambda *args: None)
        try:
            driver = await adapter._driver("boss")
            assert type(driver).__module__.startswith("patchright.")
            assert await adapter._driver("boss") is driver
            other_driver = await adapter._driver("liepin")
            assert type(other_driver).__module__.startswith("playwright.")
            original_launch = driver.chromium.launch_persistent_context
            launch_options = []

            async def headless_launch(profile, **options):
                launch_options.append(options.copy())
                return await original_launch(profile, **(options | {"headless": True}))

            monkeypatch.setattr(driver.chromium, "launch_persistent_context", headless_launch)
            context = await adapter._context("boss")
            await context.route("**/*", lambda route: route.abort())
            page = await adapter._page("boss")
            await page.set_content('''<ul class="rec-job-list"><li class="job-card-box">
                <a class="job-name" href="/job_detail/offline.html">Python 开发</a>
                <span class="company-name">离线测试公司</span><span class="salary">15-25K</span>
                <span class="company-location">北京</span></li></ul>
                <textarea id="editor">离线招呼语</textarea>''')
            jobs = await adapter._dom_jobs(page, "boss")
            assert len(jobs) == 1 and jobs[0]["title"] == "Python 开发"
            assert jobs[0]["company"] == "离线测试公司"
            assert await page.locator("#editor").evaluate("el => el.value") == "离线招呼语"
            assert launch_options[0]["channel"] == "chrome"
            assert launch_options[0]["no_viewport"] is True
            assert "viewport" not in launch_options[0] and "user_agent" not in launch_options[0]
            await page.set_content('''<a class="btn btn-outline header-login-btn" href="/web/user/">登录</a>
                <button id="qr-switch" onclick="this.textContent='扫码已切换'">APP扫码登录</button>''')
            assert (await adapter.status("boss"))["state"] == "login_required"
            # Exercise the production selector against real DOM, with navigation kept in memory.
            navigation = SessionPage()
            navigation.locator = page.locator
            adapter._pages["boss"] = navigation
            monkeypatch.setattr(adapter, "_page", AsyncMock(return_value=navigation))
            await adapter.open_login("boss")
            assert await page.locator("#qr-switch").inner_text() == "扫码已切换"
            assert navigation.goto.await_count == 2
        finally:
            await adapter.close()

    asyncio.run(check())
