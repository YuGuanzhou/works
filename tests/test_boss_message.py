"""Offline BOSS message/image regressions; no recruiting site is contacted.

The modal is a synthetic fixture for the reference workflow, not a captured
copy of the live site's DOM. All names and messages are fake.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
            # Give pushState a same-origin fixture URL; these two requests are
            # answered entirely from memory, with every other request aborted.
            for path in ("job_detail/offline-fixture.html", "web/geek/chat"):
                await page.route(
                    "https://www.zhipin.com/" + path,
                    lambda route: route.fulfill(content_type="text/html", body="<!doctype html>"),
                )
            await page.goto("https://www.zhipin.com/job_detail/offline-fixture.html")
            page.set_default_timeout(1500)
            yield page
        finally:
            await browser.close()


MODAL = """
<style>
  a.btn-startchat { display: block; width: 180px; height: 40px; }
  .dialog-wrap { position: fixed; inset: 0; z-index: 100;
    padding: 70px; background: rgba(245,245,245,.98); }
  i.icon-close { display: block; width: 30px; height: 30px; cursor: pointer; }
  textarea, #chat-input { display: block; width: 320px; height: 90px; }
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
  window.mid = 0;
  document.querySelector('.btn-send').onclick = () => events.push('small-window-text');
  document.querySelector('i.icon-close').onclick = () => {
    events.push('close');
    document.querySelector('.startchat-dialog').remove();
  };
  window.appendMessage = (value, state = 'delivery', isImage = false) => {
    const row = document.createElement('li');
    row.className = 'message-item item-myself';
    row.dataset.mid = 'synthetic-' + (++mid);
    row.innerHTML = '<div class="text"><p><span class="text-content"></span></p></div>' +
      '<div class="message-status status-' + state + '"></div>';
    if (isImage) {
      row.querySelector('.text').innerHTML = '<div class="message-image-content">' +
        '<img class="message-image" alt="模拟简历图片" width="1" height="1"></div>';
    } else {
      row.querySelector('.text-content').textContent = value;
    }
    row.querySelector('.message-status').textContent = state === 'error' ? '发送失败' :
      (state === 'delivery' ? '已送达' : '发送中');
    document.querySelector('#history').append(row);
    return row;
  };
  window.openFullChat = eventName => {
    events.push(eventName);
    history.pushState({}, '', '/web/geek/chat');
    document.querySelector('.startchat-dialog')?.remove();
    const full = document.createElement('section');
    full.id = 'full-chat';
    full.innerHTML = '<ul id="history" class="chat-record"></ul>' +
      '<div id="chat-input" class="chat-input" contenteditable="true"></div>' +
      '<button type="send" class="btn-send disabled">发送</button>' +
      '<div class="btn-sendimg" aria-label="发送图片"><input type="file" accept="image/*"></div>';
    document.body.append(full);
    const editor = full.querySelector('#chat-input');
    const send = full.querySelector('.btn-send');
    editor.oninput = () => setTimeout(() => send.classList.remove('disabled'), 100);
    send.onclick = () => {
      if (send.classList.contains('disabled')) {
        events.push('disabled-send-click');
        return;
      }
      events.push('text');
      appendMessage(editor.innerText);
      editor.innerHTML = '<br>';
    };
    full.querySelector('input[type=file]').onchange = () => {
      events.push('image');
      appendMessage('', 'delivery', true);
    };
  };
  document.querySelector('a.btn-startchat').onclick = event => {
    event.preventDefault();
    openFullChat('chat');
  };
  window.showGreetingPopup = () => {
    document.querySelector('.startchat-dialog').remove();
    const popup = document.createElement('div');
    popup.className = 'dialog-wrap greet-boss-pop';
    popup.innerHTML = '<div class="dialog-title">已向BOSS发送消息</div>' +
      '<div class="dialog-footer"><button class="btn-sure">继续沟通</button></div>';
    popup.querySelector('.btn-sure').onclick = () => {
      popup.remove();
      openFullChat('continue');
    };
    document.body.append(popup);
  };
</script>
"""


@pytest.fixture
def resume_image(tmp_path):
    image = tmp_path / "synthetic-resume.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+a3ioAAAAASUVORK5CYII="
    ))
    return image


@pytest.mark.parametrize("close_mode", ["working", "missing", "ineffective", "greeting-popup", "full-chat"])
def test_image_flow_closes_message_modal_before_opening_full_chat(tmp_path, resume_image, close_mode):
    """The background chat link cannot receive clicks while the modal covers it."""

    async def check():
        async with offline_page() as page:
            await page.set_content(MODAL)
            if close_mode == "full-chat":
                await page.evaluate("openFullChat('fixture'); events = []")
            elif close_mode == "greeting-popup":
                await page.evaluate("showGreetingPopup()")
            elif close_mode == "missing":
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
                page, "您好，模拟招聘方。我熟悉 Python 开发。", str(resume_image)
            )
            events = await page.evaluate("events")
            assert "small-window-text" not in events, "Custom greetings belong in the full chat"
            assert "disabled-send-click" not in events, "Wait for the editor's debounced send state"
            if close_mode in ("working", "greeting-popup", "full-chat"):
                assert result["status"] == "submitted", result
                expected_events = {"working": ["close", "chat"], "greeting-popup": ["continue"], "full-chat": []}[close_mode]
                assert events == expected_events + ["text", "image"]
                assert len(chat_click_attempts) == (1 if close_mode == "working" else 0)
                assert "图片" in result["detail"]
            else:
                assert result["status"] == "unconfirmed", result
                assert not chat_click_attempts, "Do not click a link still covered by the modal"
                assert "text" not in events and "chat" not in events and "image" not in events
                assert await page.locator(".startchat-dialog").is_visible()
                assert any(stage in result["detail"] for stage in ("关闭", "窗口", "进入完整聊天页"))

    asyncio.run(check())


@pytest.mark.parametrize(("scenario", "expected"), [
    ("pending", "unconfirmed"),
    ("failed", "failed"),
    ("multiline_br", "submitted"),
    ("old_error_new_success", "submitted"),
    ("old_success_no_new_message", "unconfirmed"),
    ("old_pending_ack_no_new", "unconfirmed"),
    ("old_pending_ack_new_pending", "unconfirmed"),
    ("editor_removed_after_success", "submitted"),
])
def test_text_delivery_uses_current_message_receipt(tmp_path, scenario, expected):
    async def check():
        async with offline_page() as page:
            await page.set_content(MODAL)
            await page.evaluate("openFullChat('fixture'); events = []")
            greeting = "您好，模拟招聘方。我熟悉 Python 开发。"
            if scenario == "multiline_br":
                greeting = "您好，模拟招聘方。\n我熟悉 Python 开发。\n期待进一步沟通。"
            await page.evaluate(r"""({scenario, greeting}) => {
              if (scenario.startsWith('old_')) {
                const failed = scenario === 'old_error_new_success';
                const pending = scenario.startsWith('old_pending_ack');
                appendMessage(greeting, pending ? 'loading' : (failed ? 'error' : 'delivery'));
              }
              const send = document.querySelector('.btn-send');
              const original = send.onclick;
              send.onclick = () => {
                if (scenario.startsWith('old_pending_ack')) {
                  events.push('text');
                  const old = document.querySelector('#history li:first-child');
                  old.dataset.mid = 'synthetic-server-ack';
                  old.querySelector('.message-status').className = 'message-status status-delivery';
                  old.querySelector('.message-status').textContent = '已送达';
                  if (scenario === 'old_pending_ack_new_pending') appendMessage(greeting, 'loading');
                  document.querySelector('#chat-input').innerHTML = '<br>';
                  return;
                }
                if (scenario === 'old_success_no_new_message') {
                  events.push('text');
                  document.querySelector('#chat-input').innerHTML = '<br>';
                  return;
                }
                original();
                const status = document.querySelector('#history li:last-child .message-status');
                if (scenario === 'pending' || scenario === 'failed') {
                  status.className = 'message-status status-' + (scenario === 'pending' ? 'loading' : 'error');
                  status.textContent = scenario === 'pending' ? '发送中' : '发送失败';
                }
                if (scenario === 'multiline_br') {
                  const content = document.querySelector('#history li:last-child .text-content');
                  const lines = content.textContent.split('\n');
                  content.replaceChildren();
                  lines.forEach((line, index) => {
                    if (index) content.append(document.createElement('br'));
                    content.append(document.createTextNode(line));
                  });
                }
                if (scenario === 'editor_removed_after_success') {
                  document.querySelector('#chat-input').remove();
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


@pytest.mark.parametrize(("scenario", "expected"), [
    ("delivery", "submitted"), ("read", "submitted"),
    ("pending", "unconfirmed"), ("failed", "unconfirmed"),
    ("old_image_no_new", "unconfirmed"), ("peer_image", "unconfirmed"),
])
def test_image_delivery_requires_new_own_message_receipt(tmp_path, resume_image, scenario, expected):
    async def check():
        async with offline_page() as page:
            await page.set_content(MODAL)
            await page.evaluate("openFullChat('fixture'); events = []")
            await page.evaluate("""scenario => {
              if (scenario === 'old_image_no_new') appendMessage('', 'delivery', true);
              const upload = document.querySelector('input[type=file]');
              const original = upload.onchange;
              upload.onchange = () => {
                if (scenario === 'old_image_no_new') {
                  events.push('image');
                  return;
                }
                original();
                const row = document.querySelector('#history li:last-child');
                const status = row.querySelector('.message-status');
                if (scenario === 'peer_image') row.classList.remove('item-myself');
                if (scenario === 'pending') status.className = 'message-status status-loading';
                if (scenario === 'failed') status.className = 'message-status status-error';
                if (scenario === 'read') status.className = 'message-status status-read';
              };
            }""", scenario)

            class ImmediatePage:
                def __getattr__(self, name):
                    return getattr(page, name)

                async def wait_for_timeout(self, milliseconds):
                    pass

            adapter = BrowserAutomation(tmp_path, lambda *args: None)
            result = await adapter._boss_image(ImmediatePage(), str(resume_image))
            assert result["status"] == expected, result
            assert await page.evaluate("events") == ["image"]

    asyncio.run(check())


def test_closing_detail_page_cannot_overwrite_confirmed_delivery(tmp_path):
    async def check():
        outcome = {"status": "submitted", "detail": "模拟消息已确认发送"}
        page = SimpleNamespace(
            goto=AsyncMock(), wait_for_timeout=AsyncMock(), on=Mock(),
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
