# HAPowerStats - 家庭用电监控系统

家庭用电统计程序，对接HomeAssistant，支持MQTT协议，提供Web监控面板。

## 功能特性

- 实时功率监测 (W)、累计电能统计 (kWh)
- 今日/本月/昨日用电量自动统计
- 支持HomeAssistant MQTT自动发现
- Web监控面板（实时图表、历史查询、导出CSV）
- 支持MySQL数据库长期存储
- 插座开关控制（MQTT）
- 累计电能持久化，程序重启不清零
- 每日0点/每月1号自动重置统计

## 快速开始

### 方式一：Docker Compose 部署（推荐）

国内环境请从 Gitee 克隆后本地构建，避免访问 GitHub / GHCR：

```bash
git clone https://gitee.com/tyrantcwj/HAPowerStats.git
cd HAPowerStats
docker compose up -d --build
```

GitHub 仓库：https://github.com/tyrantcwj/HAPowerStats

也可以直接使用已发布镜像。国内拉取 GHCR 可用 DaoCloud 加速：

```yaml
services:
  electricity-monitor:
    image: docker.m.daocloud.io/ghcr.io/tyrantcwj/hapowerstats:latest
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

若加速站不可用，把 `image` 改回 `ghcr.io/tyrantcwj/hapowerstats:latest`。

启动：
```bash
docker-compose up -d
```

访问：`http://你的服务器IP:5000`

### 方式二：本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 启动程序
python home_electricity_monitor.py
```

## 配置说明

程序启动后，通过Web页面配置所有参数：

1. 打开 `http://localhost:5000`
2. 点击 **⚙️ 系统设置**
3. 填写HomeAssistant地址和访问令牌
4. （可选）配置MySQL数据库
5. 保存并重启程序

### 配置项说明

| 配置项 | 说明 |
|--------|------|
| HA地址 | HomeAssistant访问地址，如 `http://192.168.1.100:8123` |
| 访问令牌 | HA用户资料页面生成的Long-Lived Access Token |
| MQTT服务器 | 可选，HA的MQTT插件地址 |
| MySQL | 可选，用于长期数据存储 |

## 数据源接入

修改 `home_electricity_monitor.py` 中的 `get_power_reading()` 函数：

```python
def get_power_reading() -> float:
    """
    获取当前功率读数（单位：瓦特 W）
    请在此处接入你的实际数据源
    """
    # 示例：从文件读取
    # with open("power.txt", "r") as f:
    #     return float(f.read().strip())
    
    # 示例：从Modbus设备读取
    # from pymodbus.client import ModbusSerialClient
    # client = ModbusSerialClient(port='/dev/ttyUSB0', baudrate=9600)
    # client.connect()
    # result = client.read_holding_registers(address=0, count=1, slave=1)
    # client.close()
    # return float(result.registers[0]) / 100
    
    return 0.0  # 默认返回0
```

## 文件结构

```
├── home_electricity_monitor.py   # 主程序（MQTT+数据采集）
├── web_server.py                 # Web服务器
├── templates/
│   └── index.html                # 监控页面
├── requirements.txt              # Python依赖
├── Dockerfile                    # Docker构建文件
├── docker-compose.yml            # Docker Compose配置
└── .github/
    └── workflows/
        └── docker-publish.yml    # GitHub Actions自动构建
```

## 运行截图

- 实时功率趋势图
- 今日/本月用电统计
- 历史数据查询与导出
- 插座开关控制

## 开发说明

### 本地开发

```bash
# 克隆仓库
git clone https://gitee.com/tyrantcwj/HAPowerStats.git
cd HAPowerStats

# 安装依赖
pip install -r requirements.txt

# 运行
python home_electricity_monitor.py
```

### Docker构建

```bash
# 构建镜像
docker build -t electricity-monitor .

# 运行容器
docker run -d -p 5000:5000 --name electricity-monitor electricity-monitor
```

## License

MIT License
