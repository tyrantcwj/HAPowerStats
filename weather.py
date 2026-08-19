# -*- coding: utf-8 -*-
"""天气与气温采集。

两个来源：
    ha        —— 直接读 HA 里的 weather.* 实体（不额外请求外网，推荐）
    openmeteo —— Open-Meteo 公共接口，免密钥，默认坐标是上海浦东

采到的天气会跟着每次采样一起写进历史记录，导出报表时就能标出
「这段时间用了多少电、当时多少度、什么天气」。
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_LOCATION = {
    "name": "上海浦东",
    "latitude": 31.2231,
    "longitude": 121.5397,
}

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_TTL = 600          # 外部接口 10 分钟取一次就够了
STALE_AFTER = 3600            # 超过 1 小时没更新就标记为过期

# HA weather 实体的状态值 -> 中文
CONDITION_TEXT = {
    "clear-night": "晴（夜间）",
    "cloudy": "多云",
    "exceptional": "异常天气",
    "fog": "雾",
    "hail": "冰雹",
    "lightning": "雷电",
    "lightning-rainy": "雷阵雨",
    "partlycloudy": "局部多云",
    "pouring": "大雨",
    "rainy": "雨",
    "snowy": "雪",
    "snowy-rainy": "雨夹雪",
    "sunny": "晴",
    "windy": "大风",
    "windy-variant": "大风",
}

# WMO 天气代码 -> (统一的 condition, 中文)
WMO_CONDITIONS = {
    0: ("sunny", "晴"),
    1: ("sunny", "晴间多云"),
    2: ("partlycloudy", "局部多云"),
    3: ("cloudy", "阴"),
    45: ("fog", "雾"),
    48: ("fog", "雾凇"),
    51: ("rainy", "毛毛雨"),
    53: ("rainy", "毛毛雨"),
    55: ("rainy", "毛毛雨"),
    56: ("rainy", "冻毛毛雨"),
    57: ("rainy", "冻毛毛雨"),
    61: ("rainy", "小雨"),
    63: ("rainy", "中雨"),
    65: ("pouring", "大雨"),
    66: ("rainy", "冻雨"),
    67: ("pouring", "冻雨"),
    71: ("snowy", "小雪"),
    73: ("snowy", "中雪"),
    75: ("snowy", "大雪"),
    77: ("snowy", "米雪"),
    80: ("rainy", "阵雨"),
    81: ("rainy", "阵雨"),
    82: ("pouring", "强阵雨"),
    85: ("snowy", "阵雪"),
    86: ("snowy", "强阵雪"),
    95: ("lightning-rainy", "雷阵雨"),
    96: ("lightning-rainy", "雷阵雨伴冰雹"),
    99: ("lightning-rainy", "雷阵雨伴冰雹"),
}


def condition_text(condition: str) -> str:
    key = (condition or "").strip().lower()
    return CONDITION_TEXT.get(key, condition or "")


def _to_float(value) -> Optional[float]:
    try:
        if value in (None, "", "unknown", "unavailable"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def list_ha_weather_entities(states: list) -> list:
    """HA 里所有 weather.* 实体，给设置页下拉用。"""
    result = []
    for item in states or []:
        entity_id = item.get("entity_id") or ""
        if not entity_id.startswith("weather."):
            continue
        attrs = item.get("attributes") or {}
        result.append({
            "entity_id": entity_id,
            "name": attrs.get("friendly_name") or entity_id,
            "state": item.get("state"),
            "condition_text": condition_text(item.get("state")),
            "temperature": _to_float(attrs.get("temperature")),
        })
    result.sort(key=lambda x: x["name"])
    return result


def from_ha_states(states: list, entity_id: str = "") -> Optional[dict]:
    """从已经取到的 HA 实体列表里挑一个天气实体，不产生额外请求。"""
    chosen = None
    for item in states or []:
        current_id = item.get("entity_id") or ""
        if not current_id.startswith("weather."):
            continue
        if entity_id:
            if current_id == entity_id:
                chosen = item
                break
        elif chosen is None:
            chosen = item
    if chosen is None:
        return None

    attrs = chosen.get("attributes") or {}
    temperature = _to_float(attrs.get("temperature"))
    if str(attrs.get("temperature_unit") or "").upper().endswith("F") and temperature is not None:
        temperature = (temperature - 32) / 1.8

    return {
        "source": "ha",
        "entity_id": chosen.get("entity_id"),
        "location": attrs.get("friendly_name") or chosen.get("entity_id"),
        "condition": chosen.get("state"),
        "condition_text": condition_text(chosen.get("state")),
        "temperature_c": None if temperature is None else round(temperature, 1),
        "humidity": _to_float(attrs.get("humidity")),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def from_open_meteo(latitude: float, longitude: float, location_name: str = "", timeout: int = 10) -> Optional[dict]:
    """Open-Meteo 免密钥接口，默认取上海浦东的实时天气。"""
    try:
        response = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "timezone": "auto",
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.warning("Open-Meteo 返回 HTTP %s", response.status_code)
            return None
        current = (response.json() or {}).get("current") or {}
    except Exception as e:
        logger.warning("读取 Open-Meteo 天气失败: %s", e)
        return None

    code = current.get("weather_code")
    condition, text = WMO_CONDITIONS.get(int(code) if code is not None else -1, ("", ""))
    return {
        "source": "openmeteo",
        "entity_id": "",
        "location": location_name or DEFAULT_LOCATION["name"],
        "condition": condition,
        "condition_text": text,
        "temperature_c": _to_float(current.get("temperature_2m")),
        "humidity": _to_float(current.get("relative_humidity_2m")),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


class WeatherService:
    """按配置取天气，带缓存；HA 来源直接复用采集时已拿到的实体列表。"""

    def __init__(self, config_loader):
        self.load_config = config_loader
        self.lock = threading.Lock()
        self.cache = None
        self.cache_time = 0.0

    def current(self, states: list = None) -> Optional[dict]:
        config = self.load_config() or {}
        source = (config.get("source") or "auto").lower()
        if source == "off":
            return None

        if source in ("auto", "ha"):
            weather = from_ha_states(states or [], config.get("entity_id") or "")
            if weather:
                with self.lock:
                    self.cache = weather
                    self.cache_time = time.time()
                return weather
            if source == "ha":
                # 指定了用 HA 但实体读不到，退回缓存，避免历史记录断档
                return self._cached()

        with self.lock:
            fresh = (time.time() - self.cache_time) < OPEN_METEO_TTL
            if fresh and self.cache and self.cache.get("source") == "openmeteo":
                return self.cache

        weather = from_open_meteo(
            _to_float(config.get("latitude")) or DEFAULT_LOCATION["latitude"],
            _to_float(config.get("longitude")) or DEFAULT_LOCATION["longitude"],
            config.get("location_name") or DEFAULT_LOCATION["name"],
        )
        if weather:
            with self.lock:
                self.cache = weather
                self.cache_time = time.time()
            return weather
        return self._cached()

    def _cached(self) -> Optional[dict]:
        with self.lock:
            if not self.cache:
                return None
            weather = dict(self.cache)
        if time.time() - self.cache_time > STALE_AFTER:
            weather["stale"] = True
        return weather
