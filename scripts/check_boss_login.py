"""Opt-in, read-only BOSS login observation. Never logs in, searches, or sends messages."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobs_app.browser import BrowserAutomation


async def check():
    root = Path(__file__).resolve().parents[1] / "build"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="boss-check-", dir=root) as directory:
        assert Path(directory).resolve().parent == root.resolve()
        browser = BrowserAutomation(Path(directory), lambda *args: None)
        try:
            result = await browser.open_login("boss")
            page = browser._pages["boss"]
            content = await page.locator("body").inner_text(timeout=5000)
            login_links = []
            for link in await page.locator("a, button").all():
                text = (await link.inner_text()).strip()
                if "登录" in text and len(text) < 35:
                    login_links.append({"text": text, "class": await link.get_attribute("class"),
                                        "path": urlsplit(await link.get_attribute("href") or "").path})
            observations = []
            for _ in range(5):
                await asyncio.sleep(4)
                result = await browser.status("boss")
                parts = urlsplit(page.url)
                observations.append({"page": parts.hostname + parts.path if parts.hostname else parts.path,
                                     "state": result["state"], "loop_detected": browser._unstable(page)})
                if result["state"] in {"verification_required", "browser_blocked"}:
                    break
            print(json.dumps({"driver": "patchright", "title": await page.title(), "body_length": len(content),
                              "login_controls": login_links, "observations": observations}, ensure_ascii=True), flush=True)
            return 1 if any(item["loop_detected"] for item in observations) else 0
        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
