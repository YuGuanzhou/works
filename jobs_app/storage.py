import base64
import ctypes
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import defaults


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_diagnostic(message):
    message = re.sub(r"https?://\S+", "[地址已隐藏]", str(message))
    message = re.sub(r"(?i)(api[_-]?key|token|authorization|cookie|key)\s*[:=]\s*\S+", r"\1=[已隐藏]", message)
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[密钥已隐藏]", message)


def protect(value: str, decrypt=False) -> str:
    """Use the signed-in Windows user's DPAPI; Unix relies on owner-only data files."""
    if not value or os.name != "nt":
        return value
    if decrypt and not value.startswith("dpapi:"):
        raise ValueError("无法读取凭据：格式不受支持")
    raw = base64.b64decode(value[6:]) if decrypt else value.encode("utf-8")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ValueError("Windows 凭据加密失败，请使用保存配置时的 Windows 账户")
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode("utf-8") if decrypt else "dpapi:" + base64.b64encode(result).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.data_dir.chmod(0o700)
        self.path = self.data_dir / "jobs.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL,
                    title TEXT NOT NULL, company TEXT NOT NULL, city TEXT NOT NULL DEFAULT '',
                    salary TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', score INTEGER, greeting TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(platform, external_id));
                CREATE TABLE IF NOT EXISTS delivery_ledger (
                    platform TEXT NOT NULL, external_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL, PRIMARY KEY(platform,external_id));
                CREATE TABLE IF NOT EXISTS blacklist (
                    id INTEGER PRIMARY KEY, company TEXT NOT NULL, normalized TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'all', reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    UNIQUE(normalized,platform));
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, level TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '', message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(platform,status,updated_at);
                CREATE INDEX IF NOT EXISTS logs_time ON logs(created_at);
            """)
            if not db.execute("SELECT 1 FROM settings WHERE id=1").fetchone():
                db.execute("INSERT INTO settings VALUES (1,?)", (json.dumps(defaults(), ensure_ascii=False),))
            db.execute("UPDATE jobs SET status='unconfirmed', reason='上次运行在投递中断开，请先到平台核实，程序不会自动重投' WHERE status='submitting'")
            db.execute("UPDATE delivery_ledger SET status='unconfirmed' WHERE status='submitting'")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self, public=False):
        with self.connect() as db:
            data = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        for group, key in [("ai", "api_key"), ("notifications", "webhook")]:
            if public:
                data[group][key + "_set"] = bool(data[group][key])
                data[group][key] = ""
            else:
                data[group][key] = protect(data[group][key], decrypt=True)
        return data

    def save_settings(self, data):
        encoded = json.loads(json.dumps(data))
        for group, key in [("ai", "api_key"), ("notifications", "webhook")]:
            encoded[group][key] = protect(encoded[group][key])
        with self.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(encoded, ensure_ascii=False),))

    def log(self, level, message, platform=""):
        # Strip tokens/URLs from diagnostics before persisting or streaming them.
        message = safe_diagnostic(message)
        with self.connect() as db:
            cur = db.execute("INSERT INTO logs(created_at,level,platform,message) VALUES(?,?,?,?)", (now(), level.upper(), platform, message[:3000]))
            return cur.lastrowid

    def logs(self, after=0, level="", platform=""):
        where, values = ["id > ?"], [after]
        if level:
            where.append("level = ?")
            values.append(level.upper())
        if platform:
            where.append("platform = ?")
            values.append(platform)
        with self.connect() as db:
            order = "DESC" if after == 0 else "ASC"
            rows = [dict(row) for row in db.execute(f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY id {order} LIMIT 500", values)]
            return rows[::-1] if after == 0 else rows

    def blacklist(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,company,platform,reason,created_at FROM blacklist ORDER BY id DESC")]

    def add_blacklist(self, company, reason="", platform="all"):
        normalized = re.sub(r"\s+", "", company).casefold()
        with self.connect() as db:
            db.execute("INSERT INTO blacklist(company,normalized,reason,platform,created_at) VALUES(?,?,?,?,?) ON CONFLICT(normalized,platform) DO UPDATE SET reason=excluded.reason", (company.strip(), normalized, reason, platform, now()))

    def remove_blacklist(self, item_id):
        with self.connect() as db:
            return db.execute("DELETE FROM blacklist WHERE id=?", (item_id,)).rowcount

    def save_job(self, job, status, reason="", score=None, greeting=""):
        with self.connect() as db:
            db.execute("""INSERT INTO jobs(platform,external_id,title,company,city,salary,url,description,status,reason,score,greeting,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,external_id) DO UPDATE SET
                title=excluded.title,company=excluded.company,city=excluded.city,salary=excluded.salary,url=excluded.url,
                description=excluded.description,status=excluded.status,reason=excluded.reason,score=excluded.score,greeting=excluded.greeting,updated_at=excluded.updated_at""",
                (job["platform"], job["external_id"], job["title"], job["company"], job.get("city", ""), job.get("salary", ""), job.get("url", ""), job.get("description", ""), status, reason, score, greeting, now(), now()))
            return db.execute("SELECT id FROM jobs WHERE platform=? AND external_id=?", (job["platform"], job["external_id"])).fetchone()[0]

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def jobs(self, platform="", status="", q="", page=1, page_size=20):
        where, values = ["1=1"], []
        for key, value in [("platform", platform), ("status", status)]:
            if value:
                where.append(f"{key}=?")
                values.append(value)
        if q:
            where.append("(instr(lower(title), lower(?)) > 0 OR instr(lower(company), lower(?)) > 0)")
            values.extend([q, q])
        clause = " AND ".join(where)
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) FROM jobs WHERE {clause}", values).fetchone()[0]
            rows = db.execute(f"SELECT * FROM jobs WHERE {clause} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?", [*values, page_size, (page-1)*page_size])
            return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def attempted(self, platform, external_id):
        with self.connect() as db:
            row = db.execute("SELECT status FROM delivery_ledger WHERE platform=? AND external_id=?", (platform, external_id)).fetchone()
            return row[0] if row else None

    def claim_delivery(self, job):
        with self.connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO delivery_ledger VALUES(?,?,?,?)", (job["platform"], job["external_id"], now(), "submitting"))
            return cur.rowcount == 1

    def finish_delivery(self, job, status):
        with self.connect() as db:
            db.execute("UPDATE delivery_ledger SET status=? WHERE platform=? AND external_id=?", (status, job["platform"], job["external_id"]))

    def attempts_today(self, platform):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM delivery_ledger WHERE platform=? AND substr(created_at,1,10)=?", (platform, now()[:10])).fetchone()[0]

    def save_task(self, task):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?)", (task["id"], json.dumps(task, ensure_ascii=False)))

    def latest_task(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM tasks ORDER BY rowid DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else None

    def stats(self):
        days = [(datetime.now().date()-timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        trend = {day: {"date": day, "submitted": 0, "preview": 0, "filtered": 0, "failed": 0} for day in days}
        with self.connect() as db:
            # Job observations count once; verified submissions use the immutable attempt date.
            rows = db.execute("SELECT substr(updated_at,1,10) day,status,count(*) n FROM jobs WHERE status!='submitted' GROUP BY day,status")
            for row in rows:
                key = "failed" if row["status"] in {"unconfirmed", "failed", "submitting"} else "filtered" if row["status"] == "already_contacted" else row["status"]
                if row["day"] in trend and key in trend[row["day"]]:
                    trend[row["day"]][key] += row["n"]
            for row in db.execute("SELECT substr(created_at,1,10) day,count(*) n FROM delivery_ledger WHERE status='submitted' GROUP BY day"):
                if row["day"] in trend:
                    trend[row["day"]]["submitted"] = row["n"]
            total = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        return {"today": {k:v for k,v in trend[days[-1]].items() if k != "date"}, "trend": list(trend.values()), "total_jobs": total}
