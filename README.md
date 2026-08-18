# HAPowerStats - 家庭用电监控系统

对接 Home Assistant 的家庭用电统计程序：**只填 HA 地址和令牌，插座设备直接勾选导入，支持多个插座**。
功率、电量、电压、电流等参数会从 HA 实体自动绑定，无需手动填实体 ID。

## 功能特性

- **只配连接**：HA 只需要地址 + 长期访问令牌
- **一键导入插座**：自动扫描 HA 里带功率 / 电量计量的设备，勾选即导入
- **参数自动识别**：功率(W/kW)、电量(kWh)、电压、电流、功率因数、频率、开关实体自动绑定并统一单位
- **多插座支持**：每个插座独立统计，另有全屋合计；实时曲线一线一插座
- 今日 / 本月用电自动统计，每日 0 点、每月 1 号自动重置
- 有电量实体的直接用 HA 累计值（自动处理实体归零），没有的按功率积分推算
- 插座开关控制（直接调用 HA 服务，不依赖 MQTT）
- 历史查询、按插座筛选、CSV 导出
- 可选：MQTT 自动发现把统计结果回写成 HA 实体；MySQL 长期存储
- 累计电能持久化，程序重启不清零

## 快速开始

### 方式一：Docker Compose 部署（推荐）

```bash
git clone https://github.com/tyrantcwj/HAPowerStats.git
cd HAPowerStats
docker compose up -d --build
```

也可以直接使用已发布镜像：

```yaml
services:
  electricity-monitor:
    image: ghcr.io/tyrantcwj/hapowerstats:latest
    container_name: electricity-monitor
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./data/electricity_data:/app/electricity_data
      - ./data/config:/app/config
    environment:
      - TZ=Asia/Shanghai
```

访问：`http://你的服务器IP:5000`（无需登录，建议只在内网使用）

### 方式二：本地运行

```bash
pip install -r requirements.txt
python home_electricity_monitor.py
```

## 配置流程

打开 Web 页面 → **⚙️ 系统设置**，三步即可：

1. **填 HA 连接**：HA 地址（如 `http://192.168.1.100:8123`）+ 长期访问令牌
   （HA 左下角点击用户名 → 长期访问令牌 → 创建令牌），点「保存」
2. **扫描插座**：点「🔍 扫描设备」，程序会列出所有带功率或电量计量的设备，
   每个设备下面直接显示识别到的参数和当前读数
3. **勾选导入**：勾选要监控的插座（可多选），点「➕ 导入选中」

导入后采集程序会在下一个采集周期（默认 10 秒）自动生效，**不需要重启**。

### 自动识别的参数

| 角色 | 识别依据 | 统一单位 |
|------|----------|----------|
| 功率 | `device_class=power` 或单位 W/kW | W |
| 电量 | `device_class=energy` 或单位 Wh/kWh/MWh | kWh |
| 电压 | `device_class=voltage` | V |
| 电流 | `device_class=current` | A |
| 功率因数 / 频率 | 对应 `device_class` | - |
| 开关 | 同设备下的 `switch.*` 实体 | - |

说明：

- 归组优先用 HA 的**设备**信息（通过模板接口读取 `device_id`），拿不到时按实体名前缀归组
- 同一设备有多个电量实体时，优先取累计总量（`total_increasing`），自动跳过「今日 / 本月用电」这类分段统计
- 既没有功率也没有电量的设备不会出现在列表里（温度传感器等不会被误识别）

### 已添加插座的管理

「🔌 已添加的插座」面板里可以改名、停用、移除，也能选择电量统计方式：

| 方式 | 说明 |
|------|------|
| 自动（默认） | 有电量实体就用电量实体，没有就按功率积分 |
| 用电量实体 | 强制读 HA 累计电量实体 |
| 功率积分 | 忽略电量实体，用功率对时间积分 |

### 可选配置

| 配置项 | 说明 |
|--------|------|
| MQTT 服务器 | 配置后把「合计 + 每个插座」的功率/总电能/今日/本月回写成 HA 实体 |
| MySQL | 长期存储，表结构带设备维度；不配则只写本地 CSV |
| `WEB_PORT` / `WEB_HOST` | 环境变量，改 Web 服务监听地址和端口（默认 `0.0.0.0:5000`） |

## 从旧版本升级

旧版本只能配一个 `power_entity_id`，升级后会自动迁移：

- 原来的功率实体变成一个名为「默认功率实体」的插座（key 为 `legacy_main`）
- 历史累计电量、今日 / 本月起始值原样继承，不会清零
- 旧的 `electricity_YYYY-MM.csv` 仍可查询，归到该设备名下；新数据写入 `records_YYYY-MM.csv`（带设备列）
- MySQL 老表会自动补上 `device_key` / `device_name` / `voltage_v` / `current_a` 列

## 文件结构

```
├── home_electricity_monitor.py   # 主程序：多设备采集、电量统计、MQTT 上报
├── ha_client.py                  # HA REST 客户端 + 插座设备自动发现
├── config_store.py               # 配置读写（HA 连接、插座列表、MQTT、MySQL）
├── storage.py                    # 历史存储：CSV / MySQL（按设备维度）
├── web_server.py                 # Web 服务与 API
├── templates/
│   └── index.html                # 监控 / 历史 / 设置页面
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .github/workflows/            # 自动构建镜像、同步 Gitee
```

## 数据存储

| 位置 | 内容 |
|------|------|
| `config/ha_config.json` | HA 地址与令牌 |
| `config/devices.json` | 已导入的插座及其实体映射 |
| `electricity_data/electricity_state.json` | 每个插座的累计电量等运行状态 |
| `electricity_data/records_YYYY-MM.csv` | 明细记录（时间、设备、功率、累计电量、电压、电流） |

## 主要 API

| 接口 | 说明 |
|------|------|
| `GET /api/realtime` | 合计 + 每个插座的实时数据 |
| `GET /api/ha/discover` | 扫描 HA 中的可用插座设备 |
| `POST /api/devices/import` | 按 key 批量导入插座 |
| `GET /api/devices` / `POST /api/devices` | 读取 / 保存插座配置 |
| `DELETE /api/devices/<key>` | 移除插座 |
| `GET /api/history?date=&device=` | 历史查询，`device` 为空表示全部合计 |
| `POST /api/switch` | 开关插座（`device_key` + `action=on/off`） |

## License

MIT License
