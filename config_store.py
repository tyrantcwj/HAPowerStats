# -*- coding: utf-8 -*-
"""统一的配置与数据路径，避免主程序和 Web 各读各的。"""

import json
import os
import re
from pathlib import Path

import mi_decode

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "config"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "electricity_data"))

HA_CONFIG_FILE = CONFIG_DIR / "ha_config.json"
DEVICES_FILE = CONFIG_DIR / "devices.json"
MQTT_CONFIG_FILE = CONFIG_DIR / "mqtt_config.json"
MYSQL_CONFIG_FILE = CONFIG_DIR / "mysql_config.json"
STATE_FILE = DATA_DIR / "electricity_state.json"

# 一个插座可绑定的实体角色（与 ha_client.ALL_ROLES 保持一致）
DEVICE_ROLES = (
    "power",
    "energy",
    "voltage",
    "current",
    "power_factor",
    "frequency",
    "apparent_power",
    "switch",
)

# 可由小米打包寄存器解码出来的角色（没有独立实体）
DECODER_ROLES = ("power", "voltage", "today_energy", "month_energy")

# 电量统计方式：auto=有电量实体就用、没有就按功率积分
ENERGY_MODES = ("auto", "entity", "integrate")

LEGACY_DEVICE_KEY = "legacy_main"

DEFAULT_MQTT = {
    "broker": "",
    "port": 1883,
    "username": "",
    "password": "",
    "client_id": "home_electricity_monitor",
}

DEFAULT_HA = {
    "url": "",
    "token": "",
}

DEFAULT_MYSQL = {
    "host": "",
    "port": 3306,
    "database": "electricity_monitor",
    "user": "",
    "password": "",
}


def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: dict) -> dict:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(default)
            if isinstance(data, dict):
                merged.update(data)
            return merged
    except Exception:
        pass
    return dict(default)


def save_json(path: Path, data: dict):
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def keep_secret(new_value, old_value) -> str:
    """表单留空时保留原密码/令牌。"""
    if new_value is None:
        return old_value or ""
    text = str(new_value).strip()
    if text:
        return text
    return old_value or ""


def load_ha_config() -> dict:
    """HA 连接配置：只有地址和令牌，具体设备在 devices.json 里。"""
    config = load_json(HA_CONFIG_FILE, DEFAULT_HA)
    return {
        "url": (config.get("url") or "").strip(),
        "token": config.get("token") or "",
    }


def save_ha_config(data: dict) -> dict:
    current = load_ha_config()
    url = (data.get("url") or "").strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "http://" + url
    config = {
        "url": url,
        "token": keep_secret(data.get("token"), current.get("token")),
    }
    save_json(HA_CONFIG_FILE, config)
    return config


# =============================================================================
# 插座设备列表（支持多个）
# =============================================================================

def _slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", str(text or "")).strip("_").lower()
    return slug or fallback


def normalize_device(raw: dict, index: int = 0) -> dict:
    """把前端/发现结果里的一条设备整理成统一结构，非法项返回 None。"""
    if not isinstance(raw, dict):
        return None

    entities = {}
    raw_entities = raw.get("entities") or {}
    if isinstance(raw_entities, dict):
        for role in DEVICE_ROLES:
            entity_id = (raw_entities.get(role) or "").strip()
            if entity_id:
                entities[role] = entity_id

    # 小米插座的打包寄存器：{角色: {entity_id, spec}}
    decoders = {}
    raw_decoders = raw.get("decoders") or {}
    if isinstance(raw_decoders, dict):
        for role in DECODER_ROLES:
            item = raw_decoders.get(role)
            if not isinstance(item, dict):
                continue
            entity_id = (item.get("entity_id") or "").strip()
            spec = (item.get("spec") or "").strip()
            if entity_id and spec in mi_decode.DECODER_LABELS:
                decoders[role] = {"entity_id": entity_id, "spec": spec}

    has_entity_data = bool(entities.get("power") or entities.get("energy"))
    has_decoded_data = bool(decoders.get("power") or decoders.get("today_energy"))
    if not has_entity_data and not has_decoded_data:
        # 既没功率也没电量，采集不到任何有效数据
        return None

    key = (raw.get("key") or "").strip()
    if not key:
        base = (entities.get("power") or entities.get("energy")
                or (decoders.get("power") or {}).get("entity_id") or "device")
        key = _slugify(base.split(".", 1)[-1], "device_%d" % (index + 1))

    energy_mode = (raw.get("energy_mode") or "auto").strip().lower()
    if energy_mode not in ENERGY_MODES:
        energy_mode = "auto"

    name = (raw.get("name") or "").strip() or key
    return {
        "key": key,
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
        "model": (raw.get("model") or "").strip(),
        "manufacturer": (raw.get("manufacturer") or "").strip(),
        "energy_mode": energy_mode,
        "entities": entities,
        "decoders": decoders,
    }


def _migrate_legacy_device() -> list:
    """老版本只配了一个 power_entity_id，自动转成一个设备。"""
    raw = load_json(HA_CONFIG_FILE, {})
    entity_id = (raw.get("power_entity_id") or "").strip()
    if not entity_id:
        return []
    device = normalize_device({
        "key": LEGACY_DEVICE_KEY,
        "name": "默认功率实体",
        "entities": {"power": entity_id},
    })
    devices = [device] if device else []
    if devices:
        save_json(DEVICES_FILE, {"devices": devices})
    return devices


def load_devices() -> list:
    """读取已配置的插座设备列表。"""
    if not DEVICES_FILE.exists():
        return _migrate_legacy_device()

    data = load_json(DEVICES_FILE, {"devices": []})
    raw_list = data.get("devices")
    if not isinstance(raw_list, list):
        return []

    devices = []
    seen = set()
    for index, raw in enumerate(raw_list):
        device = normalize_device(raw, index)
        if not device or device["key"] in seen:
            continue
        seen.add(device["key"])
        devices.append(device)
    return devices


def save_devices(raw_list) -> list:
    """整体覆盖保存设备列表。"""
    devices = []
    seen = set()
    for index, raw in enumerate(raw_list or []):
        device = normalize_device(raw, index)
        if not device or device["key"] in seen:
            continue
        seen.add(device["key"])
        devices.append(device)
    save_json(DEVICES_FILE, {"devices": devices})
    return devices


def upsert_devices(new_devices) -> list:
    """按 key 合并进已有列表：已存在的更新实体映射，不存在的追加。"""
    devices = load_devices()
    by_key = {d["key"]: d for d in devices}
    order = [d["key"] for d in devices]

    for index, raw in enumerate(new_devices or []):
        device = normalize_device(raw, index)
        if not device:
            continue
        existing = by_key.get(device["key"])
        if existing:
            # 保留用户改过的名字和启用状态
            device["name"] = existing.get("name") or device["name"]
            device["enabled"] = existing.get("enabled", True)
            device["energy_mode"] = existing.get("energy_mode", device["energy_mode"])
        else:
            order.append(device["key"])
        by_key[device["key"]] = device

    return save_devices([by_key[key] for key in order])


def remove_device(key: str) -> list:
    devices = [d for d in load_devices() if d["key"] != key]
    return save_devices(devices)


def devices_signature(devices) -> str:
    """设备配置指纹，主程序据此感知 Web 端改动并热加载。"""
    payload = [
        {
            "key": d.get("key"),
            "name": d.get("name"),
            "enabled": d.get("enabled"),
            "energy_mode": d.get("energy_mode"),
            "entities": d.get("entities"),
            "decoders": d.get("decoders"),
        }
        for d in devices or []
    ]
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def load_mqtt_config() -> dict:
    config = load_json(MQTT_CONFIG_FILE, DEFAULT_MQTT)
    try:
        config["port"] = int(config.get("port") or 1883)
    except (TypeError, ValueError):
        config["port"] = 1883
    return config


def save_mqtt_config(data: dict) -> dict:
    current = load_mqtt_config()
    try:
        port = int(data.get("port") or 1883)
    except (TypeError, ValueError):
        port = 1883
    config = {
        "broker": (data.get("broker") or "").strip(),
        "port": port,
        "username": (data.get("username") or "").strip(),
        "password": keep_secret(data.get("password"), current.get("password")),
        "client_id": (data.get("client_id") or "home_electricity_monitor").strip()
        or "home_electricity_monitor",
    }
    save_json(MQTT_CONFIG_FILE, config)
    return config


def load_mysql_config() -> dict:
    config = load_json(MYSQL_CONFIG_FILE, DEFAULT_MYSQL)
    try:
        config["port"] = int(config.get("port") or 3306)
    except (TypeError, ValueError):
        config["port"] = 3306
    return config


def save_mysql_config(data: dict) -> dict:
    current = load_mysql_config()
    try:
        port = int(data.get("port") or 3306)
    except (TypeError, ValueError):
        port = 3306
    config = {
        "host": (data.get("host") or "").strip(),
        "port": port,
        "database": (data.get("database") or "electricity_monitor").strip(),
        "user": (data.get("user") or "").strip(),
        "password": keep_secret(data.get("password"), current.get("password")),
    }
    save_json(MYSQL_CONFIG_FILE, config)
    return config

