# -*- coding: utf-8 -*-
"""
家庭用电监控 - Web服务器
提供实时监控页面、历史数据查询、插座控制功能
"""

import json
import os
import csv
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, jsonify, send_file, request
from flask_socketio import SocketIO, emit
import threading

# =============================================================================
# Web服务器配置
# =============================================================================

WEB_CONFIG = {
    "host": "0.0.0.0",      # 监听地址（0.0.0.0 允许外部访问）
    "port": 5000,            # 端口号
    "debug": False,          # 调试模式
    "secret_key": "electricity_monitor_secret_key"  # Flask密钥
}

# 配置文件路径
MQTT_CONFIG_FILE = Path("mqtt_config.json")
HA_CONFIG_FILE = Path("ha_config.json")
MYSQL_CONFIG_FILE = Path("mysql_config.json")

# =============================================================================
# 初始化Flask应用
# =============================================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = WEB_CONFIG['secret_key']

# 初始化SocketIO（用于实时推送数据）
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# 数据目录（与主程序一致）
DATA_DIR = Path("electricity_data")
STATE_FILE = Path("electricity_state.json")

# 全局变量：最新的实时数据
latest_data = {
    "power_w": 0.0,
    "total_energy_kwh": 0.0,
    "today_energy_kwh": 0.0,
    "month_energy_kwh": 0.0,
    "last_update": None
}

# =============================================================================
# 路由：主页
# =============================================================================

@app.route('/')
def index():
    """渲染主监控页面"""
    return render_template('index.html')

# =============================================================================
# API：获取实时数据
# =============================================================================

@app.route('/api/realtime')
def get_realtime_data():
    """获取当前实时数据"""
    # 尝试从状态文件读取最新数据
    _update_latest_data()
    return jsonify(latest_data)

# =============================================================================
# API：获取历史数据
# =============================================================================

@app.route('/api/history')
def get_history_data():
    """获取指定日期的历史数据"""
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    
    try:
        # 解析日期
        query_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({"error": "日期格式错误，请使用 YYYY-MM-DD"}), 400
    
    # 读取CSV数据
    records = _read_csv_records(query_date)
    
    return jsonify({
        "date": date_str,
        "records": records,
        "count": len(records)
    })

# =============================================================================
# API：获取指定月份的所有日期
# =============================================================================

@app.route('/api/dates')
def get_available_dates():
    """获取有数据的日期列表"""
    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    
    csv_path = DATA_DIR / f"electricity_{year}-{month:02d}.csv"
    
    if not csv_path.exists():
        return jsonify({"dates": []})
    
    dates = set()
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                date_str = row['timestamp'][:10]
                dates.add(date_str)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
    return jsonify({"dates": sorted(list(dates))})

# =============================================================================
# API：导出CSV文件
# =============================================================================

@app.route('/api/export')
def export_csv():
    """导出指定月份的CSV文件"""
    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    
    csv_path = DATA_DIR / f"electricity_{year}-{month:02d}.csv"
    
    if not csv_path.exists():
        return jsonify({"error": "文件不存在"}), 404
    
    return send_file(
        csv_path,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f"electricity_{year}-{month:02d}.csv"
    )

# =============================================================================
# API：MySQL配置
# =============================================================================

@app.route('/api/config/mysql', methods=['GET'])
def get_mysql_config():
    """获取MySQL配置"""
    config = _load_mysql_config()
    return jsonify({
        "host": config.get("host", ""),
        "port": config.get("port", 3306),
        "database": config.get("database", "electricity_monitor"),
        "user": config.get("user", ""),
        "password_set": bool(config.get("password"))
    })

@app.route('/api/config/mysql', methods=['POST'])
def save_mysql_config():
    """保存MySQL配置"""
    data = request.get_json()
    
    host = data.get('host', '').strip()
    port = data.get('port', 3306)
    database = data.get('database', 'electricity_monitor').strip()
    user = data.get('user', '').strip()
    password = data.get('password', '').strip()
    
    if not host or not database or not user:
        return jsonify({"error": "请填写数据库地址、数据库名和用户名"}), 400
    
    config = {
        "host": host,
        "port": int(port),
        "database": database,
        "user": user,
        "password": password
    }
    
    try:
        with open(MYSQL_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        return jsonify({"success": True, "message": "配置已保存，重启程序后生效"})
        
    except Exception as e:
        return jsonify({"error": f"保存失败: {str(e)}"}), 500

@app.route('/api/config/mysql/test', methods=['POST'])
def test_mysql_connection():
    """测试MySQL连接"""
    data = request.get_json()
    
    host = data.get('host', '').strip()
    port = int(data.get('port', 3306))
    database = data.get('database', '').strip()
    user = data.get('user', '').strip()
    password = data.get('password', '').strip()
    
    if not host or not database or not user:
        return jsonify({"error": "请填写完整信息"}), 400
    
    try:
        import pymysql
        
        # 测试连接
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset='utf8mb4',
            connect_timeout=5
        )
        conn.close()
        
        return jsonify({"success": True, "message": "连接成功！"})
        
    except ImportError:
        return jsonify({"error": "请先安装 pymysql: pip install pymysql"})
    except pymysql.err.OperationalError as e:
        error_msg = str(e)
        if "Unknown database" in error_msg:
            return jsonify({"error": f"数据库 '{database}' 不存在，请先手动创建"})
        elif "Access denied" in error_msg:
            return jsonify({"error": "用户名或密码错误"})
        elif "Connection refused" in error_msg:
            return jsonify({"error": "连接被拒绝，请检查MySQL是否运行"})
        else:
            return jsonify({"error": f"连接失败: {error_msg}"})
    except Exception as e:
        return jsonify({"error": f"连接失败: {str(e)}"})

def _load_mysql_config() -> dict:
    """加载MySQL配置"""
    try:
        if MYSQL_CONFIG_FILE.exists():
            with open(MYSQL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"加载MySQL配置失败: {e}")
    
    return {"host": "", "port": 3306, "database": "electricity_monitor", "user": "", "password": ""}

# =============================================================================
# API：HomeAssistant配置
# =============================================================================

@app.route('/api/config/ha', methods=['GET'])
def get_ha_config():
    """获取HA配置"""
    config = _load_ha_config()
    # 隐藏token
    safe_config = {
        "url": config.get("url", ""),
        "token_set": bool(config.get("token"))
    }
    return jsonify(safe_config)

@app.route('/api/config/ha', methods=['POST'])
def save_ha_config():
    """保存HA配置"""
    data = request.get_json()
    
    url = data.get('url', '').strip()
    token = data.get('token', '').strip()
    
    if not url:
        return jsonify({"error": "HA地址不能为空"}), 400
    
    # 保存到配置文件
    config = {
        "url": url,
        "token": token
    }
    
    try:
        with open(HA_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        return jsonify({"success": True, "message": "配置已保存，重启程序后生效"})
        
    except Exception as e:
        return jsonify({"error": f"保存失败: {str(e)}"}), 500

@app.route('/api/config/ha/test', methods=['POST'])
def test_ha_connection():
    """测试HA连接"""
    import requests
    
    data = request.get_json()
    
    url = data.get('url', '').strip()
    token = data.get('token', '').strip()
    
    if not url:
        return jsonify({"error": "HA地址不能为空"}), 400
    
    if not token:
        return jsonify({"error": "访问令牌不能为空"}), 400
    
    # 确保URL格式正确
    if not url.startswith('http'):
        url = 'http://' + url
    url = url.rstrip('/')
    
    try:
        # 测试HA连接
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(f"{url}/api/", headers=headers, timeout=10)
        
        if response.status_code == 200:
            ha_info = response.json()
            return jsonify({
                "success": True, 
                "message": f"连接成功！HA版本: {ha_info.get('version', '未知')}"
            })
        elif response.status_code == 401:
            return jsonify({"error": "令牌无效，请检查访问令牌"})
        else:
            return jsonify({"error": f"连接失败，状态码: {response.status_code}"})
            
    except requests.exceptions.Timeout:
        return jsonify({"error": "连接超时，请检查HA地址是否正确"})
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "无法连接，请检查HA是否运行，地址是否正确"})
    except Exception as e:
        return jsonify({"error": f"连接失败: {str(e)}"})

def _load_ha_config() -> dict:
    """加载HA配置"""
    try:
        if HA_CONFIG_FILE.exists():
            with open(HA_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"加载HA配置失败: {e}")
    
    return {"url": "", "token": ""}

# =============================================================================
# API：MQTT配置
# =============================================================================

@app.route('/api/config/mqtt', methods=['GET'])
def get_mqtt_config():
    """获取MQTT配置"""
    config = _load_mqtt_config()
    # 隐藏密码，只返回是否已设置
    safe_config = {
        "broker": config.get("broker", ""),
        "port": config.get("port", 1883),
        "username": config.get("username", ""),
        "password_set": bool(config.get("password")),
        "client_id": config.get("client_id", "home_electricity_monitor")
    }
    return jsonify(safe_config)

@app.route('/api/config/mqtt', methods=['POST'])
def save_mqtt_config():
    """保存MQTT配置"""
    data = request.get_json()
    
    broker = data.get('broker', '').strip()
    port = data.get('port', 1883)
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    client_id = data.get('client_id', 'home_electricity_monitor').strip()
    
    if not broker:
        return jsonify({"error": "服务器地址不能为空"}), 400
    
    # 保存到配置文件
    config = {
        "broker": broker,
        "port": int(port),
        "username": username,
        "password": password,
        "client_id": client_id
    }
    
    try:
        with open(MQTT_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        return jsonify({"success": True, "message": "配置已保存，重启程序后生效"})
        
    except Exception as e:
        return jsonify({"error": f"保存失败: {str(e)}"}), 500

@app.route('/api/config/mqtt/test', methods=['POST'])
def test_mqtt_connection():
    """测试MQTT连接"""
    data = request.get_json()
    
    broker = data.get('broker', '').strip()
    port = int(data.get('port', 1883))
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    
    if not broker:
        return jsonify({"error": "服务器地址不能为空"}), 400
    
    try:
        import paho.mqtt.client as mqtt
        
        # 创建测试客户端
        test_client = mqtt.Client(client_id="test_connection", protocol=mqtt.MQTTv311)
        
        if username:
            test_client.username_pw_set(username, password)
        
        # 设置超时
        test_client.loop_start()
        result = test_client.connect(broker, port, timeout=5)
        test_client.loop_stop()
        test_client.disconnect()
        
        return jsonify({"success": True, "message": "连接成功"})
        
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower():
            return jsonify({"error": "连接超时，请检查服务器地址和端口"})
        elif "refused" in error_msg.lower():
            return jsonify({"error": "连接被拒绝，请检查服务器是否运行"})
        else:
            return jsonify({"error": f"连接失败: {error_msg}"})

# =============================================================================
# API：插座控制
# =============================================================================

@app.route('/api/switch', methods=['POST'])
def control_switch():
    """控制插座开关"""
    data = request.get_json()
    action = data.get('action')  # 'on' 或 'off'
    switch_id = data.get('switch_id', 'main')  # 插座ID
    
    if action not in ['on', 'off']:
        return jsonify({"error": "无效的操作"}), 400
    
    # 发送MQTT控制命令
    success = _send_switch_command(switch_id, action)
    
    if success:
        return jsonify({"success": True, "action": action, "switch_id": switch_id})
    else:
        return jsonify({"error": "MQTT发送失败"}), 500

# =============================================================================
# WebSocket事件：客户端连接
# =============================================================================

@socketio.on('connect')
def handle_connect():
    """客户端连接时发送最新数据"""
    print("客户端已连接")
    _update_latest_data()
    emit('data_update', latest_data)

@socketio.on('request_data')
def handle_request_data():
    """客户端请求数据"""
    _update_latest_data()
    emit('data_update', latest_data)

# =============================================================================
# 辅助函数
# =============================================================================

def _update_latest_data():
    """从状态文件更新最新数据"""
    global latest_data
    
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
            
            latest_data['power_w'] = state.get('current_power_w', 0.0)
            latest_data['total_energy_kwh'] = state.get('total_energy_kwh', 0.0)
            latest_data['today_energy_kwh'] = state.get('today_energy_kwh', 0.0)
            latest_data['month_energy_kwh'] = state.get('month_energy_kwh', 0.0)
            latest_data['last_update'] = datetime.now().isoformat()
            
    except Exception as e:
        print(f"读取状态文件失败: {e}")

def _read_csv_records(date) -> list:
    """读取指定日期的记录（优先从MySQL读取）"""
    # 尝试从MySQL读取
    mysql_config = _load_mysql_config()
    if mysql_config.get('host'):
        try:
            import pymysql
            conn = pymysql.connect(
                host=mysql_config['host'],
                port=mysql_config.get('port', 3306),
                user=mysql_config['user'],
                password=mysql_config.get('password', ''),
                database=mysql_config['database'],
                charset='utf8mb4'
            )
            cursor = conn.cursor()
            
            if isinstance(date, str):
                date_str = date[:10]
            else:
                date_str = date.strftime('%Y-%m-%d')
                
            cursor.execute("""
                SELECT record_time, power_w, total_energy_kwh
                FROM electricity_records
                WHERE DATE(record_time) = %s
                ORDER BY record_time
            """, (date_str,))
            
            records = []
            for row in cursor.fetchall():
                records.append({
                    'timestamp': row[0].strftime('%Y-%m-%d %H:%M:%S'),
                    'power_w': float(row[1]),
                    'total_energy_kwh': float(row[2])
                })
                
            cursor.close()
            conn.close()
            
            if records:
                return records
                
        except Exception as e:
            print(f"MySQL读取失败，回退到CSV: {e}")
    
    # 回退到CSV读取
    csv_path = DATA_DIR / f"electricity_{date.year}-{date.month:02d}.csv"
    
    if not csv_path.exists():
        return []
    
    records = []
    date_str = date.strftime('%Y-%m-%d')
    
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['timestamp'].startswith(date_str):
                    records.append({
                        "timestamp": row['timestamp'],
                        "power_w": float(row['power_w']),
                        "total_energy_kwh": float(row['total_energy_kwh'])
                    })
    except Exception as e:
        print(f"读取CSV失败: {e}")
    
    return records

def _send_switch_command(switch_id: str, action: str) -> bool:
    """
    发送插座控制MQTT命令
    
    参数：
        switch_id: 插座ID
        action: 'on' 或 'off'
    
    返回：
        是否发送成功
    """
    try:
        # 导入主程序的MQTT客户端（如果在同进程运行）
        # 或者创建独立的MQTT连接
        import paho.mqtt.client as mqtt
        
        # 从配置文件读取MQTT配置
        mqtt_config = _load_mqtt_config()
        
        if not mqtt_config:
            print("无法加载MQTT配置")
            return False
        
        # 创建临时MQTT客户端发送控制命令
        client = mqtt.Client(client_id="web_control", protocol=mqtt.MQTTv311)
        
        if mqtt_config.get('username'):
            client.username_pw_set(
                mqtt_config['username'],
                mqtt_config.get('password')
            )
        
        client.connect(
            mqtt_config['broker'],
            mqtt_config['port'],
            keepalive=5
        )
        
        # 发送控制命令
        topic = f"home/switch/{switch_id}/set"
        payload = json.dumps({"state": action.upper()})
        
        client.publish(topic, payload, qos=1)
        client.disconnect()
        
        print(f"已发送控制命令: {topic} -> {payload}")
        return True
        
    except Exception as e:
        print(f"发送控制命令失败: {e}")
        return False

def _load_mysql_config() -> dict:
    """从配置文件加载MySQL设置"""
    try:
        if MYSQL_CONFIG_FILE.exists():
            with open(MYSQL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"加载MySQL配置失败: {e}")
    return {"host": "", "port": 3306, "database": "electricity_monitor", "user": "", "password": ""}

def _load_mqtt_config() -> dict:
    """从配置文件加载MQTT设置"""
    try:
        # 优先从配置文件读取
        if MQTT_CONFIG_FILE.exists():
            with open(MQTT_CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 确保所有字段都存在
                return {
                    "broker": config.get("broker", ""),
                    "port": config.get("port", 1883),
                    "username": config.get("username", ""),
                    "password": config.get("password", ""),
                    "client_id": config.get("client_id", "home_electricity_monitor")
                }
        
        # 如果配置文件不存在，尝试从主程序导入配置
        try:
            import sys
            sys.path.insert(0, '.')
            from home_electricity_monitor import MQTT_CONFIG
            return {
                "broker": MQTT_CONFIG.get("broker", ""),
                "port": MQTT_CONFIG.get("port", 1883),
                "username": MQTT_CONFIG.get("username", ""),
                "password": MQTT_CONFIG.get("password", ""),
                "client_id": MQTT_CONFIG.get("client_id", "home_electricity_monitor")
            }
        except ImportError:
            pass
        
        # 返回空配置
        return {
            "broker": "",
            "port": 1883,
            "username": "",
            "password": "",
            "client_id": "home_electricity_monitor"
        }
    except Exception as e:
        print(f"加载MQTT配置失败: {e}")
        return {
            "broker": "",
            "port": 1883,
            "username": "",
            "password": "",
            "client_id": "home_electricity_monitor"
        }

# =============================================================================
# 启动Web服务器
# =============================================================================

def start_web_server():
    """启动Web服务器（在独立线程中运行）"""
    print(f"Web服务器启动: http://localhost:{WEB_CONFIG['port']}")
    socketio.run(
        app,
        host=WEB_CONFIG['host'],
        port=WEB_CONFIG['port'],
        debug=WEB_CONFIG['debug'],
        allow_unsafe_werkzeug=True
    )

def run_web_server_thread():
    """在后台线程启动Web服务器"""
    web_thread = threading.Thread(target=start_web_server, daemon=True)
    web_thread.start()
    return web_thread

# =============================================================================
# 主程序入口（独立运行Web服务器时使用）
# =============================================================================

if __name__ == "__main__":
    start_web_server()
