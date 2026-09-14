"""Offline adapter contracts: no recruiting platform is contacted or messaged."""
import asyncio
from pathlib import Path

import pytest

from jobs_app.browser import (
    BrowserAutomation, _job_id, _safe_url, build_search_url, parse_salary, search_cities,
)


def test_salary_city_and_platform_boundaries():
    assert parse_salary("15-25K·14薪") == (15000, 25000)
    assert parse_salary("1.5-2万/月") == (15000, 20000)
    assert parse_salary("24-36万/年") == (20000, 30000)
    assert parse_salary("200-300元/天") == (4350, 6525)
    assert parse_salary("面议") == (None, None)
    assert _job_id("https://www.liepin.com/job/123.shtml") == "123"
    assert search_cities("51job", {"cities": ["北京市", "杭州"]}) == [("北京", "010000"), ("杭州", "080200")]
    with pytest.raises(ValueError, match="未扩大到全国"):
        search_cities("boss", {"cities": ["不存在的城市"]})
    with pytest.raises(ValueError):
        search_cities("boss", {"cities": ["北京", "上海"], "city_codes": ["101010100"]})
    assert "city=101010100" in build_search_url("boss", "Python", "101010100", {"extra_filters": {"city": "0"}})
    for unsafe in ("https://zhipin.com.evil.example/job", "javascript:alert(1)", "https://evil.example/job", "https://user@www.zhipin.com/job"):
        with pytest.raises(ValueError):
            _safe_url("boss", unsafe)


def test_offline_platform_payloads(tmp_path):
    adapter = BrowserAutomation(tmp_path, lambda *args: None)
    payload = {"data": {"items": [{"jobId": 123, "jobName": "Python", "provideSalaryString": "1-2万", "fullCompanyName": "测试公司", "jobAreaString": "北京"}]}}
    data = adapter._api_jobs("51job", payload)[0]
    job = adapter._normalize_job("51job", data, "北京")
    assert job["external_id"] == "123"
    assert job["url"] == "https://we.51job.com/pc/jobdetail?jobId=123"
    assert job["salary_min"] == 10000
    liepin = {"data": {"data": {"jobCardList": [{"job": {"jobId": 7, "title": "开发", "link": "https://www.liepin.com/job/7.shtml", "salary": "10-20k", "dq": "010"}, "comp": {"compName": "测试公司"}, "recruiter": {"recruiterTitle": "猎头"}}]}}}
    job = adapter._normalize_job("liepin", adapter._api_jobs("liepin", liepin)[0], "北京")
    assert job["city"] == "北京" and job["headhunter"]


def test_offline_browser_dom_and_success_evidence(tmp_path):
    """Run against actual Chromium with every network request blocked."""
    pytest.importorskip("playwright.async_api")

    async def check():
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = None
            for channel in ("chrome", "msedge", None):
                try:
                    browser = await pw.chromium.launch(channel=channel, headless=True)
                    break
                except Exception:
                    pass
            if browser is None:
                pytest.skip("An installed Chromium browser is needed for offline DOM verification")
            context = await browser.new_context()
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            adapter = BrowserAutomation(Path(tmp_path), lambda *args: None)
            try:
                await page.set_content('''<div class="joblist-box__item">
                    <a class="jobinfo__name" href="https://jobs.zhaopin.com/CC123.htm">Python 开发</a>
                    <p class="jobinfo__salary">15-25K</p><div class="companyinfo__name">测试公司</div>
                    <div class="jobinfo__other-info-item"><span>北京</span></div></div>''')
                job = (await adapter._dom_jobs(page, "zhilian"))[0]
                assert job["title"] == "Python 开发" and job["company"] == "测试公司"
                for platform, html in {
                    "boss": '<ul class="rec-job-list"><li class="job-card-box"><a class="job-name" href="/job_detail/abc.html">Python</a><span class="company-name">测试公司</span><span class="salary">15-25K</span><span class="company-location">北京</span></li></ul>',
                    "liepin": '<div class="job-card-pc-container"><a class="job-title-box" href="/job/123.shtml">Python</a><span class="company-name">测试公司</span><span class="job-salary">15-25K</span><span class="job-dq">北京</span></div>',
                    "51job": '<div class="joblist-item"><a class="jname" href="https://we.51job.com/pc/jobdetail?jobId=123">Python</a><span class="cname">测试公司</span><span class="sal">15-25K</span><span class="area">北京</span></div>',
                }.items():
                    await page.set_content(html)
                    result = await adapter._dom_jobs(page, platform)
                    assert len(result) == 1 and result[0]["company"] == "测试公司"

                class ImmediatePage:
                    # Keep real locator semantics while skipping observation delays in this local fixture.
                    def __getattr__(self, name):
                        return getattr(page, name)

                    async def wait_for_timeout(self, milliseconds):
                        pass

                fast = ImmediatePage()
                await page.set_content('<button id="apply">申请职位</button>')
                button = page.locator("#apply")
                result = await adapter._result_after_click(fast, "zhilian", button)
                assert result["status"] == "unconfirmed", "A click alone must never count as success"
                await page.set_content('<button id="apply">申请职位</button><div class="el-dialog__body">投递成功0个，未投递1个</div>')
                result = await adapter._result_after_click(fast, "51job", page.locator("#apply"))
                assert result["status"] == "unconfirmed"
                await page.set_content('<button id="apply">已申请</button>')
                result = await adapter._result_after_click(fast, "zhilian", page.locator("#apply"))
                assert result["status"] == "submitted"
                await page.set_content('<button id="apply">申请职位</button><div class="deliver-dialog">申请成功</div>')
                result = await adapter._result_after_click(fast, "zhilian", page.locator("#apply"))
                assert result["status"] == "submitted"
                # Fulfilled locally: exercise the full-chat URL without contacting BOSS.
                await page.route("https://www.zhipin.com/web/geek/chat", lambda route: route.fulfill(body="<html><body></body></html>"))
                await page.goto("https://www.zhipin.com/web/geek/chat")
                await page.set_content(r'''<div id="chat-input" class="chat-input" contenteditable="true"></div>
                    <button type="send" class="btn-send" onclick="const t=document.querySelector('#chat-input');
                    const row=document.createElement('li');row.className='message-item item-myself';row.dataset.mid='text-new';
                    row.innerHTML='<div class=text><p><span class=text-content></span></p></div><i class=\'message-status status-delivery\'></i>';
                    row.querySelector('.text-content').textContent=t.innerText;document.body.append(row);t.innerText=''">发送</button>''')
                result = await adapter._boss_message(fast, "您好，我擅长 Python。", None)
                assert result["status"] == "submitted"
                resume = Path(tmp_path) / "local-fixture.png"
                resume.write_bytes(b"local test fixture only")
                await page.set_content(r'''<div class="btn-sendimg" aria-label="发送图片"><input type="file" accept="image/*"
                    onchange="const row=document.createElement('li');row.className='message-item item-myself';row.dataset.mid='image-new';
                    row.innerHTML='<div class=message-image-content><img class=message-image></div><i class=\'message-status status-delivery\'></i>';
                    document.body.append(row)"></div>''')
                result = await adapter._boss_image(fast, str(resume))
                assert result["status"] == "submitted"
                await page.set_content('<div class="btn-sendimg" aria-label="发送图片"><input type="file" accept="image/*"></div>')
                result = await adapter._boss_image(fast, str(resume))
                assert result["status"] == "unconfirmed", "Selecting a file does not prove image delivery"
                await page.set_content('<h1>请完成安全验证</h1>')
                with pytest.raises(RuntimeError, match="手动完成"):
                    await adapter._guard(page, "boss")
            finally:
                await browser.close()

    asyncio.run(check())
