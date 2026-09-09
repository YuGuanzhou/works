from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLATFORM_NAMES = {"boss": "BOSS 直聘", "liepin": "猎聘", "51job": "前程无忧", "zhilian": "智联招聘"}
Platform = Literal["boss", "liepin", "51job", "zhilian"]


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(default_factory=lambda: ["Python"], min_length=1, max_length=30)
    cities: list[str] = Field(default_factory=lambda: ["北京"], min_length=1, max_length=20)
    city_codes: list[str] = Field(default_factory=list, max_length=20)
    salary_min: int = Field(0, ge=0, le=1000000)
    salary_max: int = Field(0, ge=0, le=1000000)
    excluded_words: list[str] = Field(default_factory=list, max_length=100)
    filter_inactive_hr: bool = True
    filter_headhunters: bool = False
    inactive_statuses: list[str] = Field(default_factory=lambda: ["半年前活跃", "一月前活跃", "本月活跃", "较早前活跃"])
    greeting: str = Field("您好，我对贵公司的 {title} 岗位很感兴趣，希望有机会进一步沟通，谢谢！", min_length=1, max_length=2000)
    use_ai: bool = False
    send_image: bool = False
    max_pages: int = Field(3, ge=1, le=30)
    daily_limit: int = Field(50, ge=1, le=500)
    delay_min: float = Field(5, ge=2, le=300)
    delay_max: float = Field(10, ge=2, le=300)
    extra_filters: dict[str, str | list[str]] = Field(default_factory=dict)

    @field_validator("keywords", "cities", "city_codes", "excluded_words", "inactive_statuses")
    @classmethod
    def clean_list(cls, values):
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(v) > 100 for v in cleaned):
            raise ValueError("单项内容不能超过 100 字")
        return cleaned

    @model_validator(mode="after")
    def check_ranges(self):
        if not self.keywords or not self.cities:
            raise ValueError("请填写至少一个岗位关键词和城市")
        if self.salary_max and self.salary_max < self.salary_min:
            raise ValueError("薪资上限不能低于下限")
        if self.delay_max < self.delay_min:
            raise ValueError("等待时间上限不能低于下限")
        if self.city_codes and len(self.city_codes) != len(self.cities):
            raise ValueError("自定义城市编码需要与城市一一对应")
        if any(not code.isdigit() for code in self.city_codes):
            raise ValueError("城市编码只能包含数字")
        return self


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = Field("gpt-4.1-mini", min_length=1, max_length=200)
    api_key: str = Field("", max_length=1000)
    profile: str = Field("", max_length=30000)
    prompt: str = Field("根据我的真实经历判断岗位匹配度，简洁礼貌地介绍与职位相关的优势。", max_length=8000)
    min_score: int = Field(65, ge=0, le=100)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        parts = urlsplit(value.strip())
        try:
            parts.port
        except ValueError:
            raise ValueError("模型服务端口无效") from None
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("请填写不含账号、查询参数的模型服务 HTTP(S) 地址")
        if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程模型服务必须使用 HTTPS，本机服务可以使用 HTTP")
        return value.strip().rstrip("/")


class NotificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    webhook: str = Field("", max_length=1000)

    @field_validator("webhook")
    @classmethod
    def valid_webhook(cls, value):
        if value:
            parts = urlsplit(value)
            query = parse_qs(parts.query, keep_blank_values=True)
            if parts.scheme != "https" or parts.netloc != "qyapi.weixin.qq.com" or parts.path != "/cgi-bin/webhook/send" or set(query) != {"key"} or len(query["key"]) != 1 or not query["key"][0].strip() or parts.fragment:
                raise ValueError("请填写企业微信机器人的 webhook 地址")
        return value


def defaults():
    return {"platforms": {key: PlatformConfig().model_dump() for key in PLATFORM_NAMES},
            "ai": AIConfig().model_dump(), "notifications": NotificationConfig().model_dump(),
            "resume": {"filename": "", "has_image": False}}
