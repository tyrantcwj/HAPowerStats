# -*- coding: utf-8 -*-
"""
家庭用电监控 - Web服务器
提供实时监控页面、历史数据查询、插座控制功能
"""

import csv
import json
import os
import socket
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_socketio import SocketIO, emit
from werkzeug.security import check_password_hash, generate_password_hash
import threading

from config_store import (
    DATA_DIR,
    STATE_FILE,
    WEB_AUTH_FILE,
    ensure_dirs,
    get_or_create_secret_key,
    load_ha_config,
    load_json,
    load_mqtt_config,
    load_mysql_config,
    save_ha_config,
    save_json,
    save_mqtt_config,
    save_mysql_config,
)

WEB_CONFIG = {
    "host": "0.0.0.0",
    "port": 5000,
    "debug": False,
}

PUBLIC_ENDPOINTS = {
    "login_page",
    "auth_status",
    "auth_login",
    "auth_setup",
}

app = Flask(__name__)
app.config["SECRET_KEY"] = get_or_create_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

latest_data = {
    "power_w": 0.0,
    "total_energy_kwh": 0.0,
    "today_energy_kwh": 0.0,
    "month_energy_kwh": 0.0,
    "last_update": None,
}


def _env_password() -> str:
    return (os.environ.get("WEB_PASSWORD") or "").strip()


def _auth_record() -> dict:
    return load_json(WEB_AUTH_FILE, {"password_hash": ""})


def password_configured() -> bool:
    if _env_password():
        return True
    return bool(_auth_record().get("password_hash"))


def verify_password(password: str) -> bool:
    env_password = _env_password()
    if env_password:
        return password == env_password
    password_hash = _auth_record().get("password_hash") or ""
    if not password_hash:
        return False
    return check_password_hash(password_hash, password)


def set_password(password: str):
    ensure_dirs()
    save_json(WEB_AUTH_FILE, {"password_hash": generate_password_hash(password)})


def is_authenticated() -> bool:
    return bool(session.get("authenticated"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)

    return wrapped


@app.before_request
def protect_routes():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if request.path.startswith("/static/"):
        return None
    if is_authenticated():
        return None
    if request.path.startswith("/api/") or request.path.startswith("/socket.io"):
        return jsonify({"error": "未登录"}), 401
    return redirect(url_for("login_page"))


@app.route("/login")
def login_page():
    if is_authenticated():
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/api/auth/status")
def auth_status():
    return jsonify({
        "authenticated": is_authenticated(),
        "needs_setup": not password_configured(),
        "password_from_env": bool(_env_password()),
    })


@app.route("/api/auth/setup", methods=["POST"])
def auth_setup():
    if password_configured():
        return jsonify({"error": "已设置过密码，请直接登录"}), 400
    data = request.get_json(silent=True) or {}
    password = (data.get("password") or "").strip()
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    set_password(password)
    session.clear()
    session["authenticated"] = True
    session.permanent = True
    return jsonify({"success": True})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if not password_configured():
        return jsonify({"error": "请先设置登录密码"}), 400
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if not verify_password(password):
        return jsonify({"error": "密码错误"}), 401
    session.clear()
    session["authenticated"] = True
    session.permanent = True
    return jsonify({"success": True})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/realtime")
@login_required
def get_realtime_data():
    _update_latest_data()
    return jsonify(latest_data)


@app.route("/api/history")
@login_required
def get_history_data():
    date_str = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    try:
        query_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "日期格式错误，请使用 YYYY-MM-DD"}), 400

    records = _read_csv_records(query_date)
    return jsonify({
        "date": date_str,
        "records": records,
        "count": len(records),
    })


@app.route("/api/dates")
@login_required
def get_available_dates():
    year = request.args.get("year", datetime.now().year, type=int)
    month = request.args.get("month", datetime.now().month, type=int)
    csv_path = DATA_DIR / f"electricity_{year}-{month:02d}.csv"
    if not csv_path.exists():
        return jsonify({"dates": []})

    dates = set()
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                dates.add(row["timestamp"][:10])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"dates": sorted(dates)})


@app.route("/api/export")
@login_required
def export_csv():
    year = request.args.get("year", datetime.now().year, type=int)
    month = request.args.get("month", datetime.now().month, type=int)
    csv_path = DATA_DIR / f"electricity_{year}-{month:02d}.csv"
    if not csv_path.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(
        csv_path,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"electricity_{year}-{month:02d}.csv",
    )


@app.route("/api/config/mysql", methods=["GET"])
@login_required
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
@login_required
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
        return jsonify({"error": f"保存失败: {str(e)}"}), 500


@app.route("/api/config/mysql/test", methods=["POST"])
@login_required
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
            return jsonify({"error": f"数据库 '{database}' 不存在，请先手动创建"})
        if "Access denied" in error_msg:
            return jsonify({"error": "用户名或密码错误"})
        if "Connection refused" in error_msg:
            return jsonify({"error": "连接被拒绝，请检查MySQL是否运行"})
        return jsonify({"error": f"连接失败: {error_msg}"})


@app.route("/api/config/ha", methods=["GET"])
@login_required
def get_ha_config_api():
    config = load_ha_config()
    return jsonify({
        "url": config.get("url", ""),
        "token_set": bool(config.get("token")),
        "power_entity_id": config.get("power_entity_id", ""),
    })


@app.route("/api/config/ha", methods=["POST"])
@login_required
def save_ha_config_api():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    entity_id = (data.get("power_entity_id") or "").strip()
    if not url:
        return jsonify({"error": "HA地址不能为空"}), 400
    if not entity_id:
        return jsonify({"error": "请填写功率实体 ID"}), 400
    current = load_ha_config()
    token = (data.get("token") or "").strip() or current.get("token", "")
    if not token:
        return jsonify({"error": "请填写访问令牌"}), 400
    try:
        save_ha_config(data)
        return jsonify({"success": True, "message": "配置已保存，功率采集将使用新实体"})
    except Exception as e:
        return jsonify({"error": f"保存失败: {str(e)}"}), 500


@app.route("/api/config/ha/test", methods=["POST"])
@login_required
def test_ha_connection():
    import requests

    data = request.get_json(silent=True) or {}
    current = load_ha_config()
    url = (data.get("url") or "").strip().rstrip("/")
    token = (data.get("token") or "").strip() or current.get("token", "")
    entity_id = (data.get("power_entity_id") or "").strip() or current.get("power_entity_id", "")

    if not url:
        return jsonify({"error": "HA地址不能为空"}), 400
    if not token:
        return jsonify({"error": "访问令牌不能为空"}), 400
    if not url.startswith("http"):
        url = "http://" + url

    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        response = requests.get(f"{url}/api/", headers=headers, timeout=10)
        if response.status_code == 401:
            return jsonify({"error": "令牌无效，请检查访问令牌"})
        if response.status_code != 200:
            return jsonify({"error": f"连接失败，状态码: {response.status_code}"})

        ha_info = response.json()
        message = f"连接成功！HA版本: {ha_info.get('version', '未知')}"
        if entity_id:
            state_resp = requests.get(
                f"{url}/api/states/{entity_id}",
                headers=headers,
                timeout=10,
            )
            if state_resp.status_code == 404:
                return jsonify({"error": f"HA已连通，但实体不存在: {entity_id}"})
            if state_resp.status_code != 200:
                return jsonify({"error": f"HA已连通，但读取实体失败: HTTP {state_resp.status_code}"})
            state = state_resp.json().get("state")
            unit = (state_resp.json().get("attributes") or {}).get("unit_of_measurement", "")
            message += f"；实体 {entity_id} = {state} {unit}".strip()
        return jsonify({"success": True, "message": message})
    except requests.exceptions.Timeout:
        return jsonify({"error": "连接超时，请检查HA地址是否正确"})
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "无法连接，请检查HA是否运行，地址是否正确"})
    except Exception as e:
        return jsonify({"error": f"连接失败: {str(e)}"})


@app.route("/api/config/ha/entities")
@login_required
def list_ha_power_entities():
    import requests

    config = load_ha_config()
    url = (config.get("url") or "").strip().rstrip("/")
    token = config.get("token") or ""
    if not url or not token:
        return jsonify({"error": "请先保存 HA 地址和访问令牌"}), 400
    if not url.startswith("http"):
        url = "http://" + url

    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        response = requests.get(f"{url}/api/states", headers=headers, timeout=15)
        if response.status_code != 200:
            return jsonify({"error": f"读取实体失败: HTTP {response.status_code}"}), 502

        entities = []
        for item in response.json():
            entity_id = item.get("entity_id") or ""
            if not entity_id.startswith("sensor."):
                continue
            attrs = item.get("attributes") or {}
            unit = str(attrs.get("unit_of_measurement") or "").strip()
            device_class = str(attrs.get("device_class") or "").lower()
            unit_key = unit.lower().replace(" ", "")
            if device_class not in ("power",) and unit_key not in ("w", "kw", "mw", "watt", "kilowatt"):
                continue
            entities.append({
                "entity_id": entity_id,
                "name": attrs.get("friendly_name") or entity_id,
                "state": item.get("state"),
                "unit": unit,
            })
        entities.sort(key=lambda x: x["name"])
        return jsonify({"entities": entities})
    except Exception as e:
        return jsonify({"error": f"读取实体失败: {str(e)}"}), 502


@app.route("/api/config/mqtt", methods=["GET"])
@login_required
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
@login_required
def save_mqtt_config_api():
    data = request.get_json(silent=True) or {}
    try:
        save_mqtt_config(data)
        return jsonify({"success": True, "message": "配置已保存，重启程序后生效"})
    except Exception as e:
        return jsonify({"error": f"保存失败: {str(e)}"}), 500


@app.route("/api/config/mqtt/test", methods=["POST"])
@login_required
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
        return jsonify({"error": f"连接失败，返回码: {connected['rc']}"})
    except TimeoutError:
        return jsonify({"error": "连接超时，请检查服务器地址和端口"})
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower():
            return jsonify({"error": "连接超时，请检查服务器地址和端口"})
        if "refused" in error_msg.lower():
            return jsonify({"error": "连接被拒绝，请检查服务器是否运行"})
        return jsonify({"error": f"连接失败: {error_msg}"})


@app.route("/api/switch", methods=["POST"])
@login_required
def control_switch():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    switch_id = data.get("switch_id", "main")
    if action not in ["on", "off"]:
        return jsonify({"error": "无效的操作"}), 400
    success = _send_switch_command(switch_id, action)
    if success:
        return jsonify({"success": True, "action": action, "switch_id": switch_id})
    return jsonify({"error": "MQTT发送失败"}), 500


@socketio.on("connect")
def handle_connect():
    if not is_authenticated():
        return False
    _update_latest_data()
    emit("data_update", latest_data)


@socketio.on("request_data")
def handle_request_data():
    if not is_authenticated():
        return
    _update_latest_data()
    emit("data_update", latest_data)


def _update_latest_data():
    global latest_data
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            latest_data["power_w"] = state.get("current_power_w", 0.0)
            latest_data["total_energy_kwh"] = state.get("total_energy_kwh", 0.0)
            latest_data["today_energy_kwh"] = state.get("today_energy_kwh", 0.0)
            latest_data["month_energy_kwh"] = state.get("month_energy_kwh", 0.0)
            latest_data["last_update"] = datetime.now().isoformat()
    except Exception as e:
        print(f"读取状态文件失败: {e}")


def _read_csv_records(date) -> list:
    mysql_config = load_mysql_config()
    if mysql_config.get("host"):
        try:
            import pymysql

            conn = pymysql.connect(
                host=mysql_config["host"],
                port=mysql_config.get("port", 3306),
                user=mysql_config["user"],
                password=mysql_config.get("password", ""),
                database=mysql_config["database"],
                charset="utf8mb4",
            )
            cursor = conn.cursor()
            date_str = date[:10] if isinstance(date, str) else date.strftime("%Y-%m-%d")
            cursor.execute(
                """
                SELECT record_time, power_w, total_energy_kwh
                FROM electricity_records
                WHERE DATE(record_time) = %s
                ORDER BY record_time
                """,
                (date_str,),
            )
            records = []
            for row in cursor.fetchall():
                records.append({
                    "timestamp": row[0].strftime("%Y-%m-%d %H:%M:%S"),
                    "power_w": float(row[1]),
                    "total_energy_kwh": float(row[2]),
                })
            cursor.close()
            conn.close()
            if records:
                return records
        except Exception as e:
            print(f"MySQL读取失败，回退到CSV: {e}")

    csv_path = DATA_DIR / f"electricity_{date.year}-{date.month:02d}.csv"
    if not csv_path.exists():
        return []

    records = []
    date_str = date.strftime("%Y-%m-%d")
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["timestamp"].startswith(date_str):
                    records.append({
                        "timestamp": row["timestamp"],
                        "power_w": float(row["power_w"]),
                        "total_energy_kwh": float(row["total_energy_kwh"]),
                    })
    except Exception as e:
        print(f"读取CSV失败: {e}")
    return records


def _send_switch_command(switch_id: str, action: str) -> bool:
    try:
        import paho.mqtt.client as mqtt

        mqtt_config = load_mqtt_config()
        if not mqtt_config.get("broker"):
            print("未配置 MQTT，无法发送控制命令")
            return False

        client = mqtt.Client(client_id="web_control", protocol=mqtt.MQTTv311)
        if mqtt_config.get("username"):
            client.username_pw_set(mqtt_config["username"], mqtt_config.get("password"))
        client.connect(mqtt_config["broker"], mqtt_config["port"], keepalive=5)
        topic = f"home/switch/{switch_id}/set"
        payload = json.dumps({"state": action.upper()})
        client.publish(topic, payload, qos=1)
        client.disconnect()
        return True
    except Exception as e:
        print(f"发送控制命令失败: {e}")
        return False


def start_web_server():
    print(f"Web服务器启动: http://localhost:{WEB_CONFIG['port']}")
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
