# -*- coding: utf-8 -*-
"""
家庭用电统计程序 - 对接HomeAssistant
功能：采集实时功率、累计用电量，统计今日/昨日/本月用电量，通过MQTT上报到HA
特性：累计电能持久化（程序重启不清零），每日0点/每月1号自动重置
作者：AI Assistant
日期：2026-08-12
"""

import json
import time
import csv
import logging
import signal
from datetime import datetime, timedelta
from pathlib import Path
import threading
from typing import Optional

import paho.mqtt.client as mqtt

from config_store import (
    DATA_DIR,
    STATE_FILE,
    ensure_dirs,
    load_ha_config,
    load_mqtt_config,
    load_mysql_config,
)

# =============================================================================
# 程序配置
# =============================================================================

DEVICE_CONFIG = {
    "name": "家庭用电监控",
    "device_id": "home_electricity",
    "manufacturer": "Custom",
    "model": "Electricity Monitor",
}

CONFIG = {
    "update_interval": 10,            # 采集间隔（秒）
    "csv_dir": str(DATA_DIR),
    "state_file": str(STATE_FILE),
    "log_level": "INFO",
    "max_energy_gap_seconds": 300,    # 超过此时长不补算电量，避免停机后虚增
}

WEB_CONFIG = {
    "enabled": True,
    "host": "0.0.0.0",
    "port": 5000,
    "debug": False,
}

# =============================================================================
# 日志配置
# =============================================================================
ensure_dirs()
logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"]),
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / "electricity_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# 程序运行标志
running = True

# =============================================================================
# 状态持久化管理类
# =============================================================================

class StateManager:
    """
    状态持久化管理类
    负责保存和加载程序运行状态，确保程序重启后累计电能不丢失
    """
    
    def __init__(self, state_file: str):
        """
        初始化状态管理器
        
        参数：
            state_file: 状态文件路径
        """
        self.state_file = Path(state_file)
        self.lock = threading.Lock()
        
        # 默认状态
        self.state = {
            "total_energy_kwh": 0.0,        # 总累计用电量 (kWh)
            "day_start_energy_kwh": 0.0,     # 今日开始时的总电量 (kWh)
            "month_start_energy_kwh": 0.0,   # 本月开始时的总电量 (kWh)
            "last_reset_date": None,         # 上次日期重置时间
            "last_reset_month": None,        # 上次月份重置时间
            "current_power_w": 0.0,          # 当前功率 (W) - 供Web页面读取
            "today_energy_kwh": 0.0,         # 今日用电量 (kWh)
            "month_energy_kwh": 0.0,         # 本月用电量 (kWh)
        }
        
        # 启动时加载状态
        self.load()
        
    def load(self):
        """从文件加载状态"""
        with self.lock:
            try:
                if self.state_file.exists():
                    with open(self.state_file, 'r', encoding='utf-8') as f:
                        saved_state = json.load(f)
                    
                    # 更新状态（保留默认值用于新增字段）
                    self.state.update(saved_state)
                    logger.info(f"已加载持久化状态: 总电量={self.state['total_energy_kwh']:.4f} kWh")
                else:
                    logger.info("状态文件不存在，使用默认值")
                    
            except Exception as e:
                logger.error(f"加载状态文件失败: {e}，使用默认值")
                
    def save(self):
        """保存状态到文件"""
        with self.lock:
            try:
                # 确保父目录存在
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                
                with open(self.state_file, 'w', encoding='utf-8') as f:
                    json.dump(self.state, f, indent=2, ensure_ascii=False)
                    
                logger.debug("状态已保存")
                
            except Exception as e:
                logger.error(f"保存状态文件失败: {e}")
                
    def get(self, key: str):
        """获取状态值"""
        return self.state.get(key)
        
    def set(self, key: str, value):
        """设置状态值"""
        self.state[key] = value
        
    def get_total_energy(self) -> float:
        """获取总累计用电量"""
        return float(self.state.get("total_energy_kwh", 0.0))
        
    def add_energy(self, energy_kwh: float):
        """
        累加电能到总用电量
        
        参数：
            energy_kwh: 要累加的电能 (kWh)
        """
        current = self.get_total_energy()
        self.state["total_energy_kwh"] = current + energy_kwh
        
    def update_realtime_data(self, power_w: float, today_kwh: float, month_kwh: float):
        """
        更新实时数据（供Web页面读取）
        
        参数：
            power_w: 当前功率 (W)
            today_kwh: 今日用电量 (kWh)
            month_kwh: 本月用电量 (kWh)
        """
        self.state["current_power_w"] = power_w
        self.state["today_energy_kwh"] = today_kwh
        self.state["month_energy_kwh"] = month_kwh

# =============================================================================
# 功率数据源：从 Home Assistant 实体读取
# =============================================================================

class PowerSource:
    """从 Home Assistant 功率实体读取实时功率（W）。未配置或失败时返回 None。"""

    def read(self) -> Optional[float]:
        config = load_ha_config()
        url = (config.get("url") or "").strip().rstrip("/")
        token = config.get("token") or ""
        entity_id = (config.get("power_entity_id") or "").strip()

        if not url or not token or not entity_id:
            logger.warning(
                "未配置 Home Assistant 功率实体，跳过采集。"
                "请在 Web 设置中填写 HA 地址、访问令牌和功率实体 ID"
            )
            return None

        if not url.startswith("http"):
            url = "http://" + url

        try:
            import requests

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            response = requests.get(
                f"{url}/api/states/{entity_id}",
                headers=headers,
                timeout=8,
            )

            if response.status_code == 401:
                logger.error("HA 访问令牌无效")
                return None
            if response.status_code == 404:
                logger.error(f"HA 实体不存在: {entity_id}")
                return None
            if response.status_code != 200:
                logger.error(f"读取 HA 实体失败: HTTP {response.status_code}")
                return None

            data = response.json()
            state = data.get("state")
            if state in (None, "", "unknown", "unavailable"):
                logger.warning(f"HA 实体 {entity_id} 状态不可用: {state}")
                return None

            value = float(state)
            unit = str((data.get("attributes") or {}).get("unit_of_measurement") or "W")
            unit_key = unit.lower().replace(" ", "")
            if unit_key in ("kw", "kilowatt"):
                value *= 1000.0
            elif unit_key in ("mw", "megawatt"):
                value *= 1_000_000.0

            return max(0.0, value)

        except (TypeError, ValueError):
            logger.error(f"HA 实体 {entity_id} 数值无法解析")
            return None
        except Exception as e:
            logger.error(f"读取 HA 功率失败: {e}")
            return None

# =============================================================================
# CSV历史记录管理
# =============================================================================

class CSVManager:
    """CSV文件管理类，负责历史数据的记录和查询"""
    
    def __init__(self, data_dir: str):
        """
        初始化CSV管理器
        
        参数：
            data_dir: 数据存储目录
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)  # 如果目录不存在则创建
        self.lock = threading.Lock()  # 线程锁，防止并发写入问题
        
    def _get_csv_path(self, date: datetime = None) -> Path:
        """
        获取指定日期的CSV文件路径
        
        参数：
            date: 日期对象，默认为今天
            
        返回：
            CSV文件完整路径
        """
        if date is None:
            date = datetime.now()
        filename = f"electricity_{date.strftime('%Y-%m')}.csv"
        return self.data_dir / filename
        
    def write_record(self, power_w: float, total_energy_kwh: float):
        """
        写入一条用电记录到CSV
        
        参数：
            power_w: 当前功率 (W)
            total_energy_kwh: 总累计用电量 (kWh)
        """
        with self.lock:
            try:
                csv_path = self._get_csv_path()
                file_exists = csv_path.exists()
                
                with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    
                    # 如果文件不存在，先写入表头
                    if not file_exists:
                        writer.writerow(['timestamp', 'power_w', 'total_energy_kwh'])
                    
                    # 写入数据行
                    writer.writerow([
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        round(power_w, 2),
                        round(total_energy_kwh, 4)
                    ])
                    
                logger.debug(f"数据已写入: {csv_path}")
                
            except Exception as e:
                logger.error(f"写入CSV失败: {e}")
                
    def read_day_records(self, date) -> list:
        """
        读取指定日期的所有记录
        
        参数：
            date: 日期对象或字符串
            
        返回：
            该日所有记录列表
        """
        with self.lock:
            try:
                if isinstance(date, str):
                    date_str = date[:10]  # 取 YYYY-MM-DD 部分
                else:
                    date_str = date.strftime('%Y-%m-%d')
                    
                year, month, day = date_str.split('-')
                csv_path = self._get_csv_path(datetime(int(year), int(month), 1))
                
                if not csv_path.exists():
                    return []
                    
                records = []
                with open(csv_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # 只返回指定日期的记录
                        if row['timestamp'].startswith(date_str):
                            records.append(row)
                            
                return records
                
            except Exception as e:
                logger.error(f"读取CSV失败: {e}")
                return []

# =============================================================================
# MySQL数据库管理
# =============================================================================

class MySQLManager:
    """
    MySQL数据库管理类
    负责将用电数据保存到MySQL数据库
    """
    
    def __init__(self, config: dict):
        """
        初始化MySQL管理器
        
        参数：
            config: MySQL配置字典
        """
        self.config = config
        self.enabled = bool(config.get('host'))
        self.lock = threading.Lock()
        
        if self.enabled:
            self._init_database()
            
    def _get_connection(self):
        """获取数据库连接"""
        try:
            import pymysql
            return pymysql.connect(
                host=self.config['host'],
                port=self.config.get('port', 3306),
                user=self.config['user'],
                password=self.config.get('password', ''),
                database=self.config['database'],
                charset='utf8mb4',
                autocommit=True
            )
        except Exception as e:
            logger.error(f"MySQL连接失败: {e}")
            return None
            
    def _init_database(self):
        """初始化数据库表"""
        try:
            conn = self._get_connection()
            if not conn:
                return
                
            cursor = conn.cursor()
            
            # 创建用电记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS electricity_records (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    record_time DATETIME NOT NULL,
                    power_w DECIMAL(10,2) NOT NULL,
                    total_energy_kwh DECIMAL(12,4) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_record_time (record_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            
            # 创建每日统计表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_summary (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    record_date DATE NOT NULL UNIQUE,
                    total_power_w DECIMAL(12,2) DEFAULT 0,
                    max_power_w DECIMAL(10,2) DEFAULT 0,
                    min_power_w DECIMAL(10,2) DEFAULT 0,
                    avg_power_w DECIMAL(10,2) DEFAULT 0,
                    total_energy_kwh DECIMAL(12,4) DEFAULT 0,
                    record_count INT DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            
            cursor.close()
            conn.close()
            
            logger.info(f"MySQL数据库初始化完成: {self.config['database']}")
            
        except Exception as e:
            logger.error(f"MySQL初始化失败: {e}")
            self.enabled = False
            
    def write_record(self, power_w: float, total_energy_kwh: float):
        """
        写入一条用电记录
        
        参数：
            power_w: 当前功率 (W)
            total_energy_kwh: 总累计用电量 (kWh)
        """
        if not self.enabled:
            return
            
        with self.lock:
            try:
                conn = self._get_connection()
                if not conn:
                    return
                    
                cursor = conn.cursor()
                
                # 插入记录
                cursor.execute("""
                    INSERT INTO electricity_records (record_time, power_w, total_energy_kwh)
                    VALUES (NOW(), %s, %s)
                """, (power_w, total_energy_kwh))
                
                # 更新每日统计
                today = datetime.now().date()
                cursor.execute("""
                    INSERT INTO daily_summary (record_date, total_power_w, max_power_w, min_power_w, record_count)
                    VALUES (%s, %s, %s, %s, 1)
                    ON DUPLICATE KEY UPDATE
                        total_power_w = total_power_w + VALUES(total_power_w),
                        max_power_w = GREATEST(max_power_w, VALUES(max_power_w)),
                        min_power_w = LEAST(min_power_w, VALUES(min_power_w)),
                        record_count = record_count + 1
                """, (today, power_w, power_w, power_w))
                
                cursor.close()
                conn.close()
                
                logger.debug("数据已写入MySQL")
                
            except Exception as e:
                logger.error(f"写入MySQL失败: {e}")
                
    def read_day_records(self, date) -> list:
        """
        读取指定日期的历史记录
        """
        if not self.enabled:
            return []
            
        with self.lock:
            try:
                if isinstance(date, str):
                    date_str = date[:10]
                else:
                    date_str = date.strftime('%Y-%m-%d')
                    
                conn = self._get_connection()
                if not conn:
                    return []
                    
                cursor = conn.cursor()
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
                
                return records
                
            except Exception as e:
                logger.error(f"读取MySQL失败: {e}")
                return []

# =============================================================================
# 用电量统计计算（支持日期自动重置）
# =============================================================================

class EnergyCalculator:
    """
    用电量计算器
    负责计算今日/昨日/本月用电量，支持日期变更时自动重置
    """
    
    def __init__(self, state_manager: StateManager, csv_manager: CSVManager):
        """
        初始化计算器
        
        参数：
            state_manager: 状态管理器实例
            csv_manager: CSV管理器实例
        """
        self.state_manager = state_manager
        self.csv_manager = csv_manager
        
        # 获取当前日期（基于系统本地时间）
        now = datetime.now()
        self.current_date = now.date()
        self.current_month = (now.year, now.month)
        
        # 初始化时检查是否需要重置
        self._check_and_reset()
        
    def _check_and_reset(self):
        """检查并执行必要的重置操作"""
        now = datetime.now()
        today = now.date()
        current_month = (today.year, today.month)
        
        # 获取上次重置的日期和月份
        last_reset_date = self.state_manager.get("last_reset_date")
        last_reset_month = self.state_manager.get("last_reset_month")
        
        # 转换为可比较的格式
        if last_reset_date:
            if isinstance(last_reset_date, str):
                last_reset_date = datetime.strptime(last_reset_date, '%Y-%m-%d').date()
        else:
            last_reset_date = today  # 首次运行，设置为今天
            
        if last_reset_month:
            if isinstance(last_reset_month, str):
                last_reset_month = tuple(json.loads(last_reset_month))
            elif isinstance(last_reset_month, (list, tuple)):
                last_reset_month = tuple(last_reset_month)
        else:
            last_reset_month = current_month  # 首次运行
            
        # 检查日期是否变更（每日0点重置今日用电量）
        if last_reset_date != today:
            logger.info(f"日期变更: {last_reset_date} -> {today}")
            total_energy = self.state_manager.get_total_energy()
            self.state_manager.set("day_start_energy_kwh", total_energy)
            self.state_manager.set("last_reset_date", today.isoformat())
            logger.info(f"今日起始电量已重置为: {total_energy:.4f} kWh")
            
        # 检查月份是否变更（每月1号重置本月用电量）
        if last_reset_month != current_month:
            logger.info(f"月份变更: {last_reset_month} -> {current_month}")
            total_energy = self.state_manager.get_total_energy()
            self.state_manager.set("month_start_energy_kwh", total_energy)
            self.state_manager.set("last_reset_month", list(current_month))
            logger.info(f"本月起始电量已重置为: {total_energy:.4f} kWh")
            
        # 保存状态
        self.state_manager.save()
        
    def update(self, total_energy_kwh: float) -> dict:
        """
        更新用电量统计
        
        参数：
            total_energy_kwh: 当前总累计用电量 (kWh)
            
        返回：
            包含今日和本月用电量的字典
        """
        # 检查日期变更
        self._check_and_reset()
        
        # 获取今日起始电量和本月起始电量
        day_start = float(self.state_manager.get("day_start_energy_kwh") or 0)
        month_start = float(self.state_manager.get("month_start_energy_kwh") or 0)
        
        # 计算今日和本月用电量
        today_energy = max(0, total_energy_kwh - day_start)
        month_energy = max(0, total_energy_kwh - month_start)
        
        return {
            "today_energy": today_energy,
            "month_energy": month_energy
        }
        
    def get_yesterday_energy(self) -> float:
        """
        获取昨日用电量（从CSV文件读取历史数据计算）
        
        返回：
            昨日用电量 (kWh)
        """
        try:
            yesterday = datetime.now().date() - timedelta(days=1)
            records = self.csv_manager.read_day_records(yesterday)
            
            if not records:
                return 0.0
                
            # 获取昨日第一条和最后一条记录的总电量差值
            first_energy = float(records[0]['total_energy_kwh'])
            last_energy = float(records[-1]['total_energy_kwh'])
            
            return max(0, last_energy - first_energy)
            
        except Exception as e:
            logger.error(f"计算昨日用电量失败: {e}")
            return 0.0

# =============================================================================
# HomeAssistant MQTT自动发现
# =============================================================================

class HomeAssistantDiscovery:
    """HomeAssistant MQTT自动发现管理类"""
    
    def __init__(self, mqtt_client, device_config: dict):
        """
        初始化自动发现管理器
        
        参数：
            mqtt_client: MQTT客户端实例
            device_config: 设备配置字典
        """
        self.client = mqtt_client
        self.device_config = device_config
        self.base_topic = f"homeassistant/sensor/{device_config['device_id']}"
        
    def publish_discovery(self):
        """
        发布MQTT自动发现消息，让HomeAssistant自动创建传感器实体
        
        这遵循HomeAssistant的MQTT自动发现规范
        """
        logger.info("发布HomeAssistant MQTT自动发现配置...")
        
        # 定义四个传感器实体
        sensors = [
            {
                "name": "实时功率",
                "unique_id": f"{self.device_config['device_id']}_power",
                "state_topic": f"{self.base_topic}/power/state",
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "icon": "mdi:flash",
                "value_template": "{{ value }}"
            },
            {
                "name": "总电能",
                "unique_id": f"{self.device_config['device_id']}_total",
                "state_topic": f"{self.base_topic}/total/state",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                "icon": "mdi:meter-electric",
                "value_template": "{{ value }}"
            },
            {
                "name": "今日耗电",
                "unique_id": f"{self.device_config['device_id']}_today",
                "state_topic": f"{self.base_topic}/today/state",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                "icon": "mdi:calendar-today",
                "value_template": "{{ value }}"
            },
            {
                "name": "本月耗电",
                "unique_id": f"{self.device_config['device_id']}_month",
                "state_topic": f"{self.base_topic}/month/state",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                "icon": "mdi:calendar-month",
                "value_template": "{{ value }}"
            }
        ]
        
        # 为每个传感器发布发现消息
        for sensor in sensors:
            try:
                # 构建发现消息
                discovery_topic = f"{self.base_topic}/{sensor['unique_id'].split('_')[-1]}/config"
                
                # 添加设备信息
                payload = {
                    **sensor,
                    "device": {
                        "identifiers": [self.device_config['device_id']],
                        "name": self.device_config['name'],
                        "manufacturer": self.device_config['manufacturer'],
                        "model": self.device_config['model']
                    }
                }
                
                # 发布消息（保留消息，确保HA重启后仍能发现）
                self.client.publish(
                    discovery_topic,
                    json.dumps(payload),
                    qos=1,
                    retain=True
                )
                
                logger.info(f"已发布传感器发现: {sensor['name']}")
                
            except Exception as e:
                logger.error(f"发布传感器发现失败 {sensor['name']}: {e}")
                
    def publish_state(self, stats: dict):
        """
        发布传感器状态数据
        
        参数：
            stats: 包含各传感器数值的字典
        """
        try:
            # 发布实时功率 (W)
            self.client.publish(
                f"{self.base_topic}/power/state",
                str(round(stats['power_w'], 2)),
                qos=1,
                retain=True
            )
            
            # 发布总电能 (kWh)
            self.client.publish(
                f"{self.base_topic}/total/state",
                str(round(stats['total_energy_kwh'], 4)),
                qos=1,
                retain=True
            )
            
            # 发布今日耗电 (kWh)
            self.client.publish(
                f"{self.base_topic}/today/state",
                str(round(stats['today_energy_kwh'], 4)),
                qos=1,
                retain=True
            )
            
            # 发布本月耗电 (kWh)
            self.client.publish(
                f"{self.base_topic}/month/state",
                str(round(stats['month_energy_kwh'], 4)),
                qos=1,
                retain=True
            )
            
            logger.debug("状态数据已发布到MQTT")
            
        except Exception as e:
            logger.error(f"发布状态失败: {e}")

# =============================================================================
# MQTT客户端管理
# =============================================================================

class MQTTManager:
    """MQTT客户端管理类，处理连接、重连等操作"""
    
    def __init__(self, config: dict):
        """
        初始化MQTT管理器
        
        参数：
            config: MQTT配置字典
        """
        self.config = config
        self.enabled = bool((config.get("broker") or "").strip())
        self.client = mqtt.Client(
            client_id=config.get("client_id") or "home_electricity_monitor",
            protocol=mqtt.MQTTv311
        )
        
        # 设置认证信息
        if config.get('username'):
            self.client.username_pw_set(
                config['username'],
                config.get('password')
            )
            
        # 设置回调函数
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        
        # 连接状态
        self.connected = False
        self.reconnect_count = 0
        self.max_reconnect = 5  # 最大重连次数
        self.reconnect_timer = None
        
    def _on_connect(self, client, userdata, flags, rc):
        """
        连接成功回调
        
        参数：
            client: 客户端实例
            userdata: 用户数据
            flags: 连接标志
            rc: 连接结果码
        """
        if rc == 0:
            logger.info("MQTT连接成功!")
            self.connected = True
            self.reconnect_count = 0
        else:
            error_messages = {
                1: "协议版本错误",
                2: "客户端标识无效",
                3: "服务器不可用",
                4: "用户名或密码错误",
                5: "未授权"
            }
            error_msg = error_messages.get(rc, f"未知错误: {rc}")
            logger.error(f"MQTT连接失败: {error_msg}")
            
    def _on_disconnect(self, client, userdata, rc):
        """
        断开连接回调
        
        参数：
            client: 客户端实例
            userdata: 用户数据
            rc: 断开原因码
        """
        self.connected = False
        if rc != 0:
            logger.warning(f"MQTT意外断开连接，原因码: {rc}")
            self._schedule_reconnect()
            
    def _on_message(self, client, userdata, msg):
        """
        收到消息回调（预留，可用于接收控制命令）
        
        参数：
            client: 客户端实例
            userdata: 用户数据
            msg: 消息对象
        """
        logger.debug(f"收到消息: {msg.topic} -> {msg.payload.decode()}")
        
    def _schedule_reconnect(self):
        """调度重连（指数退避）"""
        if self.reconnect_count < self.max_reconnect:
            self.reconnect_count += 1
            wait_time = min(30, 2 ** self.reconnect_count)  # 最大30秒
            logger.info(f"将在 {wait_time} 秒后重连 ({self.reconnect_count}/{self.max_reconnect})...")
            
            # 取消之前的定时器
            if self.reconnect_timer:
                self.reconnect_timer.cancel()
                
            self.reconnect_timer = threading.Timer(wait_time, self._do_reconnect)
            self.reconnect_timer.start()
        else:
            logger.error(f"已达到最大重连次数 ({self.max_reconnect})，请检查网络和MQTT服务器")
            
    def _do_reconnect(self):
        """执行重连"""
        try:
            logger.info("尝试重新连接MQTT服务器...")
            self.client.reconnect()
        except Exception as e:
            logger.error(f"重连失败: {e}")
            self._schedule_reconnect()
            
    def connect(self):
        """建立MQTT连接"""
        if not self.enabled:
            logger.info("未配置 MQTT 服务器，跳过连接")
            return
        try:
            logger.info(f"正在连接MQTT服务器: {self.config['broker']}:{self.config['port']}")
            self.client.connect(
                self.config['broker'],
                self.config['port'],
                keepalive=60
            )
            self.client.loop_start()
            
            # 等待连接成功
            wait_count = 0
            while not self.connected and wait_count < 10:
                time.sleep(0.5)
                wait_count += 1
                
            if not self.connected:
                logger.warning("MQTT连接超时，将在后台继续尝试重连...")
                
        except Exception as e:
            logger.error(f"MQTT连接异常: {e}")
            self._schedule_reconnect()
            
    def disconnect(self):
        """断开MQTT连接"""
        if not self.enabled:
            return
        try:
            # 取消重连定时器
            if self.reconnect_timer:
                self.reconnect_timer.cancel()
                
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("MQTT已断开连接")
        except Exception as e:
            logger.error(f"断开MQTT连接时出错: {e}")

# =============================================================================
# 主程序类
# =============================================================================

class ElectricityMonitor:
    """家庭用电监控主程序"""
    
    def __init__(self):
        """初始化监控程序"""
        logger.info("=" * 50)
        logger.info("家庭用电监控程序启动")
        logger.info("=" * 50)
        
        # 初始化组件
        self.state_manager = StateManager(CONFIG['state_file'])
        self.csv_manager = CSVManager(CONFIG['csv_dir'])
        
        # 加载MySQL配置并初始化
        mysql_config = self._load_mysql_config()
        self.mysql_manager = MySQLManager(mysql_config)
        
        self.energy_calculator = EnergyCalculator(self.state_manager, self.csv_manager)
        self.mqtt_manager = MQTTManager(load_mqtt_config())
        self.power_source = PowerSource()
        self.ha_discovery = None
        
        # 记录上次数据采集时间（用于实际间隔积分）
        self.last_update_time = 0
        self.last_sample_time = 0.0
        self.last_power_w = None
        
        # 当前统计数据（用于MQTT发布）
        self.current_stats = {
            "power_w": 0.0,              # 实时功率 (W)
            "total_energy_kwh": 0.0,     # 总累计用电量 (kWh)
            "today_energy_kwh": 0.0,     # 今日用电量 (kWh)
            "month_energy_kwh": 0.0      # 本月用电量 (kWh)
        }
        
    def _load_mysql_config(self) -> dict:
        """从配置文件加载MySQL设置"""
        return load_mysql_config()
        
    def setup(self):
        """程序初始化设置"""
        if self.mqtt_manager.enabled:
            self.mqtt_manager.connect()
            time.sleep(1)
            self.ha_discovery = HomeAssistantDiscovery(
                self.mqtt_manager.client,
                DEVICE_CONFIG
            )
            self.ha_discovery.publish_discovery()
        else:
            logger.info("未配置 MQTT，跳过 Home Assistant 自动发现")
        
        self.current_stats['total_energy_kwh'] = self.state_manager.get_total_energy()
        logger.info(f"程序初始化完成，历史累计电量: {self.current_stats['total_energy_kwh']:.4f} kWh")
        
    def collect_and_publish(self):
        """采集数据并发布"""
        try:
            power_w = self.power_source.read()
            now = time.time()

            if power_w is None:
                return

            # 用上一次实测功率 × 实际间隔积分；首次采样或间隔过长不补算
            if self.last_sample_time > 0 and self.last_power_w is not None:
                elapsed = now - self.last_sample_time
                max_gap = CONFIG["max_energy_gap_seconds"]
                if elapsed <= 0:
                    pass
                elif elapsed > max_gap:
                    logger.warning(
                        f"采样间隔过长 ({elapsed:.0f}s > {max_gap}s)，跳过本次电量累加"
                    )
                else:
                    energy_increment_kwh = self.last_power_w * elapsed / 3600.0 / 1000.0
                    self.state_manager.add_energy(energy_increment_kwh)

            self.last_sample_time = now
            self.last_power_w = power_w
            
            # 获取更新后的总用电量
            total_energy_kwh = self.state_manager.get_total_energy()
            
            # 计算今日和本月用电量
            energy_calc = self.energy_calculator.update(total_energy_kwh)
            
            # 更新统计数据
            self.current_stats['power_w'] = power_w
            self.current_stats['total_energy_kwh'] = total_energy_kwh
            self.current_stats['today_energy_kwh'] = energy_calc['today_energy']
            self.current_stats['month_energy_kwh'] = energy_calc['month_energy']
            
            # 更新实时数据到状态管理器（供Web页面读取）
            self.state_manager.update_realtime_data(
                power_w,
                energy_calc['today_energy'],
                energy_calc['month_energy']
            )
            
            # 保存状态到文件（持久化）
            self.state_manager.save()
            
            # 保存到CSV
            self.csv_manager.write_record(power_w, total_energy_kwh)
            
            self.mysql_manager.write_record(power_w, total_energy_kwh)

            if self.ha_discovery and self.mqtt_manager.connected:
                self.ha_discovery.publish_state(self.current_stats)
            
            logger.info(
                f"数据更新 - 功率: {power_w:.1f}W, "
                f"总电能: {total_energy_kwh:.4f}kWh, "
                f"今日: {energy_calc['today_energy']:.4f}kWh, "
                f"本月: {energy_calc['month_energy']:.4f}kWh"
            )
            
        except Exception as e:
            logger.error(f"数据采集和发布失败: {e}")
            
    def run(self):
        """主运行循环"""
        self.setup()
        
        # 启动Web服务器（如果启用）
        if WEB_CONFIG.get("enabled", False):
            self._start_web_server()
        
        logger.info(f"开始数据采集，间隔: {CONFIG['update_interval']}秒")
        
        while running:
            try:
                current_time = time.time()
                
                # 检查是否到达更新时间
                if current_time - self.last_update_time >= CONFIG['update_interval']:
                    self.collect_and_publish()
                    self.last_update_time = current_time
                    
                # 短暂休眠，避免CPU占用过高
                time.sleep(0.1)
                
            except KeyboardInterrupt:
                logger.info("收到退出信号")
                break
            except Exception as e:
                logger.error(f"主循环异常: {e}")
                time.sleep(1)  # 异常时等待一下再继续
                
        self.cleanup()
        
    def _start_web_server(self):
        """启动Web服务器"""
        try:
            import sys
            sys.path.insert(0, '.')
            from web_server import run_web_server_thread, WEB_CONFIG as ServerConfig
            
            # 更新Web服务器配置
            ServerConfig['host'] = WEB_CONFIG['host']
            ServerConfig['port'] = WEB_CONFIG['port']
            ServerConfig['debug'] = WEB_CONFIG['debug']
            
            # 在后台线程启动Web服务器
            run_web_server_thread()
            logger.info(f"Web服务器已启动: http://localhost:{WEB_CONFIG['port']}")
            
        except Exception as e:
            logger.error(f"启动Web服务器失败: {e}")
            logger.warning("Web功能将不可用，但MQTT数据采集将继续运行")
            
    def cleanup(self):
        """程序清理工作"""
        logger.info("正在关闭程序...")
        # 最后保存一次状态
        self.state_manager.save()
        self.mqtt_manager.disconnect()
        logger.info("程序已安全退出")

# =============================================================================
# 程序入口
# =============================================================================

def signal_handler(signum, frame):
    """信号处理器，用于优雅退出"""
    global running
    logger.info("收到退出信号，准备关闭...")
    running = False

def main():
    """主函数"""
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 创建并运行监控程序
    monitor = ElectricityMonitor()
    monitor.run()

if __name__ == "__main__":
    main()
