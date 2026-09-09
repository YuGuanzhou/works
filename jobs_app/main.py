import asyncio
import csv
import io
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import AIConfig, NotificationConfig, PLATFORM_NAMES, Platform, PlatformConfig
from .engine import TaskRunner, safe_error
from .matching import match_job
from .storage import Store


class TaskRequest(BaseModel):
    platform: Platform
    mode: Literal["preview", "deliver"] = "preview"


class BlacklistRequest(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    reason: str = Field("", max_length=1000)
    platform: Literal["all", "boss", "liepin", "51job", "zhilian"] = "all"


class AITestRequest(BaseModel):
    job_description: str = Field(min_length=1, max_length=30000)


def create_app(data_dir=None, browser=None):
    data_path = Path(data_dir or os.getenv("JOBS_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    store = Store(data_path)
    settings_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(app):
        app.state.runner = TaskRunner(store, browser)
        store.log("INFO", f"工作无忧 Python {__version__} 已启动")
        yield
        await asyncio.to_thread(app.state.runner.close)

    app = FastAPI(title="工作无忧 Python", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("X-Jobs-Client") != "local-ui":
                return JSONResponse({"detail": "请从本地管理界面操作，缺少 X-Jobs-Client 请求头"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "不允许跨站操作本地应用"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def idle():
        if app.state.runner.running():
            raise HTTPException(409, "请先停止任务，再修改配置、简历或登录状态")

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "app": "get-jobs-python"}

    @app.get("/api/settings")
    def settings():
        return store.settings(public=True)

    @app.put("/api/settings")
    def save_settings(payload: dict):
        idle()
        if set(payload) - {"platforms", "ai", "notifications", "resume"}:
            raise HTTPException(422, "配置包含未知分组")
        try:
            with app.state.runner.lock, settings_lock:
                idle()
                current = store.settings()
                for platform, config in payload.get("platforms", {}).items():
                    if platform not in PLATFORM_NAMES:
                        raise ValueError("未知招聘平台")
                    current["platforms"][platform] = PlatformConfig(**(current["platforms"][platform] | config)).model_dump()
                for group, model, secret in [("ai", AIConfig, "api_key"), ("notifications", NotificationConfig, "webhook")]:
                    if group not in payload:
                        continue
                    updates = dict(payload[group])
                    updates.pop(secret + "_set", None)
                    clear = updates.pop("clear_" + secret, False)
                    if not updates.get(secret):
                        updates.pop(secret, None)
                    if clear:
                        updates[secret] = ""
                    current[group] = model(**(current[group] | updates)).model_dump()
                # Resume metadata is derived from the upload; ignore echoed display fields.
                store.save_settings(current)
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            message = "; ".join(error["msg"] for error in exc.errors()) if isinstance(exc, ValidationError) else safe_error(exc)
            raise HTTPException(422, message) from None
        store.log("INFO", "配置已保存")
        return store.settings(public=True)

    @app.get("/api/dashboard")
    def dashboard():
        runner = app.state.runner
        task = runner.snapshot()
        running = bool(task and task["status"] in {"running", "stopping"})
        return store.stats() | {"task": task, "platforms": [{"id": key, "name": name, "session": runner.sessions[key],
            "running": bool(running and task["platform"] == key)} for key, name in PLATFORM_NAMES.items()]}

    async def platform_action(platform, action):
        if action != "status":
            idle()
        try:
            result = await asyncio.wrap_future(app.state.runner.submit(app.state.runner.session(platform, action)))
            return result
        except Exception as exc:
            store.log("ERROR", safe_error(exc), platform)
            raise HTTPException(400, safe_error(exc)) from None

    @app.post("/api/platforms/{platform}/login")
    async def login(platform: Platform):
        return await platform_action(platform, "login")

    @app.post("/api/platforms/{platform}/logout")
    async def logout(platform: Platform):
        return await platform_action(platform, "logout")

    @app.get("/api/platforms/{platform}/status")
    async def login_status(platform: Platform):
        return await platform_action(platform, "status")

    @app.post("/api/tasks")
    def start_task(payload: TaskRequest):
        try:
            return app.state.runner.start(payload.platform, payload.mode)
        except ValueError as exc:
            raise HTTPException(409, safe_error(exc)) from None

    @app.post("/api/tasks/stop")
    def stop_task():
        return app.state.runner.stop()

    @app.get("/api/tasks/current")
    def current_task():
        return app.state.runner.snapshot()

    @app.get("/api/jobs/export")
    def export_jobs(platform: str = "", status: str = "", q: str = ""):
        def generate():
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(["平台", "职位", "公司", "城市", "薪资", "状态", "匹配分", "招呼语", "说明", "链接", "更新时间"])
            yield "\ufeff" + buffer.getvalue()
            page = 1
            while True:
                rows = store.jobs(platform, status, q, page, 500)["items"]
                if not rows:
                    break
                buffer.seek(0)
                buffer.truncate(0)
                for row in rows:
                    values = [row[key] for key in ["platform", "title", "company", "city", "salary", "status", "score", "greeting", "reason", "url", "updated_at"]]
                    # Prevent spreadsheet formula execution from scraped company/job text.
                    writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values])
                yield buffer.getvalue()
                page += 1
        return StreamingResponse(generate(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="jobs.csv"'})

    @app.get("/api/jobs")
    def jobs(platform: str = "", status: str = "", q: str = Query("", max_length=200), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return store.jobs(platform, status, q, page, page_size)

    @app.post("/api/jobs/{job_id}/blacklist")
    def blacklist_job(job_id: int):
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, "职位不存在")
        if not job["company"].strip():
            raise HTTPException(422, "该职位没有可加入黑名单的公司名称")
        store.add_blacklist(job["company"], "从投递记录手动加入", job["platform"])
        return {"ok": True}

    @app.get("/api/blacklist")
    def blacklist():
        return {"items": store.blacklist()}

    @app.post("/api/blacklist")
    def add_blacklist(payload: BlacklistRequest):
        if not payload.company.strip():
            raise HTTPException(422, "公司名称不能为空")
        store.add_blacklist(payload.company, payload.reason, payload.platform)
        return {"items": store.blacklist()}

    @app.delete("/api/blacklist/{item_id}")
    def remove_blacklist(item_id: int):
        if not store.remove_blacklist(item_id):
            raise HTTPException(404, "黑名单条目不存在")
        return {"ok": True}

    @app.get("/api/logs")
    def logs(after: int = Query(0, ge=0), level: str = "", platform: str = ""):
        return {"items": store.logs(after, level, platform)}

    @app.get("/api/events")
    async def events(request: Request, after: int = Query(0, ge=0)):
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "Last-Event-ID 必须是整数") from None
        async def stream():
            last = cursor
            while not await request.is_disconnected():
                rows = store.logs(last)
                for row in rows:
                    last = row["id"]
                    yield f"id: {last}\nevent: log\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/ai/test")
    async def ai_test(payload: AITestRequest):
        try:
            config = store.settings()["ai"]
            if not config["profile"].strip():
                raise ValueError("请先保存个人经历")
            return await match_job({"title": "手动测试岗位", "company": "", "description": payload.job_description}, config)
        except Exception as exc:
            raise HTTPException(400, safe_error(exc)) from None

    @app.post("/api/resume")
    async def upload_resume(file: UploadFile):
        idle()
        content = await file.read(10 * 1024 * 1024 + 1)
        await file.close()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "图片简历不能超过 10 MB")
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif content.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        else:
            raise HTTPException(415, "请上传 PNG 或 JPEG 图片简历")
        target = store.data_dir / "resume"
        target.mkdir(exist_ok=True)
        with app.state.runner.lock, settings_lock:
            idle()
            config = store.settings()
            old_name = config["resume"]["filename"]
            filename = "resume" + suffix
            temporary = target / (filename + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(target / filename)
            config["resume"] = {"filename": filename, "has_image": True}
            store.save_settings(config)
            if old_name and old_name != filename:
                (target / Path(old_name).name).unlink(missing_ok=True)
        return config["resume"]

    @app.delete("/api/resume")
    def delete_resume():
        idle()
        with app.state.runner.lock, settings_lock:
            idle()
            config = store.settings()
            if config["resume"]["filename"]:
                (store.data_dir / "resume" / Path(config["resume"]["filename"]).name).unlink(missing_ok=True)
            config["resume"] = {"filename": "", "has_image": False}
            store.save_settings(config)
        return config["resume"]

    static = Path(__file__).parent / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    return app
