"""Offline city resolution contracts; no recruiting platform is contacted."""
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from jobs_app.browser import build_search_url, search_cities
from jobs_app.cities import BOSS_CITY_CODES


def test_boss_changsha_search_url_keeps_the_selected_city():
    config = {"cities": ["  长沙市  "], "extra_filters": {"city": "100010000"}}
    assert search_cities("boss", config) == [("长沙", "101250100")]
    _, code = search_cities("boss", config)[0]
    params = parse_qs(urlsplit(build_search_url("boss", "Python 开发", code, config)).query)
    assert params["city"] == ["101250100"]
    assert params["query"] == ["Python 开发"]


@pytest.mark.parametrize(("city", "code"), [
    ("北京", "101010100"), ("上海", "101020100"),
    ("天津", "101030100"), ("重庆", "101040100"),
    ("哈尔滨", "101050100"), ("乌鲁木齐", "101130100"),
    ("拉萨", "101140100"), ("昆明", "101290100"),
    ("香港", "101320300"), ("澳门", "101330100"),
    ("台湾", "101341100"), ("可克达拉", "101132200"),
])
def test_boss_city_codes_across_regions(city, code):
    assert search_cities("boss", {"cities": [city]}) == [(city, code)]


@pytest.mark.parametrize(("full_name", "short_name", "code"), [
    ("湘西土家族苗族自治州", "湘西", "101251400"),
    ("恩施土家族苗族自治州", "恩施", "101201300"),
    ("大理白族自治州", "大理", "101291600"),
    ("海南藏族自治州", "海南州", "101150500"),
])
def test_boss_autonomous_prefecture_names(full_name, short_name, code):
    for value in (full_name, short_name):
        assert search_cities("boss", {"cities": [f"  {value}  "]}) == [(full_name, code)]


def test_boss_complete_official_city_table():
    assert len(BOSS_CITY_CODES) == len(set(BOSS_CITY_CODES.values())) == 373
    for city, code in BOSS_CITY_CODES.items():
        assert re.fullmatch(r"101\d{6}", code), (city, code)
        assert not code.endswith("0000"), "Province codes must not become city codes"
        assert search_cities("boss", {"cities": [f"  {city}市  "]}) == [(city, code)]


def test_explicit_city_codes_take_precedence():
    assert search_cities("boss", {
        "cities": ["长沙", "自定义城市"], "city_codes": ["101010100", "123456789"],
    }) == [("长沙", "101010100"), ("自定义城市", "123456789")]
    for codes in (["101250100"], ["101250100", "abc"], ["101250100", "0"]):
        with pytest.raises(ValueError, match="city_codes"):
            search_cities("boss", {"cities": ["长沙", "自定义城市"], "city_codes": codes})


@pytest.mark.parametrize("unknown", ["不存在的城市", "海南", "海南省", "湖南省"])
def test_unknown_city_rejects_the_whole_search(unknown):
    with pytest.raises(ValueError, match="未扩大到全国"):
        search_cities("boss", {"cities": ["长沙", unknown]})


@pytest.mark.parametrize(("platform", "beijing_code"), [
    ("boss", "101010100"), ("liepin", "010"),
    ("51job", "010000"), ("zhilian", "530"),
])
def test_platform_city_codes_remain_isolated(platform, beijing_code):
    assert search_cities(platform, {"cities": ["北京市"]}) == [("北京", beijing_code)]
    if platform != "boss":
        with pytest.raises(ValueError, match="未扩大到全国"):
            search_cities(platform, {"cities": ["长沙"]})
