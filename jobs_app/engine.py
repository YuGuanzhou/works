import asyncio
import concurrent.futures
import copy
import os
import random
import threading
import uuid
from urllib.parse import urlsplit

from .browser import BrowserAutomation
from .config import PLATFORM_NAMES
from .matching import filter_job, match_job, notify
from .storage import now, safe_diagnostic


def safe_error(exc):
    return safe_diagnostic(exc)[:800] or type(exc).__name__


class TaskRunner:
    """A single worker owns Playwright; SQLite remains usable from the HTTP thread."""
    def __init__(self, store, browser=None):
        self.store = store
        self.browser = browser or BrowserAutomation(store.data_dir, store.log)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.session_busy = False
        self.task = store.latest_task()
        if self.task and self.task["status"] in {"running", "stopping"}:
            self.task.update(status="interrupted", message="程序上次退出时任务中断，未自动恢复投递", finished_at=now())
            store.save_task(self.task)
        self.sessions = {key: {"status": "closed", "message": "点击登录，在浏览器内完成扫码；已有登录信息将自动复用"} for key in PLATFORM_NAMES}
        self.thread = threading.Thread(target=self._worker, name="jobs-browser", daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("浏览器工作线程启动超时")

    def _worker(self):
        # Windows Playwright starts a subprocess and therefore needs a Proactor loop.
        self.loop = asyncio.ProactorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.task)

    def running(self):
        task = self.snapshot()
        return bool(task and task["status"] in {"running", "stopping"})

    async def session(self, platform, action):
        with self.lock:
            if action != "status":
                if self.running() or self.session_busy:
                    raise ValueError("请先等待当前任务或登录操作结束")
                self.session_busy = True
        try:
            method = {"login": self.browser.open_login, "logout": self.browser.logout, "status": self.browser.status}[action]
            result = await method(platform)
            result = result | {"status": result.get("status", result.get("state", "unknown")),
                               "message": result.get("message", result.get("detail", ""))}
            with self.lock:
                self.sessions[platform] = result
            return result
        finally:
            if action != "status":
                with self.lock:
                    self.session_busy = False

    def update(self, **values):
        with self.lock:
            self.task.update(values)
            self.store.save_task(self.task)

    def increment(self, key):
        with self.lock:
            self.task[key] += 1
            self.store.save_task(self.task)

    def start(self, platform, mode):
        with self.lock:
            if self.running() or self.session_busy:
                raise ValueError("已有任务运行中，请先停止或等待完成")
            settings = self.store.settings()
            config = settings["platforms"][platform]
            if config["use_ai"]:
                ai = settings["ai"]
                local_model = urlsplit(ai["base_url"]).hostname in {"localhost", "127.0.0.1", "::1"}
                if not ai["enabled"] or (not local_model and not ai["api_key"]) or not ai["profile"].strip():
                    raise ValueError("请先启用 AI 并保存 API Key 和个人经历，再开启平台 AI 筛选")
            if mode == "deliver" and config["send_image"]:
                if platform != "boss":
                    raise ValueError("图片简历目前仅支持 BOSS 直聘")
                if not settings["resume"]["has_image"]:
                    raise ValueError("请先上传图片简历")
            if mode == "deliver" and self.store.attempts_today(platform) >= config["daily_limit"]:
                raise ValueError("今天已达到本地每日投递次数上限")
            self.stop_event.clear()
            self.task = dict(id=uuid.uuid4().hex, platform=platform, mode=mode, status="running", processed=0,
                             submitted=0, filtered=0, failed=0, preview=0, message="正在打开平台并检查登录状态", started_at=now())
            self.store.save_task(self.task)
            self.future = self.submit(self._run(platform, mode, settings))
            return self.snapshot()

    def stop(self):
        with self.lock:
            if self.running():
                self.stop_event.set()
                self.update(status="stopping", message="正在停止，将在当前页面操作结束后退出")
        return self.snapshot()

    async def _delay(self, seconds):
        remaining = seconds
        while remaining > 0 and not self.stop_event.is_set():
            step = min(remaining, 0.25)
            await asyncio.sleep(step)
            remaining -= step

    async def _run(self, platform, mode, settings):
        config = settings["platforms"][platform]
        self.store.log("INFO", f"{PLATFORM_NAMES[platform]}：开始{'仅预览' if mode == 'preview' else '真实投递'}任务", platform)
        limited = False
        try:
            seen = set()
            async for job in self.browser.search(platform, config, self.stop_event.is_set):
                if self.stop_event.is_set():
                    break
                job["platform"] = platform
                identity = job["external_id"]
                if identity in seen:
                    continue
                seen.add(identity)
                self.increment("processed")
                self.update(message=f"正在处理：{job['title']} · {job['company']}")
                if self.store.attempted(platform, identity):
                    self.increment("filtered")
                    self.store.log("INFO", f"跳过已有投递尝试的职位：{job['title']}（包括待核实记录）", platform)
                    continue
                reason = filter_job(job, config, self.store.blacklist())
                if reason:
                    self.store.save_job(job, "filtered", reason)
                    self.increment("filtered")
                    continue
                score = None
                greeting = config["greeting"].replace("{title}", job["title"]).replace("{company}", job["company"])
                if config["use_ai"]:
                    try:
                        if not job.get("description", "").strip():
                            raise ValueError("职位详情未读取到，无法进行可靠的 AI 匹配")
                        match = await match_job(job, settings["ai"])
                        score, greeting = match["score"], match["greeting"]
                        if score < settings["ai"]["min_score"]:
                            self.store.save_job(job, "filtered", match["reason"], score, greeting)
                            self.increment("filtered")
                            continue
                    except Exception as exc:
                        self.store.save_job(job, "failed", "AI 匹配失败，未进行投递：" + safe_error(exc))
                        self.increment("failed")
                        self.store.log("ERROR", "AI 匹配失败，任务已停止，请检查配置：" + safe_error(exc), platform)
                        raise RuntimeError("AI 匹配失败，未继续投递") from None
                if self.stop_event.is_set():
                    break
                if mode == "preview":
                    self.store.save_job(job, "preview", "仅预览，尚未向招聘方发送消息", score, greeting)
                    self.increment("preview")
                else:
                    if self.store.attempts_today(platform) >= config["daily_limit"]:
                        limited = True
                        break
                    self.store.save_job(job, "submitting", "已开始投递，正在等待平台结果", score, greeting)
                    if not self.store.claim_delivery(job):
                        continue
                    try:
                        resume = settings["resume"]
                        image = str(self.store.data_dir / "resume" / resume["filename"]) if config["send_image"] else None
                        result = await self.browser.apply(job, greeting, image)
                        status = result.get("status", "unconfirmed")
                        if status not in {"submitted", "already_contacted", "unconfirmed", "failed"}:
                            status = "unconfirmed"
                        detail = result.get("detail", "")
                    except Exception as exc:
                        # A timeout after a click is ambiguous; never blindly resend it.
                        status, detail = "unconfirmed", "请在平台核实投递结果：" + safe_error(exc)
                    self.store.finish_delivery(job, status)
                    self.store.save_job(job, status, detail, score, greeting)
                    self.increment("submitted" if status == "submitted" else "filtered" if status == "already_contacted" else "failed")
                    self.store.log("INFO" if status in {"submitted", "already_contacted"} else "WARNING", f"{job['title']}：{detail or status}", platform)
                    if status in {"unconfirmed", "failed"}:
                        self.update(message="平台未确认成功，任务停止，请核实记录后再继续")
                        raise RuntimeError("投递未获明确成功确认，已停止后续投递；该职位不会自动重投")
                await self._delay(random.uniform(config["delay_min"], config["delay_max"]))
            if mode == "deliver" and platform == "boss" and not self.stop_event.is_set():
                try:
                    for company in await self.browser.rejected_companies(platform):
                        self.store.add_blacklist(company, "平台会话中发现不合适反馈", platform)
                except Exception as exc:
                    self.store.log("WARNING", "自动同步黑名单未完成：" + safe_error(exc), platform)
            stopped = self.stop_event.is_set()
            self.update(status="stopped" if stopped else "completed", finished_at=now(),
                        message="任务已停止" if stopped else "达到每日投递上限" if limited else "本次搜索已完成")
        except Exception as exc:
            self.update(status="stopped" if self.stop_event.is_set() else "failed", message=safe_error(exc), finished_at=now())
            self.store.log("ERROR", safe_error(exc), platform)
        finally:
            task = self.snapshot()
            self.store.log("INFO", f"任务结束：已处理 {task['processed']}，已投递 {task['submitted']}，预览 {task['preview']}，过滤 {task['filtered']}，异常 {task['failed']}", platform)
            notice = settings["notifications"]
            if mode == "deliver" and notice["enabled"] and notice["webhook"]:
                try:
                    await notify(notice["webhook"], f"工作无忧 · {PLATFORM_NAMES[platform]}\n{task['message']}\n已投递 {task['submitted']}，过滤 {task['filtered']}，异常 {task['failed']}")
                except Exception as exc:
                    self.store.log("WARNING", "投递结束通知发送失败：" + safe_error(exc), platform)

    def close(self):
        self.stop_event.set()
        if getattr(self, "future", None):
            try:
                self.future.result(timeout=5)
            except Exception:
                pass
        try:
            self.submit(self.browser.close()).result(timeout=10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
