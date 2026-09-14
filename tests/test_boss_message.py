"""Offline BOSS message/image regressions; no recruiting site is contacted.

The modal is a synthetic fixture for the reference workflow, not a captured
copy of the live site's DOM. All names and messages are fake.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jobs_app.browser import BrowserAutomation


@asynccontextmanager
async def offline_page():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as driver:
        browser = None
        for channel in ("chrome", "msedge", None):
            try:
                browser = await driver.chromium.launch(channel=channel, headless=True)
                break
            except Exception:
                pass
        if browser is None:
            pytest.skip("An installed Chromium browser is needed for offline DOM verification")
        try:
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            page.set_default_timeout(1500)
            yield page
        finally:
            await browser.close()


MODAL = """
<style>
  a.btn-startchat { display: block; width: 180px; height: 40px; }
  .dialog-wrap.startchat-dialog { position: fixed; inset: 0; z-index: 100;
    padding: 70px; background: rgba(245,245,245,.98); }
  i.icon-close { display: block; width: 30px; height: 30px; cursor: pointer; }
  textarea { display: block; width: 320px; height: 90px; }
</style>
<a class="btn-startchat" href="#">继续沟通</a>
<div class="dialog-wrap startchat-dialog" role="dialog" aria-label="模拟招聘方聊天">
  <div class="dialog-title"><a class="close"><i class="icon-close" aria-label="关闭">×</i></a></div>
  <div class="startchat-content"><div class="message">
    <ul id="history" class="message-list"></ul>
    <textarea class="input-area"></textarea>
    <button type="send" class="btn-send">发送</button>
  </div></div>
</div>
<script>
  window.events = [];
  document.querySelector('.btn-send').onclick = () => {
    events.push('text');
    const input = document.querySelector('textarea');
    const message = document.createElement('li');
    message.className = 'message-item';
    const text = document.createElement('p');
    text.className = 'text';
    text.textContent = input.value;
    const status = document.createElement('span');
    status.className = 'status success';
    status.textContent = '已发送';
    message.append(text, status);
    document.querySelector('#history').append(message);
    input.value = '';
  };
  document.querySelector('i.icon-close').onclick = () => {
    events.push('close');
    document.querySelector('.startchat-dialog').remove();
  };
  document.querySelector('a.btn-startchat').onclick = event => {
    event.preventDefault();
    events.push('chat');
    const toolbar = document.createElement('div');
    toolbar.className = 'btn-sendimg';
    toolbar.setAttribute('aria-label', '发送图片');
    const upload = document.createElement('input');
    upload.type = 'file';
    upload.accept = 'image/*';
    upload.onchange = () => {
      events.push('image');
      const receipt = document.createElement('div');
      receipt.className = 'dialog-con';
      receipt.textContent = '图片发送成功';
      document.body.append(receipt);
    };
    toolbar.append(upload);
    document.body.append(toolbar);
  };
</script>
"""


@pytest.mark.parametrize("close_mode", ["working", "missing", "ineffective"])
def test_image_flow_closes_message_modal_before_opening_full_chat(tmp_path, close_mode):
    """The background chat link cannot receive clicks while the modal covers it."""
    image = tmp_path / "synthetic-resume.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+a3ioAAAAASUVORK5CYII="
    ))

    async def check():
        async with offline_page() as page:
            await page.set_content(MODAL)
            if close_mode == "missing":
                await page.locator(".dialog-title > a.close").evaluate("el => el.remove()")
            elif close_mode == "ineffective":
                await page.locator("i.icon-close").evaluate(
                    "el => el.onclick = () => events.push('close')"
                )
            adapter = BrowserAutomation(tmp_path, lambda *args: None)
            original_visible = adapter._visible
            chat_click_attempts = []

            class ObservedChat:
                def __init__(self, locator):
                    self.locator = locator

                def __getattr__(self, name):
                    return getattr(self.locator, name)

                async def click(self, **kwargs):
                    chat_click_attempts.append(True)
                    return await self.locator.click(**kwargs)

            async def visible(root, selector):
                result = await original_visible(root, selector)
                if result and "a.btn-startchat" in selector:
                    return ObservedChat(result)
                return result

            adapter._visible = visible
            result = await adapter._boss_message(
                page, "您好，模拟招聘方。我熟悉 Python 开发。", str(image)
            )
            events = await page.evaluate("events")
            assert events.count("text") == 1, "Image preparation must not resend the greeting"
            if close_mode == "working":
                assert result["status"] == "submitted", result
                assert events == ["text", "close", "chat", "image"]
                assert len(chat_click_attempts) == 1
                assert "图片发送成功" in result["detail"]
            else:
                assert result["status"] == "unconfirmed", result
                assert not chat_click_attempts, "Do not click a link still covered by the modal"
                assert "chat" not in events and "image" not in events
                assert await page.locator(".startchat-dialog").is_visible()
                assert "招呼语" in result["detail"]
                assert "关闭" in result["detail"] or "窗口" in result["detail"]

    asyncio.run(check())


@pytest.mark.parametrize(("scenario", "expected"), [
    ("pending", "unconfirmed"),
    ("failed", "failed"),
    ("old_error_new_success", "submitted"),
    ("old_success_no_new_message", "unconfirmed"),
    ("editor_removed_after_success", "submitted"),
])
def test_text_delivery_uses_current_message_receipt(tmp_path, scenario, expected):
    async def check():
        async with offline_page() as page:
            await page.set_content(MODAL)
            greeting = "您好，模拟招聘方。我熟悉 Python 开发。"
            await page.evaluate("""({scenario, greeting}) => {
              if (scenario.startsWith('old_')) {
                const row = document.createElement('li');
                row.className = 'message-item';
                const text = document.createElement('p');
                text.className = 'text';
                text.textContent = greeting;
                const status = document.createElement('span');
                const failed = scenario === 'old_error_new_success';
                status.className = 'status ' + (failed ? 'error' : 'success');
                status.textContent = failed ? '失败' : '已发送';
                row.append(text, status);
                document.querySelector('#history').append(row);
              }
              const send = document.querySelector('.btn-send');
              const original = send.onclick;
              send.onclick = () => {
                if (scenario === 'old_success_no_new_message') {
                  events.push('text');
                  document.querySelector('textarea').value = '';
                  return;
                }
                original();
                const status = document.querySelector('#history li:last-child .status');
                if (scenario === 'pending' || scenario === 'failed') {
                  status.className = 'status ' + (scenario === 'pending' ? 'sending' : 'error');
                  status.textContent = scenario === 'pending' ? '发送中' : '失败';
                }
                if (scenario === 'editor_removed_after_success') {
                  document.querySelector('textarea').remove();
                }
              };
            }""", {"scenario": scenario, "greeting": greeting})

            class ImmediatePage:
                # These fixtures never change with time; keep real Chromium locator semantics.
                def __getattr__(self, name):
                    return getattr(page, name)

                async def wait_for_timeout(self, milliseconds):
                    pass

            adapter = BrowserAutomation(tmp_path, lambda *args: None)
            result = await adapter._boss_message(ImmediatePage(), greeting, None)
            assert result["status"] == expected, result
            assert await page.evaluate("events") == ["text"]

    asyncio.run(check())


def test_closing_detail_page_cannot_overwrite_confirmed_delivery(tmp_path):
    async def check():
        outcome = {"status": "submitted", "detail": "模拟消息已确认发送"}
        page = SimpleNamespace(
            goto=AsyncMock(), wait_for_timeout=AsyncMock(),
            close=AsyncMock(side_effect=RuntimeError("simulated browser close failure")),
        )
        button = SimpleNamespace(
            inner_text=AsyncMock(return_value="立即沟通"),
            is_disabled=AsyncMock(return_value=False), click=AsyncMock(),
        )
        adapter = BrowserAutomation(tmp_path, lambda *args: None)
        adapter._context = AsyncMock(return_value=SimpleNamespace(
            new_page=AsyncMock(return_value=page)
        ))
        adapter._guard = AsyncMock(return_value="")
        adapter._find_apply = AsyncMock(return_value=button)
        adapter._boss_message = AsyncMock(return_value=outcome)
        result = await adapter.apply(
            {"platform": "boss", "external_id": "offline-fixture",
             "url": "https://www.zhipin.com/job_detail/offline-fixture.html"},
            "您好，模拟招聘方。",
        )
        assert result == outcome
        button.click.assert_awaited_once()
        adapter._boss_message.assert_awaited_once()
        page.close.assert_awaited_once()

    asyncio.run(check())
