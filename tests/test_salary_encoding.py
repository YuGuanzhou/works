"""Offline regression for BOSS font-encoded salaries and saved records."""

import csv
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from jobs_app import matching
from jobs_app.browser import BrowserAutomation
from jobs_app.main import create_app
from jobs_app.storage import Store


FONT_DIGITS = (
    "\ue031\ue032\ue033\ue034\ue035\ue036\ue037\ue038\ue039\ue03a",
    "\ue8f0\ue8f1\ue8f2\ue8f3\ue8f4\ue8f5\ue8f6\ue8f7\ue8f8\ue8f9",
)


@pytest.mark.parametrize("digits", FONT_DIGITS)
def test_salary_font_mapping_preserves_text_and_monthly_parsing(digits):
    assert matching.normalize_salary(digits) == "0123456789"
    assert matching.normalize_salary(None) == ""
    unchanged = " 面议 ５－８Ｋ/月 · 13薪 \ue030\ue03b\ue8ef\ue8fa\ue000 "
    assert matching.normalize_salary(unchanged) == unchanged
    cases = {
        "10-15K": (10000, 15000),
        "6-11K·13薪": (6000, 11000),
        "1.5-2万/月": (15000, 20000),
        "20-30万/年": (16666.67, 25000),
        "300-500元/天": (6525, 10875),
        "15K以上": (15000, None),
        "5千以下": (None, 5000),
        "100元/小时": (None, None),
        "13薪": (None, None),
        "面议": (None, None),
    }
    for plain, expected in cases.items():
        encoded = plain.translate(str.maketrans("0123456789", digits))
        assert matching.normalize_salary(encoded) == plain
        assert matching.parse_salary(encoded) == expected


@pytest.mark.parametrize("digits", FONT_DIGITS)
def test_boss_browser_and_storage_normalize_new_salaries(tmp_path, digits):
    encoded = "6-11K·13薪".translate(str.maketrans("0123456789", digits))
    raw = {
        "platform": "boss", "external_id": "salary-example", "title": "AI 工程师",
        "company": "示例科技", "salary": encoded,
        "url": "https://www.zhipin.com/job_detail/salary-example.html",
    }
    adapter = BrowserAutomation(tmp_path, lambda *args: None)
    normalized = adapter._normalize_job("boss", raw, "长沙")
    assert normalized["salary"] == "6-11K·13薪"
    assert (normalized["salary_min"], normalized["salary_max"]) == (6000, 11000)
    assert normalized["city"] == "长沙"

    store = Store(tmp_path)
    job_id = store.save_job(raw, "preview")
    assert store.job(job_id)["salary"] == "6-11K·13薪"
    assert raw["salary"] == encoded
    other_id = store.save_job(raw | {"platform": "liepin"}, "preview")
    assert store.job(other_id)["salary"] == encoded


def test_existing_salary_repair_preserves_records_and_exports(tmp_path):
    store = Store(tmp_path)
    examples = [
        ("boss", "\ue032\ue031-\ue032\ue036K", "10-15K"),
        ("boss", "\ue8f6-\ue8f1\ue8f1K·\ue8f1\ue8f3薪", "6-11K·13薪"),
        ("boss", " 15-25K·14薪 ", " 15-25K·14薪 "),
        ("liepin", "\ue032\ue031-\ue032\ue036K", "\ue032\ue031-\ue032\ue036K"),
    ]
    expected_salaries = {}
    for index, (platform, encoded, expected) in enumerate(examples):
        job = {
            "platform": platform, "external_id": str(index), "title": f"职位 {index}",
            "company": "公司\ue032", "city": "长沙", "salary": encoded,
            "url": f"https://example.test/job/{index}", "description": "职位描述\ue033",
        }
        job_id = store.save_job(job, "submitted", reason="保留说明", score=70, greeting="保留招呼语")
        assert store.claim_delivery(job)
        store.finish_delivery(job, "submitted")
        # Seed the exact pre-fix persisted values, bypassing save-time normalization.
        with store.connect() as db:
            db.execute(
                "UPDATE jobs SET salary=?, created_at=?, updated_at=? WHERE id=?",
                (encoded, "2026-09-01T09:00:00+08:00", "2026-09-02T10:00:00+08:00", job_id),
            )
            db.execute("UPDATE delivery_ledger SET created_at='2026-09-01T09:00:00+08:00'")
        expected_salaries[job_id] = expected

    def snapshot(current):
        with current.connect() as db:
            jobs = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")]
            ledger = [dict(row) for row in db.execute("SELECT * FROM delivery_ledger ORDER BY platform,external_id")]
        return jobs, ledger

    original_jobs, original_ledger = snapshot(store)
    expected_jobs = [row | {"salary": expected_salaries[row["id"]]} for row in original_jobs]
    reopened = Store(tmp_path)
    assert snapshot(reopened) == (expected_jobs, original_ledger)
    assert snapshot(Store(tmp_path)) == (expected_jobs, original_ledger)
    for row in expected_jobs:
        assert reopened.job(row["id"]) == row

    app = create_app(tmp_path, browser=SimpleNamespace(close=AsyncMock()))
    with TestClient(app) as client:
        response = client.get("/api/jobs")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == len(expected_jobs)
        # The detail dialog reads this same complete item from the records list.
        assert sorted(payload["items"], key=lambda row: row["id"]) == expected_jobs
        exported = client.get("/api/jobs/export")
        assert exported.status_code == 200
        csv_rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert {row["职位"]: row["薪资"] for row in csv_rows} == {
            row["title"]: row["salary"] for row in expected_jobs
        }
    assert snapshot(app.state.store) == (expected_jobs, original_ledger)
