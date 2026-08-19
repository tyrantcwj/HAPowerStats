# -*- coding: utf-8 -*-
"""Home Assistant REST 客户端与插座设备自动发现。

只需要 HA 地址 + 长期访问令牌，其余（功率、电量、电压、电流、开关等实体）
全部从 HA 里自动读出来并按设备归组，供 Web 端勾选导入。
"""

import json
import logging
import re
from typing import Optional

import requests

import mi_decode

logger = logging.getLogger(__name__)

# 角色 -> 中文名，前端展示用
ROLE_LABELS = {
    "power": "功率",
    "energy": "电量",
    "voltage": "电压",
    "current": "电流",
    "power_factor": "功率因数",
    "frequency": "频率",
    "apparent_power": "视在功率",
    "switch": "开关",
    # 下面两个只会由小米打包寄存器解码得到，没有独立实体
    "today_energy": "今日电量",
    "month_energy": "本月电量",
}

# 采集时会写入状态的数值角色
NUMERIC_ROLES = (
    "power",
    "energy",
    "voltage",
    "current",
    "power_factor",
    "frequency",
    "apparent_power",
)
ALL_ROLES = NUMERIC_ROLES + ("switch",)

POWER_UNITS = {"w": 1.0, "watt": 1.0, "kw": 1000.0, "kilowatt": 1000.0, "mw": 1_000_000.0}
ENERGY_UNITS = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}
VOLTAGE_UNITS = {"v": 1.0, "mv": 0.001, "kv": 1000.0}
CURRENT_UNITS = {"a": 1.0, "ma": 0.001}

UNAVAILABLE_STATES = (None, "", "unknown", "unavailable", "none")

# 电量实体里出现这些词说明是“今日/本月”之类的分段统计，不能当累计总量
PARTIAL_ENERGY_WORDS = (
    "today",
    "daily",
    "yesterday",
    "week",
    "month",
    "year",
    "今日",
    "当日",
    "昨日",
    "本周",
    "本月",
    "本年",
    "月度",
    "年度",
)

# 归组时需要从实体 id 里剥掉的后缀（长的排前面，先匹配）
ROLE_SUFFIXES = (
    "electric_consumption_w",
    "electrical_measurement",
    "electric_consumption",
    "electric_potential",
    "apparent_power",
    "power_factor",
    "energy_total",
    "total_energy",
    "energy_today",
    "today_energy",
    "daily_energy",
    "electric_current",
    "current_power",
    "active_power",
    "power_w",
    "energy_kwh",
    "consumption",
    "frequency",
    "voltage",
    "current",
    "energy",
    "power",
)


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "http://" + url
    return url


def _unit_key(unit) -> str:
    return str(unit or "").strip().lower().replace(" ", "")


def to_float(state) -> Optional[float]:
    if state in UNAVAILABLE_STATES:
        return None
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def convert(value: Optional[float], unit, table: dict, default: float = 1.0) -> Optional[float]:
    """按单位换算到基准单位（W / kWh / V / A）。"""
    if value is None:
        return None
    return value * table.get(_unit_key(unit), default)


def to_watts(state, unit) -> Optional[float]:
    return convert(to_float(state), unit, POWER_UNITS)


def to_kwh(state, unit) -> Optional[float]:
    return convert(to_float(state), unit, ENERGY_UNITS)


def role_value(role: str, state, unit) -> Optional[float]:
    """把某个角色的原始状态换算成统一单位。"""
    if role in ("power", "apparent_power"):
        return convert(to_float(state), unit, POWER_UNITS)
    if role == "energy":
        return convert(to_float(state), unit, ENERGY_UNITS)
    if role == "voltage":
        return convert(to_float(state), unit, VOLTAGE_UNITS)
    if role == "current":
        return convert(to_float(state), unit, CURRENT_UNITS)
    return to_float(state)


class HAError(Exception):
    """HA 接口调用失败，message 直接面向用户。"""


class HAClient:
    """极简 HA REST 客户端。"""

    def __init__(self, url: str, token: str, timeout: int = 12):
        self.url = normalize_url(url)
        self.token = (token or "").strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _headers(self) -> dict:
        return {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs):
        if not self.configured:
            raise HAError("请先填写 HA 地址和访问令牌")
        timeout = kwargs.pop("timeout", self.timeout)
        try:
            response = requests.request(
                method,
                self.url + path,
                headers=self._headers(),
                timeout=timeout,
                **kwargs
            )
        except requests.exceptions.Timeout:
            raise HAError("连接超时，请检查 HA 地址是否正确")
        except requests.exceptions.ConnectionError:
            raise HAError("无法连接，请检查 HA 是否运行、地址是否正确")
        except Exception as e:
            raise HAError("请求失败: {}".format(e))

        if response.status_code == 401:
            raise HAError("令牌无效，请检查长期访问令牌")
        if response.status_code == 403:
            raise HAError("令牌无权访问该接口")
        if response.status_code == 404:
            raise HAError("接口或实体不存在: {}".format(path))
        if response.status_code >= 400:
            raise HAError("HA 返回错误: HTTP {}".format(response.status_code))
        return response

    def api_info(self) -> dict:
        return self._request("GET", "/api/").json()

    def get_states(self) -> list:
        return self._request("GET", "/api/states", timeout=max(self.timeout, 15)).json()

    def get_state(self, entity_id: str) -> dict:
        return self._request("GET", "/api/states/" + entity_id).json()

    def render_template(self, template: str) -> str:
        response = self._request(
            "POST",
            "/api/template",
            data=json.dumps({"template": template}).encode("utf-8"),
            timeout=max(self.timeout, 20),
        )
        return response.text

    def call_service(self, domain: str, service: str, data: dict):
        response = self._request(
            "POST",
            "/api/services/{}/{}".format(domain, service),
            data=json.dumps(data).encode("utf-8"),
        )
        try:
            return response.json()
        except Exception:
            return []


# =============================================================================
# 实体角色识别
# =============================================================================

def _name_of(item: dict) -> str:
    attrs = item.get("attributes") or {}
    return str(attrs.get("friendly_name") or item.get("entity_id") or "")


def _match_words(text: str, words) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


def classify_entity(item: dict) -> Optional[str]:
    """判断一个实体属于哪个角色，识别不出来返回 None。"""
    entity_id = item.get("entity_id") or ""
    domain = entity_id.split(".")[0]
    attrs = item.get("attributes") or {}
    device_class = str(attrs.get("device_class") or "").lower()
    unit = _unit_key(attrs.get("unit_of_measurement"))
    text = (entity_id + " " + _name_of(item)).lower()

    if domain in ("switch", "input_boolean"):
        return "switch"
    if domain != "sensor":
        return None

    by_device_class = {
        "power": "power",
        "energy": "energy",
        "voltage": "voltage",
        "current": "current",
        "power_factor": "power_factor",
        "frequency": "frequency",
        "apparent_power": "apparent_power",
    }
    if device_class in by_device_class:
        return by_device_class[device_class]

    if unit in ("w", "kw", "watt", "kilowatt"):
        return "power"
    if unit == "va":
        return "apparent_power"
    if unit in ENERGY_UNITS:
        return "energy"
    if unit in VOLTAGE_UNITS and _match_words(text, ("volt", "电压")):
        return "voltage"
    if unit in CURRENT_UNITS and _match_words(text, ("current", "amp", "电流")):
        return "current"
    if unit == "hz":
        return "frequency"
    if unit == "%" and _match_words(text, ("power_factor", "功率因数")):
        return "power_factor"
    return None


def _role_score(role: str, item: dict) -> int:
    """同一设备同角色有多个实体时，挑最合适的那个。"""
    attrs = item.get("attributes") or {}
    entity_id = item.get("entity_id") or ""
    text = (entity_id + " " + _name_of(item)).lower()
    device_class = str(attrs.get("device_class") or "").lower()
    state_class = str(attrs.get("state_class") or "").lower()
    score = 0

    if device_class:
        score += 20
    if item.get("state") not in UNAVAILABLE_STATES:
        score += 5

    if role == "energy":
        if state_class in ("total_increasing", "total"):
            score += 30
        if _match_words(text, PARTIAL_ENERGY_WORDS):
            score -= 40
        if _match_words(text, ("total", "累计", "总")):
            score += 10
    elif role == "power":
        if state_class == "measurement":
            score += 10
        if _match_words(text, ("max", "min", "avg", "average", "最大", "最小", "平均")):
            score -= 30
        if _match_words(text, ("apparent", "reactive", "视在", "无功")):
            score -= 20
    elif role == "switch":
        if device_class == "outlet":
            score += 15
        if entity_id.startswith("switch."):
            score += 10
        if _match_words(text, ("led", "indicator", "child_lock", "背光", "童锁", "指示灯")):
            score -= 40
    return score


def _strip_role_suffix(object_id: str) -> str:
    for suffix in ROLE_SUFFIXES:
        if object_id.endswith("_" + suffix):
            return object_id[: -len(suffix) - 1]
    return object_id


def _fallback_group_key(entity_id: str) -> str:
    """拿不到 HA 设备信息时，按实体 id 前缀归组。"""
    object_id = entity_id.split(".", 1)[-1]
    base = _strip_role_suffix(object_id)
    return re.sub(r"[^a-z0-9_]+", "", base.lower()) or object_id


def _device_map_via_template(client: HAClient, entity_ids: list) -> dict:
    """用模板接口把实体映射到 HA 设备（这样能拿到真正的设备名）。"""
    if not entity_ids:
        return {}
    template = (
        "{%- set eids = " + json.dumps(entity_ids) + " -%}"
        "[{%- for e in eids -%}"
        "{%- set d = device_id(e) -%}"
        "{{ {'e': e,"
        " 'd': d if d else '',"
        " 'n': (device_attr(d, 'name_by_user') or device_attr(d, 'name') or '') if d else '',"
        " 'm': (device_attr(d, 'model') or '') if d else '',"
        " 'b': (device_attr(d, 'manufacturer') or '') if d else ''} | tojson }}"
        "{{ ',' if not loop.last else '' }}"
        "{%- endfor -%}]"
    )
    try:
        rows = json.loads(client.render_template(template))
    except HAError as e:
        logger.info("模板接口不可用，改用实体名前缀归组: %s", e)
        return {}
    except Exception as e:
        logger.info("解析设备映射失败，改用实体名前缀归组: %s", e)
        return {}

    mapping = {}
    for row in rows if isinstance(rows, list) else []:
        entity_id = row.get("e")
        device_key = (row.get("d") or "").strip()
        if entity_id and device_key:
            mapping[entity_id] = {
                "device_id": device_key,
                "name": (row.get("n") or "").strip(),
                "model": (row.get("m") or "").strip(),
                "manufacturer": (row.get("b") or "").strip(),
            }
    return mapping


def _common_name(names: list) -> str:
    """从同一设备的多个实体名里取公共前缀作为设备名。"""
    names = [n for n in names if n]
    if not names:
        return ""
    tokens = [n.split() for n in names]
    common = []
    for index in range(min(len(t) for t in tokens)):
        word = tokens[0][index]
        if all(t[index] == word for t in tokens):
            common.append(word)
        else:
            break
    if common:
        joined = " ".join(common)
        if len(joined) >= 2:
            return joined
    return sorted(names, key=len)[0]


def build_entity_index(states: list) -> dict:
    """entity_id -> 精简后的实体信息，采集时按实体 id 直接查。"""
    index = {}
    for item in states or []:
        entity_id = item.get("entity_id")
        if not entity_id:
            continue
        attrs = item.get("attributes") or {}
        index[entity_id] = {
            "entity_id": entity_id,
            "name": _name_of(item),
            "state": item.get("state"),
            "unit": str(attrs.get("unit_of_measurement") or ""),
            "device_class": str(attrs.get("device_class") or ""),
            "state_class": str(attrs.get("state_class") or ""),
            "last_updated": item.get("last_updated"),
        }
    return index


def discover_devices(client: HAClient, states: Optional[list] = None) -> list:
    """扫描 HA，返回可作为“插座 / 计量设备”导入的候选列表。

    每项形如：
        {key, name, model, manufacturer, source,
         entities: {role: entity_id}, readings: {role: {...}}, roles: [...]}
    """
    if states is None:
        states = client.get_states()

    candidates = {}
    packed = {}
    for item in states or []:
        role = classify_entity(item)
        if role:
            candidates[item.get("entity_id")] = (role, item)
        elif mi_decode.looks_like_packed(item):
            # 小米插座把多项数据塞进一个大整数里，单独收集起来后面解码
            packed[item.get("entity_id")] = item

    device_map = _device_map_via_template(
        client, sorted(candidates.keys()) + sorted(packed.keys())
    )

    groups = {}
    for entity_id, (role, item) in candidates.items():
        info = device_map.get(entity_id)
        if info:
            key = info["device_id"]
            source = "device"
        else:
            key = "grp_" + _fallback_group_key(entity_id)
            info = {"name": "", "model": "", "manufacturer": ""}
            source = "prefix"

        group = groups.setdefault(key, {
            "key": key,
            "name": info.get("name") or "",
            "model": info.get("model") or "",
            "manufacturer": info.get("manufacturer") or "",
            "source": source,
            "members": [],
            "packed": [],
        })
        group["members"].append((role, item))
        if not group["name"] and info.get("name"):
            group["name"] = info["name"]

    for entity_id, item in packed.items():
        info = device_map.get(entity_id)
        if not info:
            # 拿不到设备归属的打包寄存器无从判断属于哪台插座，跳过
            continue
        group = groups.get(info["device_id"])
        if group is None:
            group = groups.setdefault(info["device_id"], {
                "key": info["device_id"],
                "name": info.get("name") or "",
                "model": info.get("model") or "",
                "manufacturer": info.get("manufacturer") or "",
                "source": "device",
                "members": [],
                "packed": [],
            })
        group.setdefault("packed", []).append(item)

    devices = []
    for group in groups.values():
        roles = {}
        for role, item in group["members"]:
            best = roles.get(role)
            if best is None or _role_score(role, item) > _role_score(role, best):
                roles[role] = item

        decoded = mi_decode.detect(group.get("packed") or [])

        # 既没有功率也没有电量（包括解码得到的）就不算计量插座
        has_data = "power" in roles or "energy" in roles
        has_decoded = "power" in decoded or "today_energy" in decoded
        if not has_data and not has_decoded:
            continue

        name = group["name"] or _common_name([_name_of(i) for _, i in group["members"]])
        entities = {}
        readings = {}
        for role, item in roles.items():
            attrs = item.get("attributes") or {}
            unit = attrs.get("unit_of_measurement")
            entities[role] = item.get("entity_id")
            readings[role] = {
                "entity_id": item.get("entity_id"),
                "name": _name_of(item),
                "state": item.get("state"),
                "unit": str(unit or ""),
                "value": role_value(role, item.get("state"), unit),
            }

        # 解码值只补实体缺失的角色，真实实体优先
        decoders = {}
        for role, found in decoded.items():
            if role in entities:
                continue
            decoders[role] = {"entity_id": found["entity_id"], "spec": found["spec"]}
            readings[role] = {
                "entity_id": found["entity_id"],
                "name": found.get("source_name") or found["entity_id"],
                "state": None,
                "unit": found["unit"],
                "value": found["value"],
                "decoded": True,
            }

        role_order = ALL_ROLES + ("today_energy", "month_energy")
        devices.append({
            "key": group["key"],
            "name": (name or group["key"]).strip(),
            "model": group["model"],
            "manufacturer": group["manufacturer"],
            "source": group["source"],
            "entities": entities,
            "decoders": decoders,
            "readings": readings,
            "roles": [r for r in role_order if r in entities or r in decoders],
        })

    devices.sort(key=lambda d: (d["name"] or d["key"]).lower())
    return devices


def list_entities_by_role(states: list, role: str) -> list:
    """给前端手动改绑用的实体下拉列表。"""
    result = []
    for item in states or []:
        if classify_entity(item) != role:
            continue
        attrs = item.get("attributes") or {}
        result.append({
            "entity_id": item.get("entity_id"),
            "name": _name_of(item),
            "state": item.get("state"),
            "unit": str(attrs.get("unit_of_measurement") or ""),
        })
    result.sort(key=lambda x: (x["name"] or "").lower())
    return result
