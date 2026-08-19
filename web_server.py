# -*- coding: utf-8 -*-
"""
家庭用电监控 - Web 服务器

提供实时监控页面、HA 设备扫描导入、多插座数据查询与开关控制。
"""

import json
import os
import socket
import threading
import time
from datetime import datetime
from urllib.parse import quote

from flask import Flask, Response, jsonify, render_template, request
from flask_socketio import SocketIO, emit

import ha_client
from config_store import (
    DATA_DIR,
    STATE_FILE,
    load_devices,
    load_ha_config,
    load_mqtt_config,
    load_mysql_config,
    remove_device,
    save_devices,
    save_ha_config,
    save_mqtt_config,
    save_mysql_config,
    upsert_devices,
)
import reports
from ha_client import HAClient, HAError
from storage import CSVStore, read_day_records, read_month_records

WEB_CONFIG = {
    "host": os.environ.get("WEB_HOST", "0.0.0.0"),
    "port": int(os.environ.get("WEB_PORT", "5000")),
    "debug": False,
}

app = Flask(__name__)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# 扫描结果缓存，避免前端反复点击时把 HA 打满
_discovery_cache = {"time": 0.0, "devices": [], "states": []}
_discovery_lock = threading.Lock()
DISCOVERY_TTL = 20


@app.route("/")
def index():
    return render_template("index.html")


# =============================================================================
# 实时数据
# =============================================================================

def _read_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                return state
    except Exception as e:
        print("读取状态文件失败: %s" % e)
    return {}


def _realtime_payload() -> dict:
    state = _read_state()
    device_states = state.get("devices") if isinstance(state.get("devices"), dict) else {}
    aggregate = state.get("aggregate") or {
        "current_power_w": state.get("current_power_w", 0.0),
        "total_energy_kwh": state.get("total_energy_kwh", 0.0),
        "today_energy_kwh": state.get("today_energy_kwh", 0.0),
        "month_energy_kwh": state.get("month_energy_kwh", 0.0),
    }

    devices = []
    for config in load_devices():
        runtime = device_states.get(config["key"]) or {}
        devices.append({
            "key": config["key"],
            "name": config.get("name") or config["key"],
            "enabled": config.get("enabled", True),
            "model": config.get("model", ""),
            "manufacturer": config.get("manufacturer", ""),
            "energy_mode": config.get("energy_mode", "auto"),
            "entities": config.get("entities", {}),
            "decoders": config.get("decoders", {}),
            "has_switch": bool((config.get("entities") or {}).get("switch")),
            "available": bool(runtime.get("available")),
            "power_w": float(runtime.get("current_power_w") or 0.0),
            "total_energy_kwh": float(runtime.get("total_energy_kwh") or 0.0),
            "today_energy_kwh": float(runtime.get("today_energy_kwh") or 0.0),
            "month_energy_kwh": float(runtime.get("month_energy_kwh") or 0.0),
            "voltage_v": runtime.get("voltage_v"),
            "current_a": runtime.get("current_a"),
            "current_derived": bool(runtime.get("current_derived")),
            "device_total_kwh": runtime.get("device_total_kwh"),
            "total_basis": runtime.get("total_basis", "accumulated"),
            "power_factor": runtime.get("power_factor"),
            "frequency_hz": runtime.get("frequency_hz"),
            "switch_state": runtime.get("switch_state"),
            "energy_source": runtime.get("energy_source", "integrate"),
            "period_source": runtime.get("period_source", "computed"),
            "last_update": runtime.get("last_update"),
        })

    return {
        # 合计（老字段名保留，方便外部脚本继续用）
        "power_w": float(aggregate.get("current_power_w") or 0.0),
        "total_energy_kwh": float(aggregate.get("total_energy_kwh") or 0.0),
        "today_energy_kwh": float(aggregate.get("today_energy_kwh") or 0.0),
        "month_energy_kwh": float(aggregate.get("month_energy_kwh") or 0.0),
        "device_count": len(devices),
        "online_count": sum(1 for d in devices if d["available"]),
        "devices": devices,
        "last_update": state.get("last_update") or datetime.now().isoformat(),
    }


@app.route("/api/realtime")
def get_realtime_data():
    return jsonify(_realtime_payload())


# =============================================================================
# 历史数据
# =============================================================================

def _aggregate_records(records: list) -> list:
    """把同一时刻的多设备记录合并成一条合计记录。"""
    buckets = {}
    for record in records:
        bucket = buckets.setdefault(record["timestamp"], {
            "timestamp": record["timestamp"],
            "power_w": 0.0,
            "total_energy_kwh": 0.0,
            "device_count": 0,
        })
        bucket["power_w"] += record.get("power_w") or 0.0
        bucket["total_energy_kwh"] += record.get("total_energy_kwh") or 0.0
        bucket["device_count"] += 1
    return [buckets[k] for k in sorted(buckets)]


@app.route("/api/history")
def get_history_data():
    date_str = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    device_key = (request.args.get("device") or "").strip()
    if device_key in ("all", "__all__"):
        device_key = ""
    try:
        query_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "日期格式错误，请使用 YYYY-MM-DD"}), 400

    records = read_day_records(query_date, device_key or None)
    devices_in_day = []
    seen = set()
    for record in records:
        key = record.get("device_key")
        if key and key not in seen:
            seen.add(key)
            devices_in_day.append({"key": key, "name": record.get("device_name") or key})

    if device_key:
        rows = [
            {
                "timestamp": r["timestamp"],
                "power_w": r.get("power_w") or 0.0,
                "total_energy_kwh": r.get("total_energy_kwh") or 0.0,
                "voltage_v": r.get("voltage_v"),
                "current_a": r.get("current_a"),
            }
            for r in records
        ]
    else:
        rows = _aggregate_records(records)

    return jsonify({
        "date": date_str,
        "device": device_key,
        "devices": devices_in_day,
        "records": rows,
        "count": len(rows),
    })


@app.route("/api/dates")
def get_available_dates():
    year = request.args.get("year", datetime.now().year, type=int)
    month = request.args.get("month", datetime.now().month, type=int)
    return jsonify({"dates": CSVStore(DATA_DIR).available_dates(year, month)})


@app.route("/api/export")
def export_csv():
    """导出报表：明细 / 分时 / 每日，中文表头，Excel 直接能开。"""
    report_type = (request.args.get("type") or "detail").strip()
    if report_type not in reports.REPORT_TYPES:
        return jsonify({"error": "未知的报表类型"}), 400

    device_key = (request.args.get("device") or "").strip()
    if device_key in ("all", "__all__"):
        device_key = ""

    try:
        interval = max(0, int(request.args.get("interval") or 0))
    except (TypeError, ValueError):
        interval = 0
    try:
        price = max(0.0, float(request.args.get("price") or 0))
    except (TypeError, ValueError):
        price = 0.0

    date_str = (request.args.get("date") or "").strip()
    month_str = (request.args.get("month") or "").strip()

    if report_type == "daily" or (month_str and not date_str):
        source = month_str or date_str or datetime.now().strftime("%Y-%m")
        try:
            year, month = int(source[:4]), int(source[5:7])
            datetime(year, month, 1)
        except (TypeError, ValueError):
            return jsonify({"error": "月份格式错误，请使用 YYYY-MM"}), 400
        records = read_month_records(year, month, device_key or None)
        scope = "%04d-%02d" % (year, month)
    else:
        source = date_str or datetime.now().strftime("%Y-%m-%d")
        try:
            query_date = datetime.strptime(source[:10], "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "日期格式错误，请使用 YYYY-MM-DD"}), 400
        records = read_day_records(query_date, device_key or None)
        scope = query_date.strftime("%Y-%m-%d")

    if not records:
        return jsonify({"error": "该时间范围没有数据"}), 404

    body = reports.build_csv(report_type, records, interval=interval, price=price)
    device_name = ""
    if device_key:
        device_name = next(
            (d["name"] for d in load_devices() if d["key"] == device_key), device_key
        )

    filename = "%s_%s%s.csv" % (
        reports.REPORT_LABELS[report_type],
        scope,
        ("_" + device_name) if device_name else "",
    )
    # 带 BOM，否则 Excel 打开中文是乱码
    payload = ("﻿" + body).encode("utf-8")
    ascii_name = quote(filename)
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=export.csv; filename*=UTF-8''%s" % ascii_name,
            "Content-Length": str(len(payload)),
        },
    )


# =============================================================================
# HA 连接配置（只要地址 + 令牌）
# =============================================================================

def _client_from_request(data: dict) -> HAClient:
    """优先用表单里填的，令牌留空则沿用已保存的。"""
    current = load_ha_config()
    url = (data.get("url") or "").strip() or current.get("url", "")
    token = (data.get("token") or "").strip() or current.get("token", "")
    return HAClient(url, token)


@app.route("/api/config/ha", methods=["GET"])
def get_ha_config_api():
    config = load_ha_config()
    return jsonify({
        "url": config.get("url", ""),
        "token_set": bool(config.get("token")),
    })


@app.route("/api/config/ha", methods=["POST"])
def save_ha_config_api():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "HA 地址不能为空"}), 400
    current = load_ha_config()
    token = (data.get("token") or "").strip() or current.get("token", "")
    if not token:
        return jsonify({"error": "请填写访问令牌"}), 400
    try:
        save_ha_config(data)
        with _discovery_lock:
            _discovery_cache["time"] = 0.0
        return jsonify({"success": True, "message": "已保存，现在可以扫描插座设备了"})
    except Exception as e:
        return jsonify({"error": "保存失败: %s" % e}), 500


@app.route("/api/config/ha/test", methods=["POST"])
def test_ha_connection():
    data = request.get_json(silent=True) or {}
    client = _client_from_request(data)
    if not client.url:
        return jsonify({"error": "HA 地址不能为空"}), 400
    if not client.token:
        return jsonify({"error": "访问令牌不能为空"}), 400
    try:
        info = client.api_info()
        return jsonify({
            "success": True,
            "message": "连接成功！HA 版本: %s" % (info.get("version") or "未知"),
        })
    except HAError as e:
        return jsonify({"error": str(e)})


# =============================================================================
# 设备扫描与导入
# =============================================================================

def _discover(force: bool = False, data: dict = None):
    """扫描 HA 里的计量插座，20 秒内复用缓存。"""
    with _discovery_lock:
        fresh = (time.time() - _discovery_cache["time"]) < DISCOVERY_TTL
        if fresh and not force and _discovery_cache["devices"]:
            return _discovery_cache["devices"], _discovery_cache["states"]

        client = _client_from_request(data or {})
        states = client.get_states()
        devices = ha_client.discover_devices(client, states)
        _discovery_cache.update({"time": time.time(), "devices": devices, "states": states})
        return devices, states


def _new_roles(discovered: dict, saved: dict) -> list:
    """已导入的插座这次扫描又多认出哪些参数（比如新支持的小米解包）。"""
    saved_entities = saved.get("entities") or {}
    saved_decoders = saved.get("decoders") or {}
    new_roles = []

    for role, entity_id in (discovered.get("entities") or {}).items():
        if saved_entities.get(role) != entity_id and not saved_decoders.get(role):
            new_roles.append(role)

    for role, decoder in (discovered.get("decoders") or {}).items():
        if saved_entities.get(role):
            continue
        current = saved_decoders.get(role) or {}
        if (current.get("entity_id"), current.get("spec")) != (decoder.get("entity_id"), decoder.get("spec")):
            new_roles.append(role)

    order = list(ha_client.ROLE_LABELS.keys())
    return sorted(set(new_roles), key=lambda r: order.index(r) if r in order else 99)


@app.route("/api/ha/discover", methods=["GET", "POST"])
def discover_ha_devices():
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force")) or request.args.get("force") == "1"
    try:
        devices, _states = _discover(force=force, data=data)
    except HAError as e:
        return jsonify({"error": str(e)}), 400

    configured = {d["key"]: d for d in load_devices()}
    result = []
    for device in devices:
        item = dict(device)
        saved = configured.get(device["key"])
        item["added"] = saved is not None
        item["new_roles"] = _new_roles(device, saved) if saved else []
        item["needs_update"] = bool(item["new_roles"])
        result.append(item)

    return jsonify({
        "devices": result,
        "count": len(result),
        "added_count": sum(1 for d in result if d["added"]),
        "update_count": sum(1 for d in result if d["needs_update"]),
        "role_labels": ha_client.ROLE_LABELS,
    })


@app.route("/api/ha/entities")
def list_ha_entities():
    """给「手动改绑实体」的下拉框用。"""
    role = (request.args.get("role") or "power").strip()
    if role not in ha_client.ALL_ROLES:
        return jsonify({"error": "未知的实体类型: %s" % role}), 400
    try:
        _devices, states = _discover()
    except HAError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"role": role, "entities": ha_client.list_entities_by_role(states, role)})


@app.route("/api/devices", methods=["GET"])
def get_devices_api():
    payload = _realtime_payload()
    return jsonify({"devices": payload["devices"], "role_labels": ha_client.ROLE_LABELS})


@app.route("/api/devices", methods=["POST"])
def save_devices_api():
    data = request.get_json(silent=True) or {}
    raw_devices = data.get("devices")
    if not isinstance(raw_devices, list):
        return jsonify({"error": "参数格式错误"}), 400
    try:
        devices = save_devices(raw_devices)
        return jsonify({"success": True, "devices": devices, "message": "已保存 %d 个插座" % len(devices)})
    except Exception as e:
        return jsonify({"error": "保存失败: %s" % e}), 500


@app.route("/api/devices/import", methods=["POST"])
def import_devices_api():
    """勾选扫描结果后一键导入：功率 / 电量 / 电压等实体自动绑定。"""
    data = request.get_json(silent=True) or {}
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        return jsonify({"error": "请先勾选要导入的插座"}), 400

    try:
        devices, _states = _discover(data=data)
    except HAError as e:
        return jsonify({"error": str(e)}), 400

    by_key = {d["key"]: d for d in devices}
    selected = []
    missing = []
    for key in keys:
        device = by_key.get(key)
        if not device:
            missing.append(key)
            continue
        selected.append({
            "key": device["key"],
            "name": device.get("name") or device["key"],
            "model": device.get("model", ""),
            "manufacturer": device.get("manufacturer", ""),
            "entities": device.get("entities", {}),
            "decoders": device.get("decoders", {}),
            "energy_mode": "auto",
            "enabled": True,
        })

    if not selected:
        return jsonify({"error": "所选设备已不在扫描结果里，请重新扫描"}), 400

    saved = upsert_devices(selected)
    message = "已导入 %d 个插座，采集程序会在下个周期自动生效" % len(selected)
    if missing:
        message += "（%d 个未找到已跳过）" % len(missing)
    return jsonify({"success": True, "devices": saved, "message": message})


@app.route("/api/devices/<path:key>", methods=["DELETE"])
def delete_device_api(key):
    devices = remove_device(key)
    return jsonify({"success": True, "devices": devices, "message": "已移除该插座"})


# =============================================================================
# 插座开关（直接调用 HA 服务）
# =============================================================================

@app.route("/api/switch", methods=["POST"])
def control_switch():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    device_key = (data.get("device_key") or data.get("switch_id") or "").strip()
    if action not in ("on", "off"):
        return jsonify({"error": "无效的操作"}), 400

    device = next((d for d in load_devices() if d["key"] == device_key), None)
    if not device:
        return jsonify({"error": "未找到该插座"}), 404

    entity_id = (device.get("entities") or {}).get("switch")
    if not entity_id:
        return jsonify({"error": "该插座没有绑定开关实体"}), 400

    ha_config = load_ha_config()
    client = HAClient(ha_config.get("url"), ha_config.get("token"))
    domain = entity_id.split(".")[0]
    service = "turn_on" if action == "on" else "turn_off"
    try:
        client.call_service(domain, service, {"entity_id": entity_id})
        return jsonify({"success": True, "action": action, "device_key": device_key})
    except HAError as e:
        return jsonify({"error": str(e)}), 502


# =============================================================================
# MQTT / MySQL 配置
# =============================================================================

@app.route("/api/config/mqtt", methods=["GET"])
def get_mqtt_config_api():
    config = load_mqtt_config()
    return jsonify({
        "broker": config.get("broker", ""),
        "port": config.get("port", 1883),
        "username": config.get("username", ""),
        "password_set": bool(config.get("password")),
        "client_id": config.get("client_id", "home_electricity_monitor"),
    })


@app.route("/api/config/mqtt", methods=["POST"])
def save_mqtt_config_api():
    data = request.get_json(silent=True) or {}
    try:
        save_mqtt_config(data)
        return jsonify({"success": True, "message": "配置已保存，重启程序后生效"})
    except Exception as e:
        return jsonify({"error": "保存失败: %s" % e}), 500


@app.route("/api/config/mqtt/test", methods=["POST"])
def test_mqtt_connection():
    data = request.get_json(silent=True) or {}
    current = load_mqtt_config()
    broker = (data.get("broker") or "").strip()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip() or current.get("password", "")
    try:
        port = int(data.get("port") or 1883)
    except (TypeError, ValueError):
        port = 1883

    if not broker:
        return jsonify({"error": "服务器地址不能为空"}), 400

    try:
        import paho.mqtt.client as mqtt

        socket.create_connection((broker, port), timeout=5).close()
        connected = {"ok": False, "rc": None}

        def on_connect(client, userdata, flags, rc):
            connected["ok"] = rc == 0
            connected["rc"] = rc
            client.disconnect()

        test_client = mqtt.Client(client_id="hapowerstats_test", protocol=mqtt.MQTTv311)
        test_client.on_connect = on_connect
        if username:
            test_client.username_pw_set(username, password)
        test_client.connect(broker, port, keepalive=30)
        test_client.loop_start()
        deadline = time.time() + 5
        while time.time() < deadline and connected["rc"] is None:
            time.sleep(0.1)
        test_client.loop_stop()
        try:
            test_client.disconnect()
        except Exception:
            pass

        if connected["ok"]:
            return jsonify({"success": True, "message": "连接成功"})
        if connected["rc"] == 4:
            return jsonify({"error": "用户名或密码错误"})
        if connected["rc"] is None:
            return jsonify({"error": "MQTT 握手超时"})
        return jsonify({"error": "连接失败，返回码: %s" % connected["rc"]})
    except TimeoutError:
        return jsonify({"error": "连接超时，请检查服务器地址和端口"})
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower():
            return jsonify({"error": "连接超时，请检查服务器地址和端口"})
        if "refused" in error_msg.lower():
            return jsonify({"error": "连接被拒绝，请检查服务器是否运行"})
        return jsonify({"error": "连接失败: %s" % error_msg})


@app.route("/api/config/mysql", methods=["GET"])
def get_mysql_config_api():
    config = load_mysql_config()
    return jsonify({
        "host": config.get("host", ""),
        "port": config.get("port", 3306),
        "database": config.get("database", "electricity_monitor"),
        "user": config.get("user", ""),
        "password_set": bool(config.get("password")),
    })


@app.route("/api/config/mysql", methods=["POST"])
def save_mysql_config_api():
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    database = (data.get("database") or "electricity_monitor").strip()
    user = (data.get("user") or "").strip()
    if not host or not database or not user:
        return jsonify({"error": "请填写数据库地址、数据库名和用户名"}), 400
    try:
        save_mysql_config(data)
        return jsonify({"success": True, "message": "配置已保存，重启程序后生效"})
    except Exception as e:
        return jsonify({"error": "保存失败: %s" % e}), 500


@app.route("/api/config/mysql/test", methods=["POST"])
def test_mysql_connection():
    data = request.get_json(silent=True) or {}
    current = load_mysql_config()
    host = (data.get("host") or "").strip()
    database = (data.get("database") or "").strip()
    user = (data.get("user") or "").strip()
    password = (data.get("password") or "").strip() or current.get("password", "")
    try:
        port = int(data.get("port") or 3306)
    except (TypeError, ValueError):
        port = 3306

    if not host or not database or not user:
        return jsonify({"error": "请填写完整信息"}), 400

    try:
        import pymysql

        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            connect_timeout=5,
        )
        conn.close()
        return jsonify({"success": True, "message": "连接成功！"})
    except ImportError:
        return jsonify({"error": "请先安装 pymysql: pip install pymysql"})
    except Exception as e:
        error_msg = str(e)
        if "Unknown database" in error_msg:
            return jsonify({"error": "数据库 '%s' 不存在，请先手动创建" % database})
        if "Access denied" in error_msg:
            return jsonify({"error": "用户名或密码错误"})
        if "Connection refused" in error_msg:
            return jsonify({"error": "连接被拒绝，请检查 MySQL 是否运行"})
        return jsonify({"error": "连接失败: %s" % error_msg})


# =============================================================================
# WebSocket
# =============================================================================

@socketio.on("connect")
def handle_connect():
    emit("data_update", _realtime_payload())


@socketio.on("request_data")
def handle_request_data():
    emit("data_update", _realtime_payload())


def start_web_server():
    print("Web 服务器启动: http://localhost:%s" % WEB_CONFIG["port"])
    socketio.run(
        app,
        host=WEB_CONFIG["host"],
        port=WEB_CONFIG["port"],
        debug=WEB_CONFIG["debug"],
        allow_unsafe_werkzeug=True,
    )


def run_web_server_thread():
    web_thread = threading.Thread(target=start_web_server, daemon=True)
    web_thread.start()
    return web_thread


if __name__ == "__main__":
    start_web_server()
