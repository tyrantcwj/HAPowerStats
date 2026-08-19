# -*- coding: utf-8 -*-
"""历史数据存储：CSV 与 MySQL，均按设备维度记录。

CSV 采用新文件名 records_YYYY-MM.csv（带设备列）；
老版本的 electricity_YYYY-MM.csv 只读兼容，归到 legacy_main 设备下。
"""

import csv
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

from config_store import DATA_DIR, LEGACY_DEVICE_KEY, load_mysql_config

logger = logging.getLogger(__name__)

CSV_PREFIX = "records_"
LEGACY_CSV_PREFIX = "electricity_"
CSV_HEADER = [
    "timestamp",
    "device_key",
    "device_name",
    "power_w",
    "total_energy_kwh",
    "voltage_v",
    "current_a",
    "temperature_c",
    "weather",
]
LEGACY_DEVICE_NAME = "默认功率实体"


def _to_date(value):
    if isinstance(value, str):
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    if isinstance(value, datetime):
        return value.date()
    return value


def csv_path(date=None, data_dir: Path = None) -> Path:
    date = _to_date(date) or datetime.now().date()
    base = Path(data_dir or DATA_DIR)
    return base / ("%s%04d-%02d.csv" % (CSV_PREFIX, date.year, date.month))


def legacy_csv_path(date=None, data_dir: Path = None) -> Path:
    date = _to_date(date) or datetime.now().date()
    base = Path(data_dir or DATA_DIR)
    return base / ("%s%04d-%02d.csv" % (LEGACY_CSV_PREFIX, date.year, date.month))


def _float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class CSVStore:
    """按月存一个文件，每次采集把所有设备各写一行。"""

    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _upgrade_header(self, path: Path):
        """老文件没有气温/天气列，补齐表头后再往里追加，避免列错位。"""
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None or header == CSV_HEADER:
                    return
                if not set(header).issubset(set(CSV_HEADER)):
                    return
                rows = list(reader)
        except Exception as e:
            logger.error("检查 CSV 表头失败: %s", e)
            return

        try:
            index_of = {name: header.index(name) for name in header}
            tmp = path.with_suffix(".upgrading")
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADER)
                for row in rows:
                    writer.writerow([
                        row[index_of[name]] if name in index_of and index_of[name] < len(row) else ""
                        for name in CSV_HEADER
                    ])
            tmp.replace(path)
            logger.info("已为 %s 补上气温/天气列", path.name)
        except Exception as e:
            logger.error("升级 CSV 表头失败: %s", e)

    def write_rows(self, rows: list):
        if not rows:
            return
        with self.lock:
            try:
                path = csv_path(data_dir=self.data_dir)
                exists = path.exists()
                if exists:
                    self._upgrade_header(path)
                with open(path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if not exists:
                        writer.writerow(CSV_HEADER)
                    for row in rows:
                        writer.writerow([
                            row.get("timestamp"),
                            row.get("device_key"),
                            row.get("device_name"),
                            "" if row.get("power_w") is None else round(row["power_w"], 2),
                            "" if row.get("total_energy_kwh") is None else round(row["total_energy_kwh"], 4),
                            "" if row.get("voltage_v") is None else round(row["voltage_v"], 1),
                            "" if row.get("current_a") is None else round(row["current_a"], 3),
                            "" if row.get("temperature_c") is None else round(row["temperature_c"], 1),
                            row.get("weather") or "",
                        ])
            except Exception as e:
                logger.error("写入 CSV 失败: %s", e)

    def read_day(self, date, device_key=None) -> list:
        """读某一天的记录；device_key 为空表示所有设备。"""
        date = _to_date(date)
        date_str = date.strftime("%Y-%m-%d")
        records = []

        with self.lock:
            path = csv_path(date, self.data_dir)
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for row in csv.DictReader(f):
                            if not (row.get("timestamp") or "").startswith(date_str):
                                continue
                            key = row.get("device_key") or LEGACY_DEVICE_KEY
                            if device_key and key != device_key:
                                continue
                            records.append({
                                "timestamp": row["timestamp"],
                                "device_key": key,
                                "device_name": row.get("device_name") or key,
                                "power_w": _float(row.get("power_w"), 0.0),
                                "total_energy_kwh": _float(row.get("total_energy_kwh"), 0.0),
                                "voltage_v": _float(row.get("voltage_v")),
                                "current_a": _float(row.get("current_a")),
                                "temperature_c": _float(row.get("temperature_c")),
                                "weather": row.get("weather") or "",
                            })
                except Exception as e:
                    logger.error("读取 CSV 失败: %s", e)

            # 老格式文件（无设备列）
            if not device_key or device_key == LEGACY_DEVICE_KEY:
                legacy = legacy_csv_path(date, self.data_dir)
                if legacy.exists():
                    try:
                        with open(legacy, "r", encoding="utf-8") as f:
                            for row in csv.DictReader(f):
                                if not (row.get("timestamp") or "").startswith(date_str):
                                    continue
                                records.append({
                                    "timestamp": row["timestamp"],
                                    "device_key": LEGACY_DEVICE_KEY,
                                    "device_name": LEGACY_DEVICE_NAME,
                                    "power_w": _float(row.get("power_w"), 0.0),
                                    "total_energy_kwh": _float(row.get("total_energy_kwh"), 0.0),
                                    "voltage_v": None,
                                    "current_a": None,
                                    "temperature_c": None,
                                    "weather": "",
                                })
                    except Exception as e:
                        logger.error("读取旧 CSV 失败: %s", e)

        records.sort(key=lambda r: r["timestamp"])
        return records

    def read_month(self, year: int, month: int, device_key=None) -> list:
        """读整月的记录，给月度报表用。"""
        records = []
        with self.lock:
            for path, legacy in (
                (self.data_dir / ("%s%04d-%02d.csv" % (CSV_PREFIX, year, month)), False),
                (self.data_dir / ("%s%04d-%02d.csv" % (LEGACY_CSV_PREFIX, year, month)), True),
            ):
                if not path.exists():
                    continue
                if legacy and device_key and device_key != LEGACY_DEVICE_KEY:
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for row in csv.DictReader(f):
                            key = (row.get("device_key") or LEGACY_DEVICE_KEY) if not legacy else LEGACY_DEVICE_KEY
                            if device_key and key != device_key:
                                continue
                            records.append({
                                "timestamp": row.get("timestamp"),
                                "device_key": key,
                                "device_name": (row.get("device_name") if not legacy else LEGACY_DEVICE_NAME) or key,
                                "power_w": _float(row.get("power_w"), 0.0),
                                "total_energy_kwh": _float(row.get("total_energy_kwh"), 0.0),
                                "voltage_v": _float(row.get("voltage_v")),
                                "current_a": _float(row.get("current_a")),
                                "temperature_c": _float(row.get("temperature_c")),
                                "weather": row.get("weather") or "",
                            })
                except Exception as e:
                    logger.error("读取月度 CSV 失败: %s", e)
        records.sort(key=lambda r: r["timestamp"] or "")
        return records

    def read_range(self, start, end, device_key=None) -> list:
        """读一个日期区间（含首尾），跨月自动拼接。"""
        start = _to_date(start)
        end = _to_date(end)
        if start > end:
            start, end = end, start

        records = []
        cursor = start.replace(day=1)
        while cursor <= end:
            for record in self.read_month(cursor.year, cursor.month, device_key):
                stamp = (record.get("timestamp") or "")[:10]
                if stamp and start.isoformat() <= stamp <= end.isoformat():
                    records.append(record)
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        records.sort(key=lambda r: r["timestamp"] or "")
        return records

    def available_dates(self, year: int, month: int) -> list:
        dates = set()
        for path in (
            self.data_dir / ("%s%04d-%02d.csv" % (CSV_PREFIX, year, month)),
            self.data_dir / ("%s%04d-%02d.csv" % (LEGACY_CSV_PREFIX, year, month)),
        ):
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        stamp = row.get("timestamp") or ""
                        if len(stamp) >= 10:
                            dates.add(stamp[:10])
            except Exception as e:
                logger.error("读取 CSV 日期失败: %s", e)
        return sorted(dates)


class MySQLStore:
    """可选的 MySQL 长期存储，表结构带设备维度。"""

    def __init__(self, config: dict = None, init: bool = True):
        self.config = config if config is not None else load_mysql_config()
        self.enabled = bool((self.config.get("host") or "").strip())
        self.lock = threading.Lock()
        self.summary_enabled = True
        if self.enabled and init:
            self._init_database()

    def _connect(self):
        try:
            import pymysql

            return pymysql.connect(
                host=self.config["host"],
                port=int(self.config.get("port") or 3306),
                user=self.config.get("user") or "",
                password=self.config.get("password") or "",
                database=self.config.get("database") or "electricity_monitor",
                charset="utf8mb4",
                autocommit=True,
                connect_timeout=8,
            )
        except Exception as e:
            logger.error("MySQL 连接失败: %s", e)
            return None

    def _column_exists(self, cursor, table: str, column: str) -> bool:
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
            """,
            (table, column),
        )
        return bool(cursor.fetchone()[0])

    def _index_exists(self, cursor, table: str, index: str) -> bool:
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
            """,
            (table, index),
        )
        return bool(cursor.fetchone()[0])

    def _init_database(self):
        conn = self._connect()
        if not conn:
            self.enabled = False
            return
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS electricity_records (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    record_time DATETIME NOT NULL,
                    device_key VARCHAR(64) NOT NULL DEFAULT 'legacy_main',
                    device_name VARCHAR(128) DEFAULT '',
                    power_w DECIMAL(10,2) NOT NULL,
                    total_energy_kwh DECIMAL(12,4) NOT NULL,
                    voltage_v DECIMAL(8,2) NULL,
                    current_a DECIMAL(8,3) NULL,
                    temperature_c DECIMAL(5,1) NULL,
                    weather VARCHAR(32) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_record_time (record_time),
                    INDEX idx_device_time (device_key, record_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 老库升级：补齐设备维度的列
            for column, ddl in (
                ("device_key", "ADD COLUMN device_key VARCHAR(64) NOT NULL DEFAULT 'legacy_main'"),
                ("device_name", "ADD COLUMN device_name VARCHAR(128) DEFAULT ''"),
                ("voltage_v", "ADD COLUMN voltage_v DECIMAL(8,2) NULL"),
                ("current_a", "ADD COLUMN current_a DECIMAL(8,3) NULL"),
                ("temperature_c", "ADD COLUMN temperature_c DECIMAL(5,1) NULL"),
                ("weather", "ADD COLUMN weather VARCHAR(32) NULL"),
            ):
                if not self._column_exists(cursor, "electricity_records", column):
                    cursor.execute("ALTER TABLE electricity_records " + ddl)
                    logger.info("MySQL 表升级：electricity_records 增加 %s", column)
            if not self._index_exists(cursor, "electricity_records", "idx_device_time"):
                cursor.execute(
                    "ALTER TABLE electricity_records ADD INDEX idx_device_time (device_key, record_time)"
                )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_summary (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    record_date DATE NOT NULL,
                    device_key VARCHAR(64) NOT NULL DEFAULT 'legacy_main',
                    device_name VARCHAR(128) DEFAULT '',
                    max_power_w DECIMAL(10,2) DEFAULT 0,
                    min_power_w DECIMAL(10,2) DEFAULT 0,
                    total_power_w DECIMAL(14,2) DEFAULT 0,
                    record_count INT DEFAULT 0,
                    total_energy_kwh DECIMAL(12,4) DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_date_device (record_date, device_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            self._upgrade_daily_summary(cursor)

            cursor.close()
            conn.close()
            logger.info("MySQL 初始化完成: %s", self.config.get("database"))
        except Exception as e:
            logger.error("MySQL 初始化失败: %s", e)
            self.enabled = False

    def _upgrade_daily_summary(self, cursor):
        """老库的 daily_summary 是按日期唯一的，改成按“日期+设备”唯一。"""
        try:
            for column, ddl in (
                ("device_key", "ADD COLUMN device_key VARCHAR(64) NOT NULL DEFAULT 'legacy_main'"),
                ("device_name", "ADD COLUMN device_name VARCHAR(128) DEFAULT ''"),
            ):
                if not self._column_exists(cursor, "daily_summary", column):
                    cursor.execute("ALTER TABLE daily_summary " + ddl)
            if not self._index_exists(cursor, "daily_summary", "uk_date_device"):
                if self._index_exists(cursor, "daily_summary", "record_date"):
                    cursor.execute("ALTER TABLE daily_summary DROP INDEX record_date")
                cursor.execute(
                    "ALTER TABLE daily_summary ADD UNIQUE KEY uk_date_device (record_date, device_key)"
                )
                logger.info("MySQL 表升级：daily_summary 改为按日期+设备唯一")
        except Exception as e:
            # 升级失败不影响明细写入，只关掉汇总表
            self.summary_enabled = False
            logger.warning("daily_summary 升级失败，已停用日汇总写入: %s", e)

    def write_rows(self, rows: list):
        if not self.enabled or not rows:
            return
        with self.lock:
            conn = self._connect()
            if not conn:
                return
            try:
                cursor = conn.cursor()
                cursor.executemany(
                    """
                    INSERT INTO electricity_records
                        (record_time, device_key, device_name, power_w, total_energy_kwh,
                         voltage_v, current_a, temperature_c, weather)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            row.get("timestamp"),
                            row.get("device_key"),
                            row.get("device_name"),
                            row.get("power_w") or 0.0,
                            row.get("total_energy_kwh") or 0.0,
                            row.get("voltage_v"),
                            row.get("current_a"),
                            row.get("temperature_c"),
                            (row.get("weather") or "")[:32],
                        )
                        for row in rows
                    ],
                )

                if self.summary_enabled:
                    today = datetime.now().date()
                    cursor.executemany(
                        """
                        INSERT INTO daily_summary
                            (record_date, device_key, device_name, max_power_w, min_power_w,
                             total_power_w, record_count, total_energy_kwh)
                        VALUES (%s, %s, %s, %s, %s, %s, 1, %s)
                        ON DUPLICATE KEY UPDATE
                            device_name = VALUES(device_name),
                            max_power_w = GREATEST(max_power_w, VALUES(max_power_w)),
                            min_power_w = LEAST(min_power_w, VALUES(min_power_w)),
                            total_power_w = total_power_w + VALUES(total_power_w),
                            record_count = record_count + 1,
                            total_energy_kwh = VALUES(total_energy_kwh)
                        """,
                        [
                            (
                                today,
                                row.get("device_key"),
                                row.get("device_name"),
                                row.get("power_w") or 0.0,
                                row.get("power_w") or 0.0,
                                row.get("power_w") or 0.0,
                                row.get("total_energy_kwh") or 0.0,
                            )
                            for row in rows
                        ],
                    )
                cursor.close()
                conn.close()
            except Exception as e:
                logger.error("写入 MySQL 失败: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass

    def read_month(self, year: int, month: int, device_key=None) -> list:
        if not self.enabled:
            return []
        prefix = "%04d-%02d" % (year, month)
        with self.lock:
            conn = self._connect()
            if not conn:
                return []
            try:
                cursor = conn.cursor()
                sql = """
                    SELECT record_time, device_key, device_name, power_w,
                           total_energy_kwh, voltage_v, current_a, temperature_c, weather
                    FROM electricity_records
                    WHERE DATE_FORMAT(record_time, '%%Y-%%m') = %s
                """
                params = [prefix]
                if device_key:
                    sql += " AND device_key = %s"
                    params.append(device_key)
                sql += " ORDER BY record_time"
                cursor.execute(sql, params)
                records = [{
                    "timestamp": row[0].strftime("%Y-%m-%d %H:%M:%S"),
                    "device_key": row[1] or LEGACY_DEVICE_KEY,
                    "device_name": row[2] or (row[1] or LEGACY_DEVICE_KEY),
                    "power_w": float(row[3] or 0),
                    "total_energy_kwh": float(row[4] or 0),
                    "voltage_v": float(row[5]) if row[5] is not None else None,
                    "current_a": float(row[6]) if row[6] is not None else None,
                    "temperature_c": float(row[7]) if row[7] is not None else None,
                    "weather": row[8] or "",
                } for row in cursor.fetchall()]
                cursor.close()
                conn.close()
                return records
            except Exception as e:
                logger.error("读取 MySQL 月度数据失败: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass
                return []

    def read_range(self, start, end, device_key=None) -> list:
        if not self.enabled:
            return []
        start_str = _to_date(start).strftime("%Y-%m-%d")
        end_str = _to_date(end).strftime("%Y-%m-%d")
        with self.lock:
            conn = self._connect()
            if not conn:
                return []
            try:
                cursor = conn.cursor()
                sql = """
                    SELECT record_time, device_key, device_name, power_w,
                           total_energy_kwh, voltage_v, current_a, temperature_c, weather
                    FROM electricity_records
                    WHERE DATE(record_time) BETWEEN %s AND %s
                """
                params = [start_str, end_str]
                if device_key:
                    sql += " AND device_key = %s"
                    params.append(device_key)
                sql += " ORDER BY record_time"
                cursor.execute(sql, params)
                records = [{
                    "timestamp": row[0].strftime("%Y-%m-%d %H:%M:%S"),
                    "device_key": row[1] or LEGACY_DEVICE_KEY,
                    "device_name": row[2] or (row[1] or LEGACY_DEVICE_KEY),
                    "power_w": float(row[3] or 0),
                    "total_energy_kwh": float(row[4] or 0),
                    "voltage_v": float(row[5]) if row[5] is not None else None,
                    "current_a": float(row[6]) if row[6] is not None else None,
                    "temperature_c": float(row[7]) if row[7] is not None else None,
                    "weather": row[8] or "",
                } for row in cursor.fetchall()]
                cursor.close()
                conn.close()
                return records
            except Exception as e:
                logger.error("读取 MySQL 区间数据失败: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass
                return []

    def read_day(self, date, device_key=None) -> list:
        if not self.enabled:
            return []
        date_str = _to_date(date).strftime("%Y-%m-%d")
        with self.lock:
            conn = self._connect()
            if not conn:
                return []
            try:
                cursor = conn.cursor()
                sql = """
                    SELECT record_time, device_key, device_name, power_w,
                           total_energy_kwh, voltage_v, current_a, temperature_c, weather
                    FROM electricity_records
                    WHERE DATE(record_time) = %s
                """
                params = [date_str]
                if device_key:
                    sql += " AND device_key = %s"
                    params.append(device_key)
                sql += " ORDER BY record_time"
                cursor.execute(sql, params)

                records = []
                for row in cursor.fetchall():
                    records.append({
                        "timestamp": row[0].strftime("%Y-%m-%d %H:%M:%S"),
                        "device_key": row[1] or LEGACY_DEVICE_KEY,
                        "device_name": row[2] or (row[1] or LEGACY_DEVICE_KEY),
                        "power_w": float(row[3] or 0),
                        "total_energy_kwh": float(row[4] or 0),
                        "voltage_v": float(row[5]) if row[5] is not None else None,
                        "current_a": float(row[6]) if row[6] is not None else None,
                        "temperature_c": float(row[7]) if row[7] is not None else None,
                        "weather": row[8] or "",
                    })
                cursor.close()
                conn.close()
                return records
            except Exception as e:
                logger.error("读取 MySQL 失败: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass
                return []


def read_day_records(date, device_key=None) -> list:
    """Web 端查询入口：优先 MySQL，没有数据则回退 CSV。"""
    mysql_config = load_mysql_config()
    if (mysql_config.get("host") or "").strip():
        try:
            records = MySQLStore(mysql_config, init=False).read_day(date, device_key)
            if records:
                return records
        except Exception as e:
            logger.warning("MySQL 查询失败，回退 CSV: %s", e)
    return CSVStore().read_day(date, device_key)


def read_range_records(start, end, device_key=None) -> list:
    """按日期区间取记录，导出报表用。"""
    mysql_config = load_mysql_config()
    if (mysql_config.get("host") or "").strip():
        try:
            records = MySQLStore(mysql_config, init=False).read_range(start, end, device_key)
            if records:
                return records
        except Exception as e:
            logger.warning("MySQL 区间查询失败，回退 CSV: %s", e)
    return CSVStore().read_range(start, end, device_key)


def read_month_records(year: int, month: int, device_key=None) -> list:
    """整月记录，导出月度报表用。"""
    mysql_config = load_mysql_config()
    if (mysql_config.get("host") or "").strip():
        try:
            records = MySQLStore(mysql_config, init=False).read_month(year, month, device_key)
            if records:
                return records
        except Exception as e:
            logger.warning("MySQL 月度查询失败，回退 CSV: %s", e)
    return CSVStore().read_month(year, month, device_key)
