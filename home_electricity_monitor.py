# -*- coding: utf-8 -*-
"""
家庭用电统计程序 - 对接 Home Assistant（支持多个插座）

只需要在 Web 端填 HA 地址和令牌，勾选插座设备即可；
每个插座的功率 / 电量 / 电压 / 电流等实体由程序自动绑定。

特性：
- 多设备并行采集，单独统计 + 全屋合计
- 有电量实体的直接用 HA 累计值（含归零处理），没有的按功率积分
- 累计电能持久化，程序重启不清零；每日 0 点 / 每月 1 号自动重置
- 可选 MQTT 自动发现回写 HA，可选 MySQL 长期存储
"""

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt

import ha_client
import mi_decode
from config_store import (
    DATA_DIR,
    LEGACY_DEVICE_KEY,
    STATE_FILE,
    devices_signature,
    ensure_dirs,
    load_devices,
    load_ha_config,
    load_mqtt_config,
    load_mysql_config,
)
from ha_client import HAClient, HAError
from storage import CSVStore, MySQLStore

# =============================================================================
# 程序配置
# =============================================================================

DEVICE_CONFIG = {
    "name": "家庭用电监控",
    "device_id": "home_electricity",
    "manufacturer": "HAPowerStats",
    "model": "Electricity Monitor",
}

CONFIG = {
    "update_interval": 10,            # 采集间隔（秒）
    "state_file": str(STATE_FILE),
    "log_level": "INFO",
    "max_energy_gap_seconds": 300,    # 超过此时长不补算电量，避免停机后虚增
    "max_energy_step_kwh": 5.0,       # 单次电量跳变上限，超过视为实体异常
}

WEB_CONFIG = {
    "enabled": True,
    "host": os.environ.get("WEB_HOST", "0.0.0.0"),
    "port": int(os.environ.get("WEB_PORT", "5000")),
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

running = True


def _empty_device_state(name: str = "") -> dict:
    return {
        "name": name,
        "total_energy_kwh": 0.0,        # 本程序累计的总电量
        "day_start_energy_kwh": 0.0,    # 今日起始累计值
        "month_start_energy_kwh": 0.0,  # 本月起始累计值
        "last_reset_date": None,
        "last_reset_month": None,
        "current_power_w": 0.0,
        "today_energy_kwh": 0.0,
        "month_energy_kwh": 0.0,
        "voltage_v": None,
        "current_a": None,
        "current_derived": False,     # 电流是 功率/电压 推算的（米家也是这么算的）
        "power_factor": None,
        "frequency_hz": None,
        "switch_state": None,
        "energy_source": "integrate",   # entity=读 HA 电量实体，integrate=功率积分
        "period_source": "computed",    # device=插座自报今日/本月，computed=本程序统计
        "total_basis": "accumulated",   # entity=插座自身累计计数器，accumulated=本程序累加
        "device_total_kwh": None,       # 插座自身的累计计数器读数
        "energy_offset_kwh": 0.0,       # 实体归零前的累计量，保证总数不倒退
        "last_energy_reading": None,    # HA 电量实体上一次读数
        "available": False,
        "last_update": None,
    }


# =============================================================================
# 状态持久化（按设备）
# =============================================================================

class StateManager:
    """保存每个设备的累计电量等状态，程序重启不丢。"""

    def __init__(self, state_file: str):
        self.state_file = Path(state_file or STATE_FILE)
        self.lock = threading.RLock()
        self.state = {
            "version": 2,
            "devices": {},
            "aggregate": {
                "current_power_w": 0.0,
                "total_energy_kwh": 0.0,
                "today_energy_kwh": 0.0,
                "month_energy_kwh": 0.0,
            },
            # 下面 4 个字段是给老版本页面 / 外部脚本兼容用的合计值
            "current_power_w": 0.0,
            "total_energy_kwh": 0.0,
            "today_energy_kwh": 0.0,
            "month_energy_kwh": 0.0,
            "last_update": None,
        }
        self.load()

    def load(self):
        with self.lock:
            try:
                if not self.state_file.exists():
                    logger.info("状态文件不存在，使用默认值")
                    return
                with open(self.state_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if not isinstance(saved, dict):
                    return
                if saved.get("version") == 2 and isinstance(saved.get("devices"), dict):
                    self.state.update(saved)
                    logger.info("已加载 %d 个设备的历史状态", len(self.state["devices"]))
                else:
                    self._migrate_v1(saved)
            except Exception as e:
                logger.error("加载状态文件失败: %s，使用默认值", e)

    def _migrate_v1(self, saved: dict):
        """老状态是单设备平铺的，迁移到 legacy_main 设备下。"""
        device = _empty_device_state("默认功率实体")
        for key in (
            "total_energy_kwh",
            "day_start_energy_kwh",
            "month_start_energy_kwh",
            "last_reset_date",
            "last_reset_month",
            "current_power_w",
            "today_energy_kwh",
            "month_energy_kwh",
        ):
            if key in saved:
                device[key] = saved[key]

        # 老配置会迁移成 legacy_main 设备，历史累计电量挂在它下面才能接上
        devices = load_devices()
        keys = [d["key"] for d in devices]
        target_key = LEGACY_DEVICE_KEY if (LEGACY_DEVICE_KEY in keys or not keys) else keys[0]
        self.state["devices"][target_key] = device
        logger.info(
            "已迁移旧版累计电量 %.4f kWh 到设备 %s",
            float(device.get("total_energy_kwh") or 0),
            target_key,
        )
        self.save()

    def save(self):
        with self.lock:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, indent=2, ensure_ascii=False)
                tmp.replace(self.state_file)
            except Exception as e:
                logger.error("保存状态文件失败: %s", e)

    def device(self, key: str, name: str = "") -> dict:
        with self.lock:
            device = self.state["devices"].get(key)
            if device is None:
                device = _empty_device_state(name)
                self.state["devices"][key] = device
            elif name:
                device["name"] = name
            return device

    def drop_missing(self, keys):
        """配置里已删除的设备，状态也一起清掉。"""
        with self.lock:
            for key in list(self.state["devices"].keys()):
                if key not in keys:
                    self.state["devices"].pop(key, None)

    def update_aggregate(self):
        with self.lock:
            devices = [d for d in self.state["devices"].values() if d.get("available") or d.get("total_energy_kwh")]
            aggregate = {
                "current_power_w": sum(float(d.get("current_power_w") or 0) for d in devices),
                "total_energy_kwh": sum(float(d.get("total_energy_kwh") or 0) for d in devices),
                "today_energy_kwh": sum(float(d.get("today_energy_kwh") or 0) for d in devices),
                "month_energy_kwh": sum(float(d.get("month_energy_kwh") or 0) for d in devices),
            }
            self.state["aggregate"] = aggregate
            self.state.update(aggregate)
            self.state["last_update"] = datetime.now().isoformat()
            return aggregate


# =============================================================================
# 单个设备的电量统计
# =============================================================================

class DeviceAccumulator:
    """负责一个插座的电量累计与日 / 月统计。"""

    def __init__(self, config: dict, state_manager: StateManager):
        self.key = config["key"]
        self.name = config.get("name") or config["key"]
        self.entities = config.get("entities") or {}
        self.decoders = config.get("decoders") or {}
        self.energy_mode = config.get("energy_mode") or "auto"
        self.state_manager = state_manager
        self.last_sample_time = 0.0
        self.last_power_w = None

    def _reading(self, index: dict, role: str):
        """取某个角色的值：优先真实实体，没有就用小米打包寄存器解码。"""
        entity_id = self.entities.get(role)
        if entity_id:
            item = index.get(entity_id)
            if not item:
                logger.warning("[%s] 实体不存在或不可读: %s", self.name, entity_id)
                return None
            return ha_client.role_value(role, item.get("state"), item.get("unit"))
        return self._decoded(index, role)

    def _decoded(self, index: dict, role: str):
        """小米插座把多项数据打包进一个大整数，这里解出来。"""
        config = self.decoders.get(role)
        if not config:
            return None
        item = index.get(config.get("entity_id"))
        if not item:
            logger.warning("[%s] 打包实体不存在或不可读: %s", self.name, config.get("entity_id"))
            return None
        value = mi_decode.decode(config.get("spec"), item.get("state"))
        if value is None:
            logger.warning(
                "[%s] 打包数据解码失败: %s = %s",
                self.name, config.get("entity_id"), item.get("state"),
            )
        return value

    def _check_reset(self, state: dict):
        """跨天 / 跨月时把起始累计值刷新掉。"""
        now = datetime.now()
        today = now.date()
        month = [today.year, today.month]

        last_date = state.get("last_reset_date")
        if last_date != today.isoformat():
            if last_date is not None:
                logger.info("[%s] 日期变更 %s -> %s，重置今日统计", self.name, last_date, today)
            state["day_start_energy_kwh"] = float(state.get("total_energy_kwh") or 0)
            state["last_reset_date"] = today.isoformat()

        last_month = state.get("last_reset_month")
        if isinstance(last_month, (list, tuple)):
            last_month = list(last_month)
        if last_month != month:
            if last_month is not None:
                logger.info("[%s] 月份变更 %s -> %s，重置本月统计", self.name, last_month, month)
            state["month_start_energy_kwh"] = float(state.get("total_energy_kwh") or 0)
            state["last_reset_month"] = month

    def _accumulate_from_entity(self, state: dict, reading: float):
        """直接采用插座自身的累计电量计数器。

        插座的耗电量实体本身就是「出厂至今」的累计值（而且往往是整数级跳变），
        以前只累加接入后的增量，导致页面上的「累计」长期是 0，这里改成直接用它。
        """
        previous = state.get("last_energy_reading")
        offset = float(state.get("energy_offset_kwh") or 0)

        if previous is not None and reading + 1e-9 < float(previous):
            # 实体归零（清零 / 换设备 / 重新上电），把归零前的读数并进偏移量
            offset += float(previous)
            state["energy_offset_kwh"] = offset
            logger.info(
                "[%s] 电量实体归零：%.4f -> %.4f kWh，累计值按偏移量继续往上加",
                self.name, previous, reading,
            )

        state["last_energy_reading"] = reading
        state["device_total_kwh"] = reading
        new_total = reading + offset

        if state.get("total_basis") != "entity":
            # 从「本程序累加」切换到「用插座累计值」，把日/月基线一起挪过去，
            # 否则今日/本月会瞬间变成一个巨大的数
            old_today = float(state.get("today_energy_kwh") or 0)
            old_month = float(state.get("month_energy_kwh") or 0)
            state["day_start_energy_kwh"] = new_total - old_today
            state["month_start_energy_kwh"] = new_total - old_month
            state["total_basis"] = "entity"

        state["total_energy_kwh"] = new_total

    def _accumulate_from_power(self, state: dict, power_w: float, now: float):
        """没有电量实体时，用功率对时间积分。"""
        if self.last_sample_time > 0 and self.last_power_w is not None:
            elapsed = now - self.last_sample_time
            if elapsed <= 0:
                pass
            elif elapsed > CONFIG["max_energy_gap_seconds"]:
                logger.warning(
                    "[%s] 采样间隔过长 (%.0fs)，跳过本次电量累加", self.name, elapsed
                )
            else:
                increment = self.last_power_w * elapsed / 3600.0 / 1000.0
                state["total_energy_kwh"] = float(state.get("total_energy_kwh") or 0) + increment
        state["total_basis"] = "accumulated"

    def collect(self, index: dict) -> Optional[dict]:
        """采集一次，返回要落库的行；设备不可读时返回 None。"""
        state = self.state_manager.device(self.key, self.name)
        now = time.time()

        power_w = self._reading(index, "power")
        energy_kwh = self._reading(index, "energy")

        if power_w is None and energy_kwh is None:
            state["available"] = False
            state["current_power_w"] = 0.0
            return None

        use_entity = energy_kwh is not None and self.energy_mode in ("auto", "entity")
        if use_entity:
            self._accumulate_from_entity(state, energy_kwh)
            state["energy_source"] = "entity"
        elif power_w is not None:
            self._accumulate_from_power(state, power_w, now)
            state["energy_source"] = "integrate"

        self.last_sample_time = now
        self.last_power_w = power_w if power_w is not None else self.last_power_w

        self._check_reset(state)

        total = float(state.get("total_energy_kwh") or 0)
        state["current_power_w"] = float(power_w or 0.0)
        state["today_energy_kwh"] = max(0.0, total - float(state.get("day_start_energy_kwh") or 0))
        state["month_energy_kwh"] = max(0.0, total - float(state.get("month_start_energy_kwh") or 0))

        # 插座自己就报今日/本月电量（小米打包寄存器解出来的），直接用它，
        # 这样页面数字和米家 App 完全一致
        reported_today = self._decoded(index, "today_energy")
        reported_month = self._decoded(index, "month_energy")
        if reported_today is not None or reported_month is not None:
            if reported_today is not None:
                state["today_energy_kwh"] = reported_today
            if reported_month is not None:
                state["month_energy_kwh"] = reported_month
            state["period_source"] = "device"
        else:
            state["period_source"] = "computed"
        state["voltage_v"] = self._reading(index, "voltage")
        measured_current = self._reading(index, "current")
        if measured_current is not None:
            state["current_a"] = measured_current
            state["current_derived"] = False
        elif power_w is not None and state["voltage_v"] and state["voltage_v"] > 50:
            # 小米插座不上报电流，米家界面里的电流也是 功率/电压 算出来的
            state["current_a"] = round(power_w / state["voltage_v"], 3)
            state["current_derived"] = True
        else:
            state["current_a"] = None
            state["current_derived"] = False
        state["power_factor"] = self._reading(index, "power_factor")
        state["frequency_hz"] = self._reading(index, "frequency")
        state["entity_energy_kwh"] = energy_kwh
        state["available"] = True
        state["last_update"] = datetime.now().isoformat()

        switch_entity = self.entities.get("switch")
        if switch_entity:
            item = index.get(switch_entity)
            state["switch_state"] = (item or {}).get("state")
        else:
            state["switch_state"] = None

        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "device_key": self.key,
            "device_name": self.name,
            "power_w": state["current_power_w"],
            "total_energy_kwh": total,
            "voltage_v": state["voltage_v"],
            "current_a": state["current_a"],
        }


# =============================================================================
# HomeAssistant MQTT 自动发现（可选）
# =============================================================================

class HomeAssistantDiscovery:
    """把统计结果通过 MQTT 回写成 HA 实体：每个插座一组 + 全屋合计。"""

    def __init__(self, mqtt_client, device_config: dict):
        self.client = mqtt_client
        self.device_config = device_config
        self.published_keys = set()

    def _slug(self, key: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in str(key)).strip("_").lower() or "device"

    def _sensor_defs(self):
        return [
            ("power", "实时功率", "W", "power", "measurement", "mdi:flash"),
            ("total", "总电能", "kWh", "energy", "total_increasing", "mdi:meter-electric"),
            ("today", "今日耗电", "kWh", "energy", "total_increasing", "mdi:calendar-today"),
            ("month", "本月耗电", "kWh", "energy", "total_increasing", "mdi:calendar-month"),
        ]

    def _publish_group(self, group_id: str, group_name: str, base_topic: str):
        for suffix, label, unit, device_class, state_class, icon in self._sensor_defs():
            payload = {
                "name": "%s %s" % (group_name, label),
                "unique_id": "%s_%s" % (group_id, suffix),
                "state_topic": "%s/%s/state" % (base_topic, suffix),
                "unit_of_measurement": unit,
                "device_class": device_class,
                "state_class": state_class,
                "icon": icon,
                "device": {
                    "identifiers": [group_id],
                    "name": group_name,
                    "manufacturer": self.device_config["manufacturer"],
                    "model": self.device_config["model"],
                },
            }
            topic = "homeassistant/sensor/%s/%s/config" % (group_id, suffix)
            self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)

    def publish_discovery(self, devices: list):
        base_id = self.device_config["device_id"]
        self._publish_group(base_id, self.device_config["name"] + " 合计", "homeassistant/sensor/" + base_id)
        for device in devices:
            group_id = "%s_%s" % (base_id, self._slug(device["key"]))
            self._publish_group(group_id, device.get("name") or device["key"], "homeassistant/sensor/" + group_id)
        self.published_keys = {d["key"] for d in devices}
        logger.info("已发布 MQTT 自动发现配置：合计 + %d 个插座", len(devices))

    def _publish_values(self, group_id: str, values: dict):
        base_topic = "homeassistant/sensor/" + group_id
        for suffix, value, digits in (
            ("power", values.get("current_power_w", 0.0), 2),
            ("total", values.get("total_energy_kwh", 0.0), 4),
            ("today", values.get("today_energy_kwh", 0.0), 4),
            ("month", values.get("month_energy_kwh", 0.0), 4),
        ):
            self.client.publish(
                "%s/%s/state" % (base_topic, suffix),
                str(round(float(value or 0), digits)),
                qos=1,
                retain=True,
            )

    def publish_state(self, aggregate: dict, device_states: dict):
        base_id = self.device_config["device_id"]
        try:
            self._publish_values(base_id, aggregate)
            for key, state in device_states.items():
                self._publish_values("%s_%s" % (base_id, self._slug(key)), state)
        except Exception as e:
            logger.error("发布 MQTT 状态失败: %s", e)


# =============================================================================
# MQTT 客户端
# =============================================================================

class MQTTManager:
    """MQTT 连接管理，带指数退避重连。"""

    def __init__(self, config: dict):
        self.config = config
        self.enabled = bool((config.get("broker") or "").strip())
        self.client = mqtt.Client(
            client_id=config.get("client_id") or "home_electricity_monitor",
            protocol=mqtt.MQTTv311,
        )
        if config.get("username"):
            self.client.username_pw_set(config["username"], config.get("password"))

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        self.connected = False
        self.reconnect_count = 0
        self.max_reconnect = 5
        self.reconnect_timer = None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT 连接成功")
            self.connected = True
            self.reconnect_count = 0
        else:
            messages = {
                1: "协议版本错误",
                2: "客户端标识无效",
                3: "服务器不可用",
                4: "用户名或密码错误",
                5: "未授权",
            }
            logger.error("MQTT 连接失败: %s", messages.get(rc, "未知错误 %s" % rc))

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            logger.warning("MQTT 意外断开，原因码: %s", rc)
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        if self.reconnect_count >= self.max_reconnect:
            logger.error("MQTT 已达最大重连次数 (%d)", self.max_reconnect)
            return
        self.reconnect_count += 1
        wait = min(30, 2 ** self.reconnect_count)
        logger.info("将在 %d 秒后重连 MQTT (%d/%d)", wait, self.reconnect_count, self.max_reconnect)
        if self.reconnect_timer:
            self.reconnect_timer.cancel()
        self.reconnect_timer = threading.Timer(wait, self._do_reconnect)
        self.reconnect_timer.daemon = True
        self.reconnect_timer.start()

    def _do_reconnect(self):
        try:
            self.client.reconnect()
        except Exception as e:
            logger.error("MQTT 重连失败: %s", e)
            self._schedule_reconnect()

    def connect(self):
        if not self.enabled:
            logger.info("未配置 MQTT 服务器，跳过连接")
            return
        try:
            logger.info("正在连接 MQTT: %s:%s", self.config["broker"], self.config["port"])
            self.client.connect(self.config["broker"], self.config["port"], keepalive=60)
            self.client.loop_start()
            for _ in range(10):
                if self.connected:
                    break
                time.sleep(0.5)
            if not self.connected:
                logger.warning("MQTT 连接超时，将在后台继续重试")
        except Exception as e:
            logger.error("MQTT 连接异常: %s", e)
            self._schedule_reconnect()

    def disconnect(self):
        if not self.enabled:
            return
        try:
            if self.reconnect_timer:
                self.reconnect_timer.cancel()
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("MQTT 已断开")
        except Exception as e:
            logger.error("断开 MQTT 出错: %s", e)


# =============================================================================
# 主程序
# =============================================================================

class ElectricityMonitor:
    """多插座用电监控主程序。"""

    def __init__(self):
        logger.info("=" * 50)
        logger.info("家庭用电监控程序启动（多插座）")
        logger.info("=" * 50)

        self.state_manager = StateManager(CONFIG["state_file"])
        self.csv_store = CSVStore(DATA_DIR)
        self.mysql_store = MySQLStore(load_mysql_config())
        self.mqtt_manager = MQTTManager(load_mqtt_config())
        self.ha_discovery = None

        self.accumulators = {}
        self.devices_signature = None

    # ---------------- 设备配置热加载 ----------------

    def refresh_devices(self, force: bool = False) -> list:
        """Web 端改了设备配置后无需重启，这里自动感知。"""
        devices = [d for d in load_devices() if d.get("enabled", True)]
        signature = devices_signature(devices)
        if not force and signature == self.devices_signature:
            return devices

        self.devices_signature = signature
        # 名称 / 实体映射可能都改了，直接按新配置重建
        self.accumulators = {d["key"]: DeviceAccumulator(d, self.state_manager) for d in devices}
        self.state_manager.drop_missing({d["key"] for d in devices})

        if devices:
            logger.info("已加载 %d 个插座: %s", len(devices), ", ".join(d["name"] for d in devices))
        else:
            logger.warning("尚未配置任何插座，请在 Web 端「系统设置」里扫描并勾选设备")

        if self.ha_discovery and self.mqtt_manager.connected:
            self.ha_discovery.publish_discovery(devices)
        return devices

    # ---------------- 采集 ----------------

    def collect_and_publish(self):
        devices = self.refresh_devices()
        if not devices:
            return

        ha_config = load_ha_config()
        client = HAClient(ha_config.get("url"), ha_config.get("token"))
        if not client.configured:
            logger.warning("未配置 HA 地址或令牌，跳过本次采集")
            return

        try:
            index = ha_client.build_entity_index(client.get_states())
        except HAError as e:
            logger.error("读取 HA 实体失败: %s", e)
            return

        rows = []
        for device in devices:
            accumulator = self.accumulators.get(device["key"])
            if not accumulator:
                continue
            try:
                row = accumulator.collect(index)
                if row:
                    rows.append(row)
            except Exception as e:
                logger.error("[%s] 采集失败: %s", device.get("name"), e)

        aggregate = self.state_manager.update_aggregate()
        self.state_manager.save()

        if rows:
            self.csv_store.write_rows(rows)
            self.mysql_store.write_rows(rows)

        if self.ha_discovery and self.mqtt_manager.connected:
            self.ha_discovery.publish_state(aggregate, self.state_manager.state["devices"])

        logger.info(
            "采集完成 - 在线 %d/%d 个插座, 合计功率 %.1fW, 今日 %.4fkWh, 本月 %.4fkWh",
            len(rows),
            len(devices),
            aggregate["current_power_w"],
            aggregate["today_energy_kwh"],
            aggregate["month_energy_kwh"],
        )

    # ---------------- 运行 ----------------

    def setup(self):
        self.refresh_devices(force=True)
        if self.mqtt_manager.enabled:
            self.mqtt_manager.connect()
            time.sleep(1)
            self.ha_discovery = HomeAssistantDiscovery(self.mqtt_manager.client, DEVICE_CONFIG)
            self.ha_discovery.publish_discovery(
                [d for d in load_devices() if d.get("enabled", True)]
            )
        else:
            logger.info("未配置 MQTT，跳过 Home Assistant 自动发现")

        aggregate = self.state_manager.state.get("aggregate", {})
        logger.info("初始化完成，历史累计电量: %.4f kWh", float(aggregate.get("total_energy_kwh") or 0))

    def run(self):
        self.setup()
        if WEB_CONFIG.get("enabled", False):
            self._start_web_server()

        logger.info("开始数据采集，间隔 %d 秒", CONFIG["update_interval"])
        last_run = 0.0
        while running:
            try:
                now = time.time()
                if now - last_run >= CONFIG["update_interval"]:
                    self.collect_and_publish()
                    last_run = now
                time.sleep(0.2)
            except KeyboardInterrupt:
                logger.info("收到退出信号")
                break
            except Exception as e:
                logger.error("主循环异常: %s", e)
                time.sleep(1)

        self.cleanup()

    def _start_web_server(self):
        try:
            import sys

            sys.path.insert(0, ".")
            from web_server import WEB_CONFIG as ServerConfig
            from web_server import run_web_server_thread

            ServerConfig["host"] = WEB_CONFIG["host"]
            ServerConfig["port"] = WEB_CONFIG["port"]
            ServerConfig["debug"] = WEB_CONFIG["debug"]
            run_web_server_thread()
            logger.info("Web 服务器已启动: http://localhost:%d", WEB_CONFIG["port"])
        except Exception as e:
            logger.error("启动 Web 服务器失败: %s", e)
            logger.warning("Web 功能不可用，但数据采集会继续运行")

    def cleanup(self):
        logger.info("正在关闭程序...")
        self.state_manager.save()
        self.mqtt_manager.disconnect()
        logger.info("程序已安全退出")


def signal_handler(signum, frame):
    global running
    logger.info("收到退出信号，准备关闭...")
    running = False


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    ElectricityMonitor().run()


if __name__ == "__main__":
    main()
