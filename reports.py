# -*- coding: utf-8 -*-
"""导出报表：把采集到的明细整理成 Excel 直接能看的 CSV。

三种报表：
    detail  明细      —— 每个采样点一行，可按间隔抽样
    hourly  按小时    —— 某一天每小时一段
    daily   按天      —— 某个月每天一行

电量不是拿累计计数器相减算的（小米插座的计数器是整度跳变，差值会一格一格地跳），
而是用功率对时间积分，结果平滑得多。
"""

import csv
import io
from datetime import datetime

# 相邻采样间隔超过这个秒数就不积分，避免程序停机期间被算成一直在耗电
MAX_GAP_SECONDS = 300

REPORT_TYPES = ("detail", "hourly", "daily")
REPORT_LABELS = {
    "detail": "用电明细",
    "hourly": "分时统计",
    "daily": "每日统计",
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


def _integrate(records: list) -> dict:
    """按设备把功率积分成电量，返回 {(设备, 采样点索引): 该点覆盖的电量}。

    每个采样点承担「它到下一个采样点之间」的电量。
    """
    energy_at = {}
    by_device = {}
    for record in records:
        by_device.setdefault(record["device_key"], []).append(record)

    for key, items in by_device.items():
        items.sort(key=lambda r: r["timestamp"] or "")
        for index, record in enumerate(items[:-1]):
            start = _parse_time(record["timestamp"])
            end = _parse_time(items[index + 1]["timestamp"])
            if not start or not end:
                continue
            elapsed = (end - start).total_seconds()
            if elapsed <= 0 or elapsed > MAX_GAP_SECONDS:
                continue
            power = float(record.get("power_w") or 0)
            energy_at[(key, record["timestamp"])] = power * elapsed / 3600.0 / 1000.0
    return energy_at


def _bucket_key(record, bucket: str) -> str:
    moment = _parse_time(record["timestamp"])
    if not moment:
        return ""
    if bucket == "hour":
        return moment.strftime("%Y-%m-%d %H:00")
    return moment.strftime("%Y-%m-%d")


def _write(rows: list, header: list) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def build_detail(records: list, interval: int = 0) -> str:
    """明细表：时间、插座、功率、电压、电流、累计电量。"""
    header = ["时间", "插座", "功率(W)", "电压(V)", "电流(A)", "累计电量(kWh)"]
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
        ])
    return _write(rows, header)


def build_buckets(records: list, bucket: str, price: float = 0.0) -> str:
    """分时 / 每日统计：每段每个插座一行，末尾附全屋合计。"""
    time_label = "时段" if bucket == "hour" else "日期"
    header = [time_label, "插座", "用电量(kWh)", "平均功率(W)", "最大功率(W)", "采样点数"]
    if price > 0:
        header.append("电费(元)")

    energy_at = _integrate(records)
    stats = {}
    device_names = {}
    buckets_seen = []

    for record in records:
        slot = _bucket_key(record, bucket)
        if not slot:
            continue
        key = record["device_key"]
        device_names[key] = record.get("device_name") or key
        if slot not in stats:
            stats[slot] = {}
            buckets_seen.append(slot)
        entry = stats[slot].setdefault(key, {"energy": 0.0, "sum": 0.0, "max": 0.0, "count": 0})
        power = float(record.get("power_w") or 0)
        entry["energy"] += energy_at.get((key, record["timestamp"]), 0.0)
        entry["sum"] += power
        entry["max"] = max(entry["max"], power)
        entry["count"] += 1

    rows = []
    grand = {"energy": 0.0, "sum": 0.0, "max": 0.0, "count": 0}

    for slot in sorted(set(buckets_seen)):
        slot_total = {"energy": 0.0, "sum": 0.0, "max": 0.0, "count": 0}
        for key in sorted(stats[slot], key=lambda k: device_names.get(k, k)):
            entry = stats[slot][key]
            rows.append(_stat_row(_slot_text(slot, bucket), device_names.get(key, key), entry, price))
            slot_total["energy"] += entry["energy"]
            slot_total["sum"] += entry["sum"]
            slot_total["max"] = max(slot_total["max"], entry["max"])
            slot_total["count"] += entry["count"]

        if len(stats[slot]) > 1:
            rows.append(_stat_row(_slot_text(slot, bucket), TOTAL_LABEL, slot_total, price))

        grand["energy"] += slot_total["energy"]
        grand["sum"] += slot_total["sum"]
        grand["max"] = max(grand["max"], slot_total["max"])
        grand["count"] += slot_total["count"]

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
    return row


def build_csv(report_type: str, records: list, interval: int = 0, price: float = 0.0) -> str:
    if report_type == "hourly":
        return build_buckets(records, "hour", price)
    if report_type == "daily":
        return build_buckets(records, "day", price)
    return build_detail(records, interval)
