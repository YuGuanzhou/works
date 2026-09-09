"""Visible, user-authenticated platform automation.

Platform URLs/selectors are adapted from loks666/get_jobs (non-commercial
license; see THIRD_PARTY_NOTICES.md). BOSS uses the Patchright driver to avoid
the ordinary driver's console/debugger instrumentation. No CAPTCHA solvers.
All methods must run on the same asyncio event loop.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .matching import parse_salary


PLATFORMS = {
    "boss": {"name": "BOSS直聘", "home": "https://www.zhipin.com/", "login": "https://www.zhipin.com/web/user/", "domain": "zhipin.com"},
    "liepin": {"name": "猎聘", "home": "https://www.liepin.com/", "login": "https://www.liepin.com/login", "domain": "liepin.com"},
    "51job": {"name": "前程无忧", "home": "https://we.51job.com/", "login": "https://we.51job.com/", "domain": "51job.com"},
    "zhilian": {"name": "智联招聘", "home": "https://www.zhaopin.com/", "login": "https://passport.zhaopin.com/login", "domain": "zhaopin.com"},
}
# Platform codes are distinct. Unknown names must never silently become nationwide.
CITY_CODES = {
    "北京": ("101010100", "010", "010000", "530"),
    "上海": ("101020100", "020", "020000", "538"),
    "广州": ("101280100", "050020", "030200", "763"),
    "深圳": ("101280600", "050090", "040000", "765"),
    "杭州": ("101210100", "070020", "080200", "653"),
    "成都": ("101270100", "280020", "090200", "801"),
    "南京": ("101190100", "060020", "070200", "635"),
    "武汉": ("101200100", "170020", "180200", "736"),
    "苏州": ("101190400", "060080", "070300", "639"),
    "重庆": ("101040100", "040", "060000", "551"),
    "天津": ("101030100", "030", "050000", "531"),
    "西安": ("101110100", "270020", "200200", "854"),
}
SPEC = {
    "boss": {
        "cards": "ul.rec-job-list li.job-card-box, li.job-card-wrapper",
        "title": "a.job-name, .job-name", "company": ".company-name, .boss-name",
        "salary": ".salary, .job-salary", "city": ".company-location, .job-area",
        "link": "a[href*='/job_detail/']", "tags": ".tag-list li",
        "description": ".job-sec-text", "active": ".boss-active-time",
        "in": "li.nav-figure .label-text, li.nav-figure img",
        "out": "li.nav-sign a, a.header-login-btn, a[ka='403_login'], .sign-form",
        "apply": "a.btn-startchat, a.op-btn-chat",
    },
    "liepin": {
        "cards": "div[class*='job-card-pc-container']",
        "title": ".job-title-box .ellipsis-1, .job-title-box, .job-title",
        "company": ".company-name, .company-name-box", "salary": ".job-salary, .salary",
        "city": ".job-dq-box, .job-dq", "link": "a[href*='/job/'], a[href*='/a/']",
        "tags": ".job-labels-box span", "description": ".job-intro-container, .job-description, .content.content-word",
        "active": ".recruiter-active-time", "in": ".header-quick-menu-user-photo",
        "out": "#header-quick-menu-login, .login-panel",
        "apply": "button:has-text('聊一聊'), .chat-btn:has-text('聊一聊'), button:has-text('立即应聘'), a:has-text('立即应聘')",
    },
    "51job": {
        "cards": ".joblist-item, .job-item, .j_joblist .e",
        "title": "[class*='jname'], .jname", "company": "[class*='cname'], .cname",
        "salary": ".sal, .salary", "city": ".d.at, .job-area, .area",
        "link": "a[href*='jobdetail'], a[href*='jobs.51job.com']", "tags": ".tags span",
        "description": ".job_msg, .bmsg.job_msg, .job-detail .job-desc, .job-info .job-detail",
        "active": ".hr-active", "in": "a.uname.e_icon.at, a[href*='/pc/my/myjob']",
        "out": "span.login.loginBtnClick, .login-layer",
        "apply": "button:has-text('申请职位'), a:has-text('申请职位'), button:has-text('立即申请'), button:has-text('投递简历')",
    },
    "zhilian": {
        "cards": "div.joblist-box__item", "title": "a.jobinfo__name",
        "company": ".companyinfo__name", "salary": "p.jobinfo__salary",
        "city": ".jobinfo__other-info-item > span", "link": "a.jobinfo__name",
        "tags": ".jobinfo__other-info-item", "description": ".describtion__detail-content, .job-detail, .job-description",
        "active": ".recruiter-active", "in": ".home-header__c-user, .user-info .user-name, .username-text",
        "out": "a.home-header__c-no-login, .passport-login",
        "apply": "button.collect-and-apply__btn, button:has-text('申请职位'), button:has-text('立即申请'), a:has-text('申请职位')",
    },
}
CHALLENGE = re.compile(r"请完成安全验证|访问验证|请(?:按住|拖动)滑块|完成验证后|操作过于频繁|访问过于频繁")
LIMIT = re.compile(r"今日投递太多|休息一下明天再来|已达.{0,8}上限|达到.{0,8}上限|次数过多")
SUCCESS = re.compile(r"(?:投递|申请|发送)成功|简历已投递")
ALREADY = re.compile(r"已申请|已投递|已沟通|继续沟通|继续聊")
NO_JOBS = re.compile(r"暂无(?:相关|符合条件的?|符合)?职位|没有符合条件的职位|未找到相关职位|没有找到相关职位")


def search_cities(platform: str, config: dict) -> list[tuple[str, str]]:
    index = list(PLATFORMS).index(platform)
    cities = config.get("cities") or ["北京"]
    codes = config.get("city_codes") or []
    if codes:
        if len(codes) != len(cities) or any(not re.fullmatch(r"\d{2,12}", str(code)) for code in codes):
            raise ValueError("city_codes 须与 cities 一一对应，且为该平台的数字城市码")
        return [(str(city), str(code)) for city, code in zip(cities, codes)]
    result = []
    for value in cities:
        city = str(value).strip().removesuffix("市")
        if city not in CITY_CODES:
            raise ValueError(f"{PLATFORMS[platform]['name']} 暂无城市「{city}」的内置映射，请在高级配置提供 city_codes；未扩大到全国")
        result.append((city, CITY_CODES[city][index]))
    return result


def build_search_url(platform: str, keyword: str, city_code: str, config: dict) -> str:
    params = dict(config.get("extra_filters") or {})
    for key, value in list(params.items()):
        if isinstance(value, (list, tuple)):
            params[key] = ",".join(map(str, value))
        elif not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"高级查询参数 {key} 必须是字符串、数字或列表")
    if platform == "boss":
        base = "https://www.zhipin.com/web/geek/jobs"
        params.update(query=keyword, city=city_code)
    elif platform == "liepin":
        base = "https://www.liepin.com/zhaopin/"
        params.update(key=keyword, city=city_code, dq=city_code, currentPage=0)
    elif platform == "51job":
        base = "https://we.51job.com/pc/search"
        params.update(keyword=keyword, jobArea=city_code)
    else:
        base = f"https://www.zhaopin.com/sou/jl{city_code}/p1"
        params.update(kw=keyword)
    return base + "?" + urlencode(params)


def _job_id(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if query.get("jobId"):
        return query["jobId"][0]
    match = re.search(r"/(?:job_detail|job|a)/([^/?]+?)(?:\.s?html?)?$", parts.path)
    if match:
        return match[1]
    return parts.path.rsplit("/", 1)[-1].removesuffix(".htm").removesuffix(".html") or hashlib.sha256(url.encode()).hexdigest()[:20]


def _safe_url(platform: str, url: str) -> str:
    url = urljoin(PLATFORMS[platform]["home"], url)
    parts = urlsplit(url)
    domain = PLATFORMS[platform]["domain"]
    if parts.scheme != "https" or not parts.hostname or not (parts.hostname == domain or parts.hostname.endswith("." + domain)) or parts.username:
        raise ValueError("职位链接不属于目标招聘平台")
    return url


class BrowserAutomation:
    def __init__(self, data_dir: Path, log):
        self.data_dir = Path(data_dir)
        self.log = log
        self._pw = None
        self._boss_pw = None
        self._contexts: dict = {}
        self._pages: dict = {}
        self._job_sources: dict = {}
        self._navigation: dict = {}

    async def _driver(self, platform):
        if platform == "boss":
            if self._boss_pw is None:
                try:
                    from patchright.async_api import async_playwright
                except ImportError:
                    raise RuntimeError("BOSS 兼容驱动未安装，请更新软件或安装 requirements.txt 中的依赖") from None
                self._boss_pw = await async_playwright().start()
            return self._boss_pw
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        return self._pw

    def _watch_navigation(self, page):
        """Detect repeated main-frame reloads without storing session-bearing URLs."""
        if page in self._navigation:
            return
        history = deque(maxlen=12)
        self._navigation[page] = history

        def navigated(frame):
            if frame == page.main_frame:
                parts = urlsplit(frame.url)
                history.append((time.monotonic(), parts.netloc + parts.path))

        page.on("framenavigated", navigated)
        page.on("close", lambda *_: self._navigation.pop(page, None))

    def _unstable(self, page):
        recent = [(moment, path) for moment, path in self._navigation.get(page, []) if time.monotonic() - moment < 12]
        return len(recent) >= 6 or any(sum(path == candidate for _, path in recent) >= 4 for _, candidate in recent)

    def _returned_blank(self, page):
        return page.url == "about:blank" and any(
            path.split("/")[0] == "zhipin.com" or path.split("/")[0].endswith(".zhipin.com")
            for _, path in self._navigation.get(page, []))

    async def _context(self, platform: str):
        if platform not in PLATFORMS:
            raise ValueError("不支持的招聘平台")
        existing = self._contexts.get(platform)
        if existing:
            return existing
        driver = await self._driver(platform)
        profile = self.data_dir / "profiles" / platform
        profile.mkdir(parents=True, exist_ok=True)
        for channel in ("chrome", "msedge", None):
            try:
                options = {"no_viewport": True} if platform == "boss" else {"viewport": {"width": 1440, "height": 900}, "locale": "zh-CN"}
                context = await driver.chromium.launch_persistent_context(
                    str(profile), channel=channel, headless=False, accept_downloads=False, **options)
                context.set_default_timeout(8000)
                context.set_default_navigation_timeout(35000)
                self._contexts[platform] = context
                context.on("close", lambda *_: self._forget(platform, context))
                if platform == "boss":
                    context.on("page", self._watch_navigation)
                    for page in context.pages:
                        self._watch_navigation(page)
                self.log("info", f"已打开 {PLATFORMS[platform]['name']} 浏览器，请在浏览器中自行登录", platform)
                return context
            except Exception as error:
                # Only missing browser binaries justify trying a different browser.
                message = str(error).lower()
                if not any(word in message for word in ("executable doesn't exist", "distribution", "not found", "不存在")):
                    raise RuntimeError("浏览器启动失败；请关闭占用该平台专用配置的浏览器后重试") from error
        raise RuntimeError("未找到 Chrome/Edge/Chromium；请安装 Chrome 或运行 python -m playwright install chromium")

    def _forget(self, platform: str, context):
        if self._contexts.get(platform) is context:
            self._contexts.pop(platform, None)
            self._pages.pop(platform, None)

    async def _page(self, platform: str):
        context = await self._context(platform)
        page = self._pages.get(platform)
        if not page or page.is_closed():
            page = context.pages[0] if context.pages else await context.new_page()
            self._pages[platform] = page
        return page

    async def _visible(self, page, selector: str):
        for locator in await page.locator(selector).all():
            if await locator.is_visible():
                return locator
        return None

    async def _body(self, page) -> str:
        return await page.locator("body").inner_text(timeout=5000)

    async def _guard(self, page, platform: str):
        if platform == "boss" and self._unstable(page):
            raise RuntimeError("BOSS 页面连续刷新或重定向，任务已停止。请关闭该平台浏览器后重新登录，勿连续重试")
        if platform == "boss" and self._returned_blank(page):
            raise RuntimeError("BOSS 页面退回空白页，请关闭该平台浏览器后重新打开登录")
        body = await self._body(page)
        if "verify-slider" in page.url or CHALLENGE.search(body):
            raise RuntimeError("平台要求安全验证，请在已打开的浏览器中手动完成后重新运行任务")
        if LIMIT.search(body):
            raise RuntimeError("平台提示操作或投递次数达到上限，请稍后再试")
        on_boss_login = platform == "boss" and urlsplit(page.url).path.rstrip("/") == "/web/user"
        if on_boss_login or await self._visible(page, SPEC[platform]["out"]):
            raise RuntimeError("登录已失效或尚未登录，请先在平台浏览器完成登录")
        return body

    async def status(self, platform: str) -> dict:
        if platform not in PLATFORMS:
            raise ValueError("不支持的招聘平台")
        page = self._pages.get(platform)
        result = {"platform": platform, "name": PLATFORMS[platform]["name"], "logged_in": False,
                  "state": "closed", "detail": "浏览器未打开，保存的登录状态需打开后验证"}
        if not page or page.is_closed():
            result.update(status=result["state"], message=result["detail"])
            return result
        try:
            if platform == "boss" and (self._unstable(page) or self._returned_blank(page)):
                result.update(state="browser_blocked", detail="页面连续刷新或重定向，请关闭 BOSS 浏览器后重新登录")
                return result | {"status": result["state"], "message": result["detail"]}
            body = await self._body(page)
            if "verify-slider" in page.url or CHALLENGE.search(body):
                result.update(state="verification_required", detail="请在浏览器中手动完成安全验证")
            elif (platform == "boss" and urlsplit(page.url).path.rstrip("/") == "/web/user") or await self._visible(page, SPEC[platform]["out"]):
                result.update(state="login_required", detail="请在浏览器中登录或扫码")
            elif await self._visible(page, SPEC[platform]["in"]):
                result.update(state="logged_in", logged_in=True, detail="已检测到平台用户信息")
            else:
                result.update(state="unknown", detail="当前页面未显示明确登录标记；请返回平台首页刷新状态")
        except Exception:
            result.update(state="unknown", detail="页面正在加载或已关闭，请稍后刷新状态")
        result.update(status=result["state"], message=result["detail"])
        return result

    async def open_login(self, platform: str) -> dict:
        page = await self._page(platform)
        # Repeated clicks should focus the existing login/verification page, never restart it.
        parts = urlsplit(page.url)
        domain = PLATFORMS[platform]["domain"]
        on_platform = parts.hostname == domain or bool(parts.hostname and parts.hostname.endswith("." + domain))
        if not on_platform:
            await page.goto(PLATFORMS[platform]["home"], wait_until="domcontentloaded")
        result = await self.status(platform)
        for _ in range(8):
            if result["state"] != "unknown":
                break
            await page.wait_for_timeout(400)
            result = await self.status(platform)
        if result["state"] == "login_required":
            # Only a positive logged-out marker warrants navigation, not a challenge or slow page.
            on_login_page = urlsplit(page.url).path == urlsplit(PLATFORMS[platform]["login"]).path
            if not on_login_page:
                await page.goto(PLATFORMS[platform]["login"], wait_until="domcontentloaded")
                await page.wait_for_timeout(800)
            switches = {
                "boss": ".btn-sign-switch.ewm-switch, .sign-switch-tip:has-text('扫码'), :text-is('APP扫码登录')",
                "liepin": ".switch-type-mask-img-box",
                "51job": "span.login.loginBtnClick",
                "zhilian": "div.zppp-panel-normal-bar__img",
            }
            if not (on_platform and on_login_page):
                switch = await self._visible(page, switches[platform])
                if switch:
                    await switch.click()
        await page.bring_to_front()
        return await self.status(platform)

    async def _before_navigation(self, page, platform):
        # Do not replace an existing login/challenge while the user is completing it.
        if platform == "boss" and (page.url != "about:blank" or self._returned_blank(page)):
            await self._guard(page, platform)

    async def logout(self, platform: str) -> dict:
        if platform not in PLATFORMS:
            raise ValueError("不支持的招聘平台")
        context = self._contexts.get(platform)
        if context:
            await context.clear_cookies()
            await context.close()
        # Clear only this application's known per-platform profile, including IndexedDB.
        root = (self.data_dir / "profiles").resolve()
        profile = root / platform
        if profile.exists():
            if profile.is_symlink() or profile.resolve().parent != root:
                raise RuntimeError("浏览器配置目录不在应用数据目录内，已停止清理")
            shutil.rmtree(profile)
        self._pages.pop(platform, None)
        self._contexts.pop(platform, None)
        return {"platform": platform, "logged_in": False, "state": "closed", "status": "closed",
                "detail": "已清除本应用保存的该平台登录状态", "message": "已清除本应用保存的该平台登录状态"}

    async def close(self):
        for context in list(self._contexts.values()):
            await context.close()
        if self._pw:
            await self._pw.stop()
            self._pw = None
        if self._boss_pw:
            await self._boss_pw.stop()
            self._boss_pw = None

    async def _text(self, root, selector: str) -> str:
        nodes = root.locator(selector)
        return (await nodes.first.inner_text()).strip() if await nodes.count() else ""

    def _normalize_job(self, platform: str, data: dict, city: str) -> dict:
        url = _safe_url(platform, data["url"])
        salary_min, salary_max = parse_salary(data.get("salary", ""))
        raw_city = str(data.get("city") or city)
        if raw_city.isdigit():
            raw_city = city
        return {"platform": platform, "external_id": str(data.get("external_id") or _job_id(url)),
                "title": str(data.get("title") or ""), "company": str(data.get("company") or ""),
                "city": raw_city, "salary": str(data.get("salary") or ""), "salary_min": salary_min,
                "salary_max": salary_max, "url": url, "description": str(data.get("description") or ""),
                "hr_active": str(data.get("hr_active") or ""), "hr_status": str(data.get("hr_active") or ""),
                "headhunter": bool(data.get("headhunter")),
                "tags": [str(tag) for tag in data.get("tags", []) if tag]}

    async def _dom_jobs(self, page, platform: str) -> list[dict]:
        spec = SPEC[platform]
        result = []
        for card in await page.locator(spec["cards"]).all():
            link = card.locator(spec["link"]).first
            href = await link.get_attribute("href") if await link.count() else None
            if not href and platform == "boss":
                # The upstream BOSS layout loads detail into the right-hand pane.
                await card.click()
                more = page.locator("a.more-job-btn").first
                try:
                    await more.wait_for(state="visible", timeout=5000)
                    href = await more.get_attribute("href")
                except Exception:
                    pass
            if not href:
                continue
            data = {key: await self._text(card, spec[key]) for key in ("title", "company", "salary", "city")}
            data.update(url=href, tags=await card.locator(spec["tags"]).all_inner_texts())
            data["headhunter"] = "猎头" in await card.inner_text()
            result.append(data)
        return result

    def _api_jobs(self, platform: str, payload: dict) -> list[dict]:
        def at(*path):
            value = payload
            for key in path:
                value = value.get(key, {}) if isinstance(value, dict) else {}
            return value
        if platform == "liepin":
            items = at("data", "data", "jobCardList") or at("data", "jobCardList")
            result = []
            for item in items if isinstance(items, list) else []:
                job, comp, recruiter = item.get("job", {}), item.get("comp", {}), item.get("recruiter", {})
                if job.get("link"):
                    result.append({"external_id": job.get("jobId"), "title": job.get("title"), "url": job["link"],
                                   "company": comp.get("compName"), "salary": job.get("salary"), "city": job.get("dq"),
                                   "headhunter": "猎头" in str(recruiter.get("recruiterTitle", "")),
                                   "tags": [job.get("requireEduLevel"), job.get("requireWorkYears")]})
            return result
        paths = [("data", name) for name in ("items", "jobList", "list", "jobs")]
        paths += [("resultbody", "job", "items"), ("job", "items"), ("resultbody", "items")]
        items = next((at(*path) for path in paths if isinstance(at(*path), list)), [])
        return [{"external_id": item["jobId"], "title": item.get("jobName") or item.get("jobTitle"),
                 "company": item.get("fullCompanyName") or item.get("companyName") or item.get("ctmName"),
                 "salary": item.get("provideSalaryString") or item.get("salaryDesc") or item.get("salary"),
                 "city": item.get("jobAreaString") or item.get("cityName"),
                 "url": item.get("jobHref") or f"https://we.51job.com/pc/jobdetail?jobId={item['jobId']}",
                 "tags": [item.get("degreeString"), item.get("workYearString")]}
                for item in items if item.get("jobId")]

    async def _detail(self, context, job: dict):
        detail = await context.new_page()
        read_ok = False
        try:
            await detail.goto(job["url"], wait_until="domcontentloaded")
            await detail.wait_for_timeout(800)
            await self._guard(detail, job["platform"])
            spec = SPEC[job["platform"]]
            job["description"] = await self._text(detail, spec["description"])
            job["hr_active"] = await self._text(detail, spec["active"])
            job["hr_status"] = job["hr_active"]
            if job["platform"] == "boss":
                recruiter = await self._text(detail, ".boss-info-attr")
                job["headhunter"] = job["headhunter"] or "猎头" in recruiter
                company = await self._text(detail, ".sider-company .company-info a, .company-info .name")
                if company:
                    job["company"] = company
            read_ok = True
        finally:
            if read_ok:
                await detail.close()

    async def search(self, platform: str, config: dict, stop):
        cities = search_cities(platform, config)
        keywords = [str(word).strip() for word in config.get("keywords", []) if str(word).strip()]
        if not keywords:
            raise ValueError("请至少设置一个搜索关键词")
        context = await self._context(platform)
        page = await self._page(platform)
        await self._before_navigation(page, platform)
        seen = set()
        payloads = []
        async def capture(response):
            marker = "/api/job/search-pc" if platform == "51job" else "com.liepin.searchfront4c.pc-search-job"
            if marker in response.url and "cond-init" not in response.url and response.status == 200:
                try:
                    value = await response.json()
                    if isinstance(value, dict):
                        payloads.append(value)
                except Exception:
                    self.log("warning", "平台搜索响应格式无法读取，尝试页面职位卡片", platform)
        if platform in ("51job", "liepin"):
            page.on("response", capture)
        try:
            for city, code in cities:
                for keyword in keywords:
                    if stop():
                        return
                    payloads.clear()
                    url = build_search_url(platform, keyword, code, config)
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1500)
                    if platform == "zhilian":
                        field = await self._visible(page, "input[placeholder*='职位'], input[name='kw'], input[placeholder*='公司']")
                        if not field:
                            await self._guard(page, platform)
                            raise RuntimeError("智联搜索输入框未找到，平台页面结构可能已改变")
                        await field.fill(keyword)
                        await field.press("Enter")
                        await page.wait_for_timeout(1200)
                    for number in range(1, min(int(config.get("max_pages", 3)), 50) + 1):
                        if stop():
                            return
                        await self._guard(page, platform)
                        self.log("info", f"正在采集 {city} / {keyword} 第 {number} 页", platform)
                        raw_jobs = self._api_jobs(platform, payloads[-1]) if payloads else []
                        if not raw_jobs:
                            try:
                                await page.locator(SPEC[platform]["cards"]).first.wait_for(state="visible", timeout=10000)
                            except Exception:
                                body = await self._guard(page, platform)
                                if NO_JOBS.search(body):
                                    break
                                raise RuntimeError("未找到职位列表：请检查登录、网络或平台页面结构；本次未读取到职位")
                            raw_jobs = await self._dom_jobs(page, platform)
                        new_count = 0
                        for data in raw_jobs:
                            if stop():
                                return
                            job = self._normalize_job(platform, data, city)
                            if not job["title"] or job["external_id"] in seen:
                                continue
                            seen.add(job["external_id"])
                            new_count += 1
                            self._job_sources[(platform, job["external_id"])] = (url, number, keyword)
                            await self._detail(context, job)
                            if not job["description"]:
                                self.log("warning", f"职位详情正文未读取到：{job['title']}；AI 评估应跳过该职位", platform)
                            yield job
                        if not new_count:
                            if number == 1 and not raw_jobs:
                                raise RuntimeError("职位列表已出现，但未能提取有效详情链接；平台结构可能已改变")
                            break
                        if number >= min(int(config.get("max_pages", 3)), 50):
                            break
                        payloads.clear()
                        if not await self._next_page(page, platform, number + 1):
                            break
        finally:
            if platform in ("51job", "liepin"):
                page.remove_listener("response", capture)

    async def _next_page(self, page, platform: str, number: int) -> bool:
        selectors = {"liepin": "li.ant-pagination-next:not(.ant-pagination-disabled) button",
                     "zhilian": "a.soupager__btn:has-text('下一页')",
                     "boss": ".options-pages a.next, .ui-pagination-next, .page-next",
                     "51job": ".bottom-page .next, .el-pagination .btn-next"}
        next_button = await self._visible(page, selectors[platform])
        if next_button and not await next_button.is_disabled():
            if "disabled" in (await next_button.get_attribute("class") or ""):
                return False
            await next_button.click()
        elif platform == "51job" and await page.locator("#jump_page").count():
            await page.locator("#jump_page").fill(str(number))
            await page.locator("span.jumpPage").click()
        elif platform == "boss":
            # BOSS currently extends rec-job-list by scrolling, not numbered pages.
            cards = page.locator(SPEC[platform]["cards"])
            if not await cards.count():
                return False
            await cards.last.scroll_into_view_if_needed()
            await page.mouse.wheel(0, 1500)
        else:
            return False
        await page.wait_for_timeout(1500)
        return True

    async def _find_apply(self, page, job: dict):
        platform = job["platform"]
        buttons = [button for button in await page.locator(SPEC[platform]["apply"]).all() if await button.is_visible()]
        # A detail page with several application buttons may include unrelated recommendations.
        if len(buttons) == 1:
            return buttons[0]
        source = self._job_sources.get((platform, str(job["external_id"])))
        if not source or platform not in ("liepin", "zhilian"):
            return None
        source_url, page_number, keyword = source
        await page.goto(source_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        if platform == "zhilian":
            field = await self._visible(page, "input[placeholder*='职位'], input[name='kw'], input[placeholder*='公司']")
            if not field:
                return None
            await field.fill(keyword)
            await field.press("Enter")
            await page.wait_for_timeout(1200)
        for number in range(2, page_number + 1):
            if not await self._next_page(page, platform, number):
                return None
        await self._guard(page, platform)
        for card in await page.locator(SPEC[platform]["cards"]).all():
            for anchor in await card.locator(SPEC[platform]["link"]).all():
                href = await anchor.get_attribute("href")
                if href and _job_id(urljoin(PLATFORMS[platform]["home"], href)) == str(job["external_id"]):
                    await card.hover()
                    return await self._visible(card, SPEC[platform]["apply"])
        return None

    async def _result_after_click(self, page, platform: str, button) -> dict:
        # A changed application button or a success dialog is evidence; a click is not.
        dialogs = ".el-dialog__body, .el-message, .ant-message, .deliver-dialog, .successContent, .a-job-apply-workflow, [role='alert'], .toast"
        for _ in range(20):
            await page.wait_for_timeout(300)
            await self._guard(page, platform)
            for alert in await page.locator(dialogs).all():
                if await alert.is_visible():
                    message = (await alert.inner_text()).strip()
                    if SUCCESS.search(message) and not re.search(r"成功\s*0|未成功|不成功|成功.{0,3}失败", message):
                        return {"status": "submitted", "detail": message[:200]}
                    if re.search(r"申请失败|投递失败|发送失败|简历不完整|请完善简历", message):
                        return {"status": "failed", "detail": message[:200]}
            try:
                label = await button.inner_text(timeout=500)
                if ALREADY.search(label):
                    return {"status": "submitted", "detail": f"操作后目标按钮变为「{label.strip()}」"}
            except Exception:
                pass
        return {"status": "unconfirmed", "detail": "已点击目标职位按钮，未观察到明确投递成功信号，请在平台核实，勿直接重试"}

    async def _boss_message(self, page, greeting: str, image_path: str | None) -> dict:
        box = page.locator("div#chat-input.chat-input[contenteditable='true'], textarea.input-area").first
        try:
            await box.wait_for(state="visible", timeout=10000)
        except Exception:
            await self._guard(page, "boss")
            return {"status": "unconfirmed", "detail": "已点击立即沟通，但聊天输入框未出现，请在平台核实"}
        if not greeting.strip():
            return {"status": "unconfirmed", "detail": "已打开沟通窗口；招呼语为空，未发送自定义消息"}
        # Count exact visible text outside the editor before/after sending to avoid stale-message success.
        messages = page.get_by_text(greeting.strip(), exact=True)
        before = await messages.count()
        await box.fill(greeting.strip())
        send = await self._visible(page, "div.send-message, button[type='send'].btn-send, button.btn-send")
        if not send:
            return {"status": "unconfirmed", "detail": "招呼语已填写，但未找到发送按钮，请在浏览器检查"}
        await send.click()
        confirmed = False
        for _ in range(20):
            await page.wait_for_timeout(300)
            body = await self._guard(page, "boss")
            if "发送失败" in body:
                return {"status": "failed", "detail": "平台提示消息发送失败"}
            editor_value = await box.evaluate("el => el.value === undefined ? el.innerText : el.value")
            if not editor_value.strip() and await messages.count() > before:
                confirmed = True
                break
        if not confirmed:
            return {"status": "unconfirmed", "detail": "已点击发送，未同时观察到输入框清空和新增消息，请在平台核实"}
        detail = "已观察到招呼语出现在聊天记录中"
        if image_path:
            return await self._boss_image(page, image_path)
        return {"status": "submitted", "detail": detail}

    async def _boss_image(self, page, image_path: str) -> dict:
        prefix = "招呼语已出现在聊天记录中；"
        toolbar = page.locator("div.btn-sendimg[aria-label='发送图片'], div[aria-label='发送图片'].btn-sendimg").first
        upload = toolbar.locator("input[type='file'][accept*='image']").first
        if not await toolbar.count():
            # Upstream opens the full conversation before accessing the image toolbar.
            chat = await self._visible(page, "a.btn-startchat, a.op-btn-chat")
            if chat:
                await chat.click()
                try:
                    await toolbar.wait_for(state="visible", timeout=10000)
                except Exception:
                    return {"status": "unconfirmed", "detail": prefix + "未能打开图片发送工具栏，图片未发送"}
        try:
            if await upload.count():
                await upload.set_input_files(image_path)
            elif await toolbar.count():
                async with page.expect_file_chooser(timeout=5000) as pending:
                    await toolbar.click()
                await (await pending.value).set_files(image_path)
            else:
                return {"status": "unconfirmed", "detail": prefix + "未找到图片上传入口，图片未发送"}
            clicked_preview = False
            for _ in range(40):
                await page.wait_for_timeout(400)
                await self._guard(page, "boss")
                for alert in await page.locator(".dialog-con, [role='alert'], .toast, .message-tip").all():
                    if await alert.is_visible():
                        message = (await alert.inner_text()).strip()
                        if re.search(r"图片.{0,4}发送成功|发送图片成功", message):
                            return {"status": "submitted", "detail": prefix + "平台提示图片发送成功"}
                        if re.search(r"图片.{0,4}失败|上传失败|图片过大|格式不支持", message):
                            return {"status": "unconfirmed", "detail": prefix + "图片发送失败，请在平台检查"}
                # Only confirm an image preview dialog, never another job or a generic page button.
                previews = page.locator(".dialog-container:has(img), [role='dialog']:has(img)")
                if not clicked_preview:
                    for preview in await previews.all():
                        if await preview.is_visible() and re.search(r"发送图片|图片预览", await preview.inner_text()):
                            send = await self._visible(preview, "button[type='send'], button:has-text('发送')")
                            if send and not await send.is_disabled():
                                await send.click()
                                clicked_preview = True
                                break
            return {"status": "unconfirmed", "detail": prefix + "图片已选择并等待上传/发送，未获得图片送达确认；已保留浏览器页面，请在平台核实"}
        except Exception:
            return {"status": "unconfirmed", "detail": prefix + "图片上传或发送未完成确认，已保留浏览器页面，请在平台核实"}

    async def apply(self, job: dict, greeting: str, image_path: str | None = None) -> dict:
        platform = job["platform"]
        url = _safe_url(platform, job["url"])
        if image_path and (not Path(image_path).is_file() or Path(image_path).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp")):
            return {"status": "failed", "detail": "图片简历不存在或不是支持的图片文件"}
        context = await self._context(platform)
        page = await context.new_page()
        clicked = False
        outcome = None
        keep_open = False
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            await self._guard(page, platform)
            button = await self._find_apply(page, job)
            if not button:
                existing = page.get_by_text(ALREADY, exact=True)
                if await existing.count() and await existing.first.is_visible():
                    return {"status": "already_contacted", "detail": "平台显示该职位已申请或已沟通"}
                return {"status": "failed", "detail": "目标职位投递按钮未找到，可能已下架或页面结构改变"}
            if ALREADY.search(await button.inner_text()):
                return {"status": "already_contacted", "detail": "平台显示该职位已申请或已沟通"}
            if await button.is_disabled():
                return {"status": "failed", "detail": "平台投递按钮不可用"}
            clicked = True  # Even a click timeout can happen after the platform accepted an action.
            await button.click()
            if platform == "boss":
                outcome = await self._boss_message(page, greeting, image_path)
            else:
                outcome = await self._result_after_click(page, platform, button)
            return outcome
        except Exception as error:
            # Do not log raw URLs, session-bearing query parameters, cookies, or chat text.
            detail = str(error).splitlines()[0]
            if "http" in detail:
                detail = "平台页面操作失败，请查看浏览器中的登录、验证或网络状态"
            keep_open = bool(re.search(r"验证|登录|上限", detail))
            outcome = {"status": "unconfirmed" if clicked else "failed", "detail": detail[:250]}
            return outcome
        finally:
            # Keep uncertain pages visible so users can complete verification or inspect outcomes.
            if not keep_open and (not clicked or (outcome and outcome["status"] != "unconfirmed")):
                await page.close()

    async def rejected_companies(self, platform: str) -> list[str]:
        if platform != "boss":
            raise ValueError("目前仅 BOSS 支持读取明确拒绝的最近聊天公司")
        page = await self._page(platform)
        await self._before_navigation(page, platform)
        await page.goto("https://www.zhipin.com/web/geek/chat", wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)
        await self._guard(page, platform)
        result = set()
        rejection = re.compile(r"暂不合适|不太合适|不符合.{0,8}要求|暂不考虑|很遗憾|岗位已招满|已经招满")
        # ponytail: inspect at most 10 loaded conversation batches; historical sync needs explicit pagination support.
        seen = set()
        for _ in range(10):
            items = page.locator("li[role='listitem']")
            count = await items.count()
            if not count:
                raise RuntimeError("未找到聊天列表，请确认登录或检查平台页面结构")
            for item in await items.all():
                company = await self._text(item, ".title-box .name-box span:nth-child(2)")
                message = await self._text(item, ".last-msg .last-msg-text")
                if company and rejection.search(message) and not re.search(r"\.\.\.|…", company):
                    result.add(company.strip())
            marker = await items.last.inner_text()
            if marker in seen:
                break
            seen.add(marker)
            await items.last.scroll_into_view_if_needed()
            await page.mouse.wheel(0, 800)
            await page.wait_for_timeout(700)
        return sorted(result)
