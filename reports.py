# -*- coding: utf-8 -*-
"""导出报表：把采集到的明细整理成 Excel 直接能看的 CSV。

四种报表（都按日期区间导出）：
    sessions 使用时段 —— 每次电器启动到停止算一段，附当时的气温和天气
    hourly   分时统计 —— 每小时一段
    daily    每日统计 —— 每天一行
    detail   用电明细 —— 原始采样点，可抽样

电量一律用功率对时间积分算，不拿累计计数器相减（小米插座的计数器是整度
跳变，相减出来一格一格地跳）。
"""

import csv
import io
from collections import Counter
from datetime import datetime

# 相邻采样间隔超过这个秒数就不积分，避免程序停机期间被算成一直在耗电
MAX_GAP_SECONDS = 300

# 使用时段识别的默认参数
DEFAULT_STANDBY_W = 5.0        # 超过这个功率算「在工作」
DEFAULT_MIN_MINUTES = 2.0      # 短于这个时长的忽略，滤掉抖动
DEFAULT_MERGE_MINUTES = 10.0   # 中间停这么久以内算同一次使用（洗衣机中途暂停）

REPORT_TYPES = ("sessions", "hourly", "daily", "detail")
REPORT_LABELS = {
    "sessions": "使用时段",
    "hourly": "分时统计",
    "daily": "每日统计",
    "detail": "用电明细",
}
TOTAL_LABEL = "全屋合计"


def _parse_time(text):
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _round(value, digits):
    if value is None:
        return ""
    return round(float(value), digits)


def _by_device(records: list) -> dict:
    grouped = {}
    for record in records:
        grouped.setdefault(record["device_key"], []).append(record)
    for items in grouped.values():
        items.sort(key=lambda r: r["timestamp"] or "")
    return grouped


def _weather_summary(samples: list):
    """一段时间内的平均气温和主要天气。"""
    temps = [float(s["temperature_c"]) for s in samples
             if s.get("temperature_c") not in (None, "")]
    texts = [str(s.get("weather")).strip() for s in samples if str(s.get("weather") or "").strip()]
    average = round(sum(temps) / len(temps), 1) if temps else None
    dominant = Counter(texts).most_common(1)[0][0] if texts else ""
    return average, dominant


def _integrate(records: list) -> dict:
    """按设备把功率积分成电量，返回 {(设备, 时间戳): 该采样点覆盖的电量}。"""
    energy_at = {}
    for key, items in _by_device(records).items():
        for index, record in enumerate(items[:-1]):
            start = _parse_time(record["timestamp"])
            end = _parse_time(items[index + 1]["timestamp"])
            if not start or not end:
                continue
            elapsed = (end - start).total_seconds()
            if elapsed <= 0 or elapsed > MAX_GAP_SECONDS:
                continue
            energy_at[(key, record["timestamp"])] = float(record.get("power_w") or 0) * elapsed / 3600.0 / 1000.0
    return energy_at


def _write(rows: list, header: list) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


# =============================================================================
# 使用时段
# =============================================================================

def detect_sessions(records: list, thresholds: dict = None, standby_w: float = DEFAULT_STANDBY_W,
                    min_minutes: float = DEFAULT_MIN_MINUTES,
                    merge_minutes: float = DEFAULT_MERGE_MINUTES) -> list:
    """把连续的「在工作」采样合并成一次次使用。

    功率高于阈值算启动；中途掉到阈值以下但在 merge_minutes 之内又起来，
    算同一次使用（洗衣机漂洗间歇、空调压缩机停机都属于这种）。
    """
    thresholds = thresholds or {}
    energy_at = _integrate(records)
    sessions = []

    for key, items in _by_device(records).items():
        threshold = float(thresholds.get(key, standby_w))
        merge_seconds = merge_minutes * 60.0
        current = None

        for index, record in enumerate(items):
            moment = _parse_time(record["timestamp"])
            if not moment:
                continue
            power = float(record.get("power_w") or 0)
            energy = energy_at.get((key, record["timestamp"]), 0.0)
            working = power > threshold

            if working:
                if current is None:
                    current = {
                        "device_key": key,
                        "device_name": record.get("device_name") or key,
                        "start": moment,
                        "last_on": moment,
                        "energy": 0.0,
                        "power_sum": 0.0,
                        "power_max": 0.0,
                        "count": 0,
                        "samples": [],
                    }
                current["last_on"] = moment
                current["power_sum"] += power
                current["power_max"] = max(current["power_max"], power)
                current["count"] += 1
            elif current is not None and (moment - current["last_on"]).total_seconds() > merge_seconds:
                sessions.append(current)
                current = None

            if current is not None:
                # 时段内的电量全都算进去，包括中途待机的那点
                current["energy"] += energy
                current["samples"].append(record)

        if current is not None:
            sessions.append(current)

    result = []
    for session in sessions:
        # 结束时间取最后一个在工作的采样点
        duration = (session["last_on"] - session["start"]).total_seconds() / 60.0
        if duration < min_minutes:
            continue
        average_temp, weather_text = _weather_summary(session["samples"])
        result.append({
            "device_key": session["device_key"],
            "device_name": session["device_name"],
            "start": session["start"],
            "end": session["last_on"],
            "minutes": round(duration, 1),
            "energy_kwh": round(session["energy"], 4),
            "avg_power_w": round(session["power_sum"] / session["count"], 2) if session["count"] else 0.0,
            "max_power_w": round(session["power_max"], 2),
            "temperature_c": average_temp,
            "weather": weather_text,
        })

    result.sort(key=lambda s: (s["start"], s["device_name"]))
    return result


def build_sessions(records: list, thresholds: dict = None, standby_w: float = DEFAULT_STANDBY_W,
                   min_minutes: float = DEFAULT_MIN_MINUTES,
                   merge_minutes: float = DEFAULT_MERGE_MINUTES, price: float = 0.0) -> str:
    header = ["日期", "插座", "开始时间", "结束时间", "时长(分钟)", "用电量(kWh)",
              "平均功率(W)", "最大功率(W)", "平均气温(℃)", "天气"]
    if price > 0:
        header.append("电费(元)")

    sessions = detect_sessions(records, thresholds, standby_w, min_minutes, merge_minutes)
    rows = []
    for session in sessions:
        row = [
            session["start"].strftime("%Y-%m-%d"),
            session["device_name"],
            session["start"].strftime("%H:%M"),
            session["end"].strftime("%H:%M"),
            session["minutes"],
            session["energy_kwh"],
            session["avg_power_w"],
            session["max_power_w"],
            "" if session["temperature_c"] is None else session["temperature_c"],
            session["weather"],
        ]
        if price > 0:
            row.append(round(session["energy_kwh"] * price, 2))
        rows.append(row)

    if not rows:
        rows.append(["（该时间段内没有识别到启动记录，可调低「启动判定功率」后重试）"])
        return _write(rows, header)

    # 底部按「日期 + 插座」汇总，方便直接看每天用了几次、共多少度
    summary = {}
    for session in sessions:
        key = (session["start"].strftime("%Y-%m-%d"), session["device_name"])
        entry = summary.setdefault(key, {"count": 0, "minutes": 0.0, "energy": 0.0,
                                         "temps": [], "weathers": []})
        entry["count"] += 1
        entry["minutes"] += session["minutes"]
        entry["energy"] += session["energy_kwh"]
        if session["temperature_c"] is not None:
            entry["temps"].append(session["temperature_c"])
        if session["weather"]:
            entry["weathers"].append(session["weather"])

    rows.append([])
    rows.append(["【每日汇总】", "插座", "启动次数", "累计时长(分钟)", "用电量(kWh)",
                 "平均气温(℃)", "天气"] + (["电费(元)"] if price > 0 else []))
    for (day, name) in sorted(summary):
        entry = summary[(day, name)]
        row = [
            day,
            name,
            entry["count"],
            round(entry["minutes"], 1),
            round(entry["energy"], 4),
            round(sum(entry["temps"]) / len(entry["temps"]), 1) if entry["temps"] else "",
            Counter(entry["weathers"]).most_common(1)[0][0] if entry["weathers"] else "",
        ]
        if price > 0:
            row.append(round(entry["energy"] * price, 2))
        rows.append(row)

    return _write(rows, header)


# =============================================================================
# 明细 / 分时 / 每日
# =============================================================================

def build_detail(records: list, interval: int = 0) -> str:
    header = ["时间", "插座", "功率(W)", "电压(V)", "电流(A)", "累计电量(kWh)", "气温(℃)", "天气"]
    rows = []
    last_kept = {}

    for record in records:
        key = record["device_key"]
        moment = _parse_time(record["timestamp"])
        if interval and moment:
            previous = last_kept.get(key)
            if previous and (moment - previous).total_seconds() < interval:
                continue
            last_kept[key] = moment
        rows.append([
            record["timestamp"],
            record.get("device_name") or key,
            _round(record.get("power_w"), 2),
            _round(record.get("voltage_v"), 1),
            _round(record.get("current_a"), 3),
            _round(record.get("total_energy_kwh"), 3),
            _round(record.get("temperature_c"), 1),
            record.get("weather") or "",
        ])
    return _write(rows, header)


def build_buckets(records: list, bucket: str, price: float = 0.0) -> str:
    """分时 / 每日统计：每段每个插座一行，多插座时附本段合计。"""
    time_label = "时段" if bucket == "hour" else "日期"
    header = [time_label, "插座", "用电量(kWh)", "平均功率(W)", "最大功率(W)",
              "采样点数", "平均气温(℃)", "天气"]
    if price > 0:
        header.insert(6, "电费(元)")

    energy_at = _integrate(records)
    stats = {}
    device_names = {}

    for record in records:
        moment = _parse_time(record["timestamp"])
        if not moment:
            continue
        slot = moment.strftime("%Y-%m-%d %H:00") if bucket == "hour" else moment.strftime("%Y-%m-%d")
        key = record["device_key"]
        device_names[key] = record.get("device_name") or key
        entry = stats.setdefault(slot, {}).setdefault(
            key, {"energy": 0.0, "sum": 0.0, "max": 0.0, "count": 0, "samples": []})
        power = float(record.get("power_w") or 0)
        entry["energy"] += energy_at.get((key, record["timestamp"]), 0.0)
        entry["sum"] += power
        entry["max"] = max(entry["max"], power)
        entry["count"] += 1
        entry["samples"].append(record)

    rows = []
    grand = {"energy": 0.0, "sum": 0.0, "max": 0.0, "count": 0, "samples": []}

    for slot in sorted(stats):
        slot_total = {"energy": 0.0, "sum": 0.0, "max": 0.0, "count": 0, "samples": []}
        for key in sorted(stats[slot], key=lambda k: device_names.get(k, k)):
            entry = stats[slot][key]
            rows.append(_stat_row(_slot_text(slot, bucket), device_names.get(key, key), entry, price))
            slot_total["energy"] += entry["energy"]
            slot_total["sum"] += entry["sum"]
            slot_total["max"] = max(slot_total["max"], entry["max"])
            slot_total["count"] += entry["count"]
            slot_total["samples"].extend(entry["samples"])

        if len(stats[slot]) > 1:
            rows.append(_stat_row(_slot_text(slot, bucket), TOTAL_LABEL, slot_total, price))

        grand["energy"] += slot_total["energy"]
        grand["sum"] += slot_total["sum"]
        grand["max"] = max(grand["max"], slot_total["max"])
        grand["count"] += slot_total["count"]
        grand["samples"].extend(slot_total["samples"])

    if rows:
        rows.append([])
        rows.append(_stat_row("合计", TOTAL_LABEL, grand, price))

    return _write(rows, header)


def _slot_text(slot: str, bucket: str) -> str:
    if bucket != "hour":
        return slot
    start = _parse_time(slot + ":00")
    if not start:
        return slot
    return "%s %02d:00-%02d:00" % (start.strftime("%Y-%m-%d"), start.hour, (start.hour + 1) % 24)


def _stat_row(slot_text: str, name: str, entry: dict, price: float) -> list:
    average = entry["sum"] / entry["count"] if entry["count"] else 0.0
    average_temp, weather_text = _weather_summary(entry.get("samples") or [])
    row = [
        slot_text,
        name,
        round(entry["energy"], 4),
        round(average, 2),
        round(entry["max"], 2),
        entry["count"],
    ]
    if price > 0:
        row.append(round(entry["energy"] * price, 2))
    row.append("" if average_temp is None else average_temp)
    row.append(weather_text)
    return row


def build_csv(report_type: str, records: list, interval: int = 0, price: float = 0.0,
              thresholds: dict = None, standby_w: float = DEFAULT_STANDBY_W,
              min_minutes: float = DEFAULT_MIN_MINUTES,
              merge_minutes: float = DEFAULT_MERGE_MINUTES) -> str:
    if report_type == "sessions":
        return build_sessions(records, thresholds, standby_w, min_minutes, merge_minutes, price)
    if report_type == "hourly":
        return build_buckets(records, "hour", price)
    if report_type == "daily":
        return build_buckets(records, "day", price)
    return build_detail(records, interval)
