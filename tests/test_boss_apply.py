"""Real Patchright apply regressions, with all site responses supplied in memory."""
import asyncio
import base64

import pytest

from jobs_app.browser import BrowserAutomation


ORIGIN = "https://www.zhipin.com"
RECORD_EVENT = """
function record(value) {
  const events = JSON.parse(localStorage.getItem('synthetic-events') || '[]');
  events.push(value);
  localStorage.setItem('synthetic-events', JSON.stringify(events));
}
"""
CHAT = """
<style>#chat-input { width: 320px; height: 90px; }</style>
<ul id="history"></ul>
<div id="chat-input" class="chat-input" contenteditable="true"></div>
<button type="send" class="btn-send disabled">发送</button>
<div class="btn-sendimg" aria-label="发送图片">
  <input type="file" accept="image/*">
</div>
<script>
""" + RECORD_EVENT + """
function receipt(kind, value) {
  const row = document.createElement('li');
  row.className = 'message-item item-myself';
  row.dataset.mid = 'synthetic-' + kind;
  row.innerHTML = kind === 'text'
    ? '<div class="text"><span class="text-content"></span></div>'
    : '<div class="message-image-content"><img class="message-image"></div>';
  if (kind === 'text') row.querySelector('.text-content').textContent = value;
  row.innerHTML += '<span class="message-status status-delivery">已送达</span>';
  document.querySelector('#history').append(row);
  record(kind);
}
const editor = document.querySelector('#chat-input');
const send = document.querySelector('.btn-send');
editor.oninput = () => setTimeout(() => send.classList.remove('disabled'), 100);
send.onclick = () => {
  if (send.classList.contains('disabled')) return record('disabled-click');
  receipt('text', editor.innerText);
  editor.innerHTML = '<br>';
};
document.querySelector('input[type=file]').onchange = () => receipt('image');
</script>
"""


@pytest.mark.parametrize("destination", ["same-tab", "popup"])
def test_patchright_apply_sends_once_and_closes_only_task_pages(tmp_path, monkeypatch, destination):
    """Exercise actual Page.on wrapping and popup collection through apply()."""
    pytest.importorskip("patchright.async_api")
    resume = tmp_path / "synthetic-resume.png"
    resume.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+a3ioAAAAASUVORK5CYII="
    ))

    async def check():
        adapter = BrowserAutomation(tmp_path, lambda *args: None)
        try:
            driver = await adapter._driver("boss")
            assert type(driver).__module__.startswith("patchright.")
            launch = driver.chromium.launch_persistent_context

            async def headless_launch(profile, **options):
                return await launch(profile, **(options | {"headless": True, "service_workers": "block"}))

            monkeypatch.setattr(driver.chromium, "launch_persistent_context", headless_launch)
            context = await adapter._context("boss")
            navigate = "window.open('/web/geek/chat')" if destination == "popup" else "location.href='/web/geek/chat'"
            pages = {
                ORIGIN + "/web/geek/jobs": "<p>模拟搜索结果页</p>",
                ORIGIN + "/job_detail/offline-fixture.html": (
                    '<a class="btn-startchat" href="#">立即沟通</a><script>'
                    + RECORD_EVENT
                    + "document.querySelector('a').onclick = event => {"
                    + "event.preventDefault(); record('apply'); " + navigate + ";};</script>"
                ),
                ORIGIN + "/web/geek/chat": CHAT,
            }
            requests = []

            async def offline_route(route):
                url = route.request.url
                if url in pages:
                    requests.append(url)
                    await route.fulfill(content_type="text/html; charset=utf-8", body=pages[url])
                else:
                    await route.abort()

            await context.route("**/*", offline_route)
            source = await adapter._page("boss")
            await source.goto(ORIGIN + "/web/geek/jobs")
            created = []
            context.on("page", lambda page: created.append(page))
            result = await adapter.apply(
                {"platform": "boss", "external_id": "offline-fixture",
                 "url": ORIGIN + "/job_detail/offline-fixture.html"},
                "您好，这是离线模拟招呼语。", str(resume),
            )
            assert result["status"] == "submitted", result
            assert "图片" in result["detail"]
            assert await source.evaluate("JSON.parse(localStorage.getItem('synthetic-events'))") == [
                "apply", "text", "image",
            ]
            assert requests == list(pages)
            assert len(created) == (2 if destination == "popup" else 1)
            assert all(page.is_closed() for page in created)
            assert context.pages == [source]
            assert adapter._pages["boss"] is source
            assert source.url == ORIGIN + "/web/geek/jobs"
        finally:
            await adapter.close()

    asyncio.run(check())
