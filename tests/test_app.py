"""Observable API/worker invariants; all browser operations are local fakes."""

import asyncio
import base64
import copy
import csv
import io
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from jobs_app.main import create_app
from jobs_app.storage import now


HEADERS = {"X-Jobs-Client": "local-ui"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def job(identity="1", company="示例科技", title="Python 工程师"):
    return {"external_id": identity, "title": title, "company": company, "city": "北京",
            "salary": "15-25K·14薪", "description": "Python 后端开发", "hr_active": "今日活跃",
            "url": f"https://www.zhipin.com/job_detail/{identity}.html"}


class FakeBrowser:
    def __init__(self):
        self.jobs = [job()]
        self.sent = []
        self.apply_error = None
        self.block_search = False
        self.entered = threading.Event()
        self.closed = False
        self.rejected = []

    async def search(self, platform, config, stop):
        self.entered.set()
        while self.block_search and not stop():
            await asyncio.sleep(0.01)
        for item in self.jobs:
            if stop():
                return
            yield copy.deepcopy(item)

    async def apply(self, item, greeting, image_path=None):
        self.sent.append((copy.deepcopy(item), greeting, image_path))
        if self.apply_error:
            raise self.apply_error
        return {"status": "submitted", "detail": "模拟平台明确确认申请成功"}

    async def rejected_companies(self, platform):
        return self.rejected

    async def status(self, platform):
        return {"platform": platform, "name": "模拟平台", "state": "logged_in", "logged_in": True, "detail": "模拟已登录"}

    async def open_login(self, platform):
        return await self.status(platform)

    async def logout(self, platform):
        return {"platform": platform, "name": "模拟平台", "state": "closed", "logged_in": False, "detail": "模拟已退出"}

    async def close(self):
        self.closed = True


async def no_delay(self, seconds):
    await asyncio.sleep(0)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.delay = patch("jobs_app.engine.TaskRunner._delay", no_delay)
        self.delay.start()
        self.addCleanup(self.delay.stop)
        self.browser = FakeBrowser()
        self.open_app()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))

    def open_app(self):
        self.app = create_app(self.directory.name, browser=self.browser)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def start(self, mode="preview", platform="boss"):
        response = self.client.post("/api/tasks", json={"platform": platform, "mode": mode}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def finished(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.client.get("/api/tasks/current").json()
            if state and state["status"] not in ("running", "stopping"):
                # Wait for the final log/notification tail before the next run.
                self.app.state.runner.future.result(timeout=3)
                return state
            time.sleep(0.01)
        self.fail("Fake browser task did not complete")

    def save(self, settings):
        response = self.client.put("/api/settings", json=settings, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_preview_then_delivery_and_persistent_deduplication(self):
        self.browser.jobs = [job(), job(), job("2", "另一家公司")]
        self.start()
        preview = self.finished()
        self.assertEqual((preview["processed"], preview["preview"]), (2, 2))
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.app.state.store.attempts_today("boss"), 0)
        self.start("deliver")
        delivered = self.finished()
        self.assertEqual(delivered["submitted"], 2)
        self.assertEqual(len(self.browser.sent), 2)
        self.assertIn("Python 工程师", self.browser.sent[0][1])
        self.assertNotIn("{title}", self.browser.sent[0][1])
        self.assertEqual({item["status"] for item in self.client.get("/api/jobs").json()["items"]}, {"submitted"})
        self.client.__exit__(None, None, None)
        self.browser = FakeBrowser()
        self.browser.jobs = [job(), job("2")]
        self.open_app()
        self.start("deliver")
        repeat = self.finished()
        self.assertEqual(repeat["submitted"], 0)
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.client.get("/api/dashboard").json()["today"]["submitted"], 2)

    def test_uncertain_click_stops_subsequent_jobs_and_blocks_repeat(self):
        self.browser.jobs = [job(), job("2")]
        self.browser.apply_error = TimeoutError("平台点击后的响应超时")
        self.start("deliver")
        state = self.finished()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(self.browser.sent), 1)
        items = self.client.get("/api/jobs").json()["items"]
        self.assertEqual(items[0]["status"], "unconfirmed")
        self.browser.jobs = [job()]
        self.browser.apply_error = None
        self.start("deliver")
        self.finished()
        self.assertEqual(len(self.browser.sent), 1)
        self.assertEqual(self.client.get("/api/jobs").json()["items"][0]["status"], "unconfirmed")

    def test_stop_prevents_delivery_and_blocks_conflicting_mutations(self):
        self.browser.block_search = True
        self.start("deliver")
        self.assertTrue(self.browser.entered.wait(1))
        self.assertEqual(self.client.post("/api/tasks", json={"platform": "liepin"}, headers=HEADERS).status_code, 409)
        self.assertEqual(self.client.put("/api/settings", json={"platforms": {"boss": {"daily_limit": 10}}}, headers=HEADERS).status_code, 409)
        self.assertEqual(self.client.post("/api/resume", files={"file": ("cv.png", PNG, "image/png")}, headers=HEADERS).status_code, 409)
        self.assertEqual(self.client.post("/api/tasks/stop", headers=HEADERS).status_code, 200)
        self.assertEqual(self.finished()["status"], "stopped")
        self.assertEqual(self.browser.sent, [])
        self.browser.block_search = False
        self.start()
        self.assertEqual(self.finished()["preview"], 1)

    def test_daily_limit_and_crash_recovery_keep_delivery_ledger(self):
        self.save({"platforms": {"boss": {"daily_limit": 1}}})
        self.browser.jobs = [job(), job("2")]
        self.start("deliver")
        self.assertEqual(self.finished()["submitted"], 1)
        self.assertEqual(len(self.browser.sent), 1)
        self.assertEqual(self.client.post("/api/tasks", json={"platform": "boss", "mode": "deliver"}, headers=HEADERS).status_code, 409)
        item = job("crashed") | {"platform": "liepin"}
        self.app.state.store.save_job(item, "submitting")
        self.app.state.store.claim_delivery(item)
        interrupted = {"id": "crash-task", "platform": "liepin", "mode": "deliver", "status": "running", "started_at": now()}
        self.app.state.store.save_task(interrupted)
        self.client.__exit__(None, None, None)
        self.browser = FakeBrowser()
        self.browser.jobs = [item]
        self.open_app()
        self.assertEqual(self.client.get("/api/tasks/current").json()["status"], "interrupted")
        self.assertEqual(self.client.get("/api/jobs?platform=liepin").json()["items"][0]["status"], "unconfirmed")
        self.start("deliver", "liepin")
        self.finished()
        self.assertEqual(self.browser.sent, [])

    def test_settings_mask_preserve_clear_and_encrypt_secrets(self):
        secret = "test-private-api-value"
        webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-private-webhook"
        public = self.save({"ai": {"api_key": secret, "profile": "我熟悉 Python"}, "notifications": {"webhook": webhook}})
        self.assertEqual(public["ai"]["api_key"], "")
        self.assertTrue(public["ai"]["api_key_set"])
        self.assertTrue(public["notifications"]["webhook_set"])
        self.assertNotIn(secret, self.client.get("/api/settings").text)
        self.assertNotIn(webhook, self.client.get("/api/settings").text)
        self.save(public)
        self.assertEqual(self.app.state.store.settings()["ai"]["api_key"], secret)
        with self.app.state.store.connect() as db:
            saved = db.execute("SELECT value FROM settings").fetchone()[0]
        if os.name == "nt":
            self.assertNotIn(secret, saved)
            self.assertNotIn(webhook, saved)
            self.assertTrue(json.loads(saved)["ai"]["api_key"].startswith("dpapi:"))
        cleared = self.save({"ai": {"clear_api_key": True}, "notifications": {"clear_webhook": True}})
        self.assertFalse(cleared["ai"]["api_key_set"])
        self.assertFalse(cleared["notifications"]["webhook_set"])
        self.assertEqual(self.app.state.store.settings()["ai"]["api_key"], "")

    def test_invalid_settings_are_atomic_and_never_echo_secrets(self):
        self.save({"platforms": {"boss": {"daily_limit": 17}}})
        invalid = [
            {"platforms": {"boss": {"salary_min": 20000, "salary_max": 10000}}},
            {"platforms": {"boss": {"keywords": ["   "]}}},
            {"platforms": {"boss": {"delay_min": 1}}},
            {"platforms": {"boss": {"unknown_filter": True}}},
            {"ai": {"base_url": "http://remote.example/v1"}},
            {"ai": {"base_url": "https://example.com:invalid/v1"}},
            {"notifications": {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="}},
            {"notifications": {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=a&key=b"}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.client.put("/api/settings", json=payload, headers=HEADERS)
                self.assertEqual(response.status_code, 422, response.text)
        response = self.client.put("/api/settings", json={"platforms": {"boss": {"daily_limit": 19}}, "ai": {"api_key": "private-secret", "min_score": 999}}, headers=HEADERS)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-secret", response.text)
        self.assertEqual(self.client.get("/api/settings").json()["platforms"]["boss"]["daily_limit"], 17)

    def test_ai_failure_never_delivers_and_redacts_saved_diagnostics(self):
        self.save({"ai": {"enabled": True, "base_url": "http://127.0.0.1:8001/v1", "profile": "熟悉 Python"},
                   "platforms": {"boss": {"use_ai": True}}})
        with patch("jobs_app.engine.match_job", new=AsyncMock(side_effect=RuntimeError("provider failed token=private-provider-token"))):
            self.start("deliver")
            self.assertEqual(self.finished()["status"], "failed")
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.app.state.store.attempts_today("boss"), 0)
        self.assertEqual(self.client.get("/api/jobs").json()["items"][0]["status"], "failed")
        for path in ("/api/jobs", "/api/tasks/current", "/api/logs"):
            with self.subTest(path=path):
                self.assertNotIn("private-provider-token", self.client.get(path).text)

    def test_blacklist_filter_search_export_and_csv_formula_escaping(self):
        self.browser.jobs = [job("blocked", "Acme 科技"), job("safe", "数据公司", "=2+2")]
        for company in (" A C M E ", "acme"):
            response = self.client.post("/api/blacklist", json={"company": company}, headers=HEADERS)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.client.get("/api/blacklist").json()["items"]), 1)
        self.start()
        state = self.finished()
        self.assertEqual((state["preview"], state["filtered"]), (1, 1))
        self.assertEqual(self.client.get("/api/jobs?q=数据").json()["total"], 1)
        self.assertEqual(self.client.get("/api/jobs?q=%27%20OR%201%3D1--").json()["total"], 0)
        response = self.client.get("/api/jobs/export?status=preview")
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], "'=2+2")
        safe = self.client.get("/api/jobs?status=preview").json()["items"][0]
        self.assertEqual(self.client.post(f"/api/jobs/{safe['id']}/blacklist", headers=HEADERS).status_code, 200)
        entries = self.client.get("/api/blacklist").json()["items"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(self.client.delete(f"/api/blacklist/{entries[0]['id']}", headers=HEADERS).status_code, 200)
        self.assertEqual(self.client.delete("/api/blacklist/99999", headers=HEADERS).status_code, 404)

    def test_upload_boundaries_and_local_request_protection(self):
        self.assertEqual(self.client.post("/api/tasks/stop").status_code, 403)
        self.assertEqual(self.client.post("/api/tasks/stop", headers=HEADERS | {"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/tasks/stop", headers=HEADERS | {"Origin": "http://testserver"}).status_code, 200)
        self.assertEqual(self.client.get("/api/settings").headers["Cache-Control"], "no-store")
        response = self.client.post("/api/resume", files={"file": ("../../outside.png", PNG, "image/png")}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["filename"], "resume.png")
        self.assertEqual((Path(self.directory.name) / "resume" / "resume.png").read_bytes(), PNG)
        self.assertEqual(self.client.post("/api/resume", files={"file": ("fake.png", b"<script>bad</script>", "image/png")}, headers=HEADERS).status_code, 415)
        oversized = b"\x89PNG\r\n\x1a\n" + b"x" * (10 * 1024 * 1024)
        self.assertEqual(self.client.post("/api/resume", files={"file": ("large.png", oversized, "image/png")}, headers=HEADERS).status_code, 413)
        self.assertEqual((Path(self.directory.name) / "resume" / "resume.png").read_bytes(), PNG)
        self.assertEqual(self.client.delete("/api/resume", headers=HEADERS).status_code, 200)
        self.assertFalse((Path(self.directory.name) / "resume" / "resume.png").exists())
        self.assertFalse(self.client.get("/api/settings").json()["resume"]["has_image"])

    def test_task_start_during_upload_read_preserves_previous_resume(self):
        response = self.client.post("/api/resume", files={"file": ("original.png", PNG, "image/png")}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        before = self.client.get("/api/settings").json()["resume"]
        entered, release = threading.Event(), threading.Event()
        original_read = UploadFile.read

        async def delayed_read(upload, size=-1):
            if upload.filename == "replacement.png":
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            return await original_read(upload, size)

        self.browser.block_search = True
        with patch.object(UploadFile, "read", delayed_read), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.client.post, "/api/resume",
                                  files={"file": ("replacement.png", PNG + b"new-image", "image/png")}, headers=HEADERS)
            try:
                self.assertTrue(entered.wait(2), "Replacement upload did not reach its awaited read")
                self.start("deliver")
                self.assertTrue(self.browser.entered.wait(1))
                release.set()
                response = pending.result(timeout=3)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual((Path(self.directory.name) / "resume" / "resume.png").read_bytes(), PNG)
                self.assertEqual(self.client.get("/api/settings").json()["resume"], before)
            finally:
                release.set()
                self.client.post("/api/tasks/stop", headers=HEADERS)
        self.assertEqual(self.finished()["status"], "stopped")
        self.assertEqual(self.browser.sent, [])


if __name__ == "__main__":
    unittest.main()
