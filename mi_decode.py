# -*- coding: utf-8 -*-
"""小米（米家 / MIoT）插座打包寄存器解码。

小米插座会把多项数据塞进一个 32 位整数里，HA 的米家集成没有拆包，
于是在 HA 里表现为「电费提醒3 = 14418164」这种没有单位的大整数实体，
导致 HA 看到的数据和米家 App 对不上。这里按位把真实数值拆出来。

已用两台真机对照米家 App 验证：

    电费提醒3 = 14418164 -> 今日 0.55 kWh / 本月 2.44 kWh（米家：0.55 / 2.44）
    电费提醒3 = 52166863 -> 今日 1.99 kWh / 本月 2.07 kWh（米家：1.99 / 2.07）
    功率,电压 = 41101530 -> 100.34 W / 226.6 V（米家：100.78 W / 226.40 V）
    功率,电压 =   579810 ->   1.41 W / 227.4 V（米家：  1.54 W / 225.50 V）

功率/电压是实时值，采样时刻不同会有小幅波动，属正常。
"""

import re
from typing import Optional

# 解码方式 -> 中文名，前端展示用
DECODER_LABELS = {
    "mi_energy.today": "今日电量",
    "mi_energy.month": "本月电量",
    "mi_power_voltage.power": "功率",
    "mi_power_voltage.voltage": "电压",
}

# 解码出来的值对应设备的哪个角色
DECODER_ROLES = {
    "mi_energy.today": "today_energy",
    "mi_energy.month": "month_energy",
    "mi_power_voltage.power": "power",
    "mi_power_voltage.voltage": "voltage",
}

DECODER_UNITS = {
    "mi_energy.today": "kWh",
    "mi_energy.month": "kWh",
    "mi_power_voltage.power": "W",
    "mi_power_voltage.voltage": "V",
}

# 识别关键词：友好名是中文，实体 id 是拼音
ENERGY_KEYWORDS = ("电费提醒", "dian_fei_ti_xing", "dianfeitixing")
POWER_VOLTAGE_KEYWORD_PAIRS = (
    ("功率", "电压"),
    ("gong_lv", "dian_ya"),
    ("gong_shuai", "dian_ya"),
    ("gonglv", "dianya"),
    ("power", "voltage"),
)

# 合理范围，用来判断解出来的值是不是真数据（防止把配置参数当成读数）
MIN_VOLTAGE_V = 80.0
MAX_VOLTAGE_V = 300.0
MAX_POWER_W = 5000.0
MAX_MONTH_KWH = 6553.5      # 低 16 位上限
MAX_TODAY_KWH = 1638.3      # 高 14 位上限


def _to_int(state) -> Optional[int]:
    """打包寄存器一定是整数；带小数点的是正常传感器，不参与解码。"""
    if state is None:
        return None
    text = str(state).strip()
    if not re.fullmatch(r"\d{1,12}", text):
        return None
    return int(text)


def decode_energy(state) -> Optional[dict]:
    """电费提醒寄存器：高位是今日电量，低 16 位是本月电量，单位都是 0.01 kWh。"""
    raw = _to_int(state)
    if raw is None:
        return None
    today = (raw >> 18) / 100.0
    month = (raw & 0xFFFF) / 100.0
    # 本月一定包含今日，反过来说明这个寄存器装的不是电量
    if month <= 0 or month > MAX_MONTH_KWH:
        return None
    if today < 0 or today > MAX_TODAY_KWH or today > month + 0.01:
        return None
    return {"today": round(today, 2), "month": round(month, 2)}


def decode_power_voltage(state) -> Optional[dict]:
    """功率/电压寄存器：低 12 位是电压（0.1V），其余高位是功率（0.01W）。"""
    raw = _to_int(state)
    if raw is None:
        return None
    power = (raw >> 12) / 100.0
    voltage = (raw & 0xFFF) / 10.0
    if not (MIN_VOLTAGE_V <= voltage <= MAX_VOLTAGE_V):
        return None
    if power < 0 or power > MAX_POWER_W:
        return None
    return {"power": round(power, 2), "voltage": round(voltage, 1)}


def decode(spec: str, state) -> Optional[float]:
    """按解码方式取出单个数值，取不到返回 None。"""
    if spec in ("mi_energy.today", "mi_energy.month"):
        decoded = decode_energy(state)
        return None if decoded is None else decoded[spec.split(".")[1]]
    if spec in ("mi_power_voltage.power", "mi_power_voltage.voltage"):
        decoded = decode_power_voltage(state)
        return None if decoded is None else decoded[spec.split(".")[1]]
    return None


def _text_of(item: dict) -> str:
    attrs = item.get("attributes") or {}
    name = attrs.get("friendly_name") or ""
    return ("%s %s" % (item.get("entity_id") or "", name)).lower()


def _is_packed_candidate(item: dict) -> bool:
    """没有单位、状态是大整数的 sensor，才可能是打包寄存器。"""
    entity_id = item.get("entity_id") or ""
    if not entity_id.startswith("sensor."):
        return False
    attrs = item.get("attributes") or {}
    if (attrs.get("unit_of_measurement") or "").strip():
        return False
    if (attrs.get("device_class") or "").strip():
        return False
    return _to_int(item.get("state")) is not None


def looks_like_packed(item: dict) -> bool:
    """扫描阶段用：这个实体是否值得尝试解码。"""
    if not _is_packed_candidate(item):
        return False
    text = _text_of(item)
    if any(word in text for word in ENERGY_KEYWORDS):
        return True
    return any(all(part in text for part in pair) for pair in POWER_VOLTAGE_KEYWORD_PAIRS)


def detect(items: list) -> dict:
    """从一台设备的实体里找出可解码的打包寄存器。

    返回 {角色: {"entity_id", "spec", "value", "label", "unit", "source_name"}}，
    只返回解码结果通过合理性校验的项。
    """
    found = {}

    for item in items or []:
        if not _is_packed_candidate(item):
            continue
        text = _text_of(item)
        attrs = item.get("attributes") or {}
        source_name = attrs.get("friendly_name") or item.get("entity_id")

        if any(word in text for word in ENERGY_KEYWORDS):
            decoded = decode_energy(item.get("state"))
            if decoded:
                # 同类寄存器有多个时，取本月电量更大的那个（更像真实累计）
                existing = found.get("month_energy")
                if existing and existing["value"] >= decoded["month"]:
                    continue
                for spec, value in (("mi_energy.today", decoded["today"]),
                                    ("mi_energy.month", decoded["month"])):
                    found[DECODER_ROLES[spec]] = {
                        "entity_id": item.get("entity_id"),
                        "spec": spec,
                        "value": value,
                        "label": DECODER_LABELS[spec],
                        "unit": DECODER_UNITS[spec],
                        "source_name": source_name,
                    }
            continue

        if any(all(part in text for part in pair) for pair in POWER_VOLTAGE_KEYWORD_PAIRS):
            decoded = decode_power_voltage(item.get("state"))
            if decoded:
                for spec, value in (("mi_power_voltage.power", decoded["power"]),
                                    ("mi_power_voltage.voltage", decoded["voltage"])):
                    found[DECODER_ROLES[spec]] = {
                        "entity_id": item.get("entity_id"),
                        "spec": spec,
                        "value": value,
                        "label": DECODER_LABELS[spec],
                        "unit": DECODER_UNITS[spec],
                        "source_name": source_name,
                    }

    return found
