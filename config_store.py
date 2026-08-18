# -*- coding: utf-8 -*-
"""统一的配置与数据路径，避免主程序和 Web 各读各的。"""

import json
import os
import secrets
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "config"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "electricity_data"))

HA_CONFIG_FILE = CONFIG_DIR / "ha_config.json"
MQTT_CONFIG_FILE = CONFIG_DIR / "mqtt_config.json"
MYSQL_CONFIG_FILE = CONFIG_DIR / "mysql_config.json"
WEB_AUTH_FILE = CONFIG_DIR / "web_auth.json"
SECRET_KEY_FILE = CONFIG_DIR / "secret_key"
STATE_FILE = DATA_DIR / "electricity_state.json"

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
    "power_entity_id": "",
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
    return load_json(HA_CONFIG_FILE, DEFAULT_HA)


def save_ha_config(data: dict) -> dict:
    current = load_ha_config()
    config = {
        "url": (data.get("url") or "").strip().rstrip("/"),
        "token": keep_secret(data.get("token"), current.get("token")),
        "power_entity_id": (data.get("power_entity_id") or "").strip(),
    }
    if config["url"] and not config["url"].startswith("http"):
        config["url"] = "http://" + config["url"]
    save_json(HA_CONFIG_FILE, config)
    return config


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


def get_or_create_secret_key() -> str:
    env_key = (os.environ.get("FLASK_SECRET_KEY") or "").strip()
    if env_key:
        return env_key
    ensure_dirs()
    if SECRET_KEY_FILE.exists():
        stored = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    key = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(key, encoding="utf-8")
    return key
