# HAPowerStats - 家庭用电监控系统

家庭用电统计程序，从 Home Assistant 功率实体采集真实功率，统计今日/本月用电，可选通过 MQTT 自动发现回写 HA，并提供带登录保护的 Web 监控面板。

## 功能特性

- 从 Home Assistant 功率实体读取实时功率（W / kW）
- 按实际采样间隔积分累计电能（kWh），停机过久不虚增
- 今日/本月用电量自动统计，累计值持久化
- 可选 Home Assistant MQTT 自动发现
- Web 监控面板（实时图表、历史查询、导出 CSV）
- Web 登录保护；HA Token / MQTT / MySQL 密码留空保存时不会被清空
- 可选 MySQL 长期存储
- 可选插座开关控制（MQTT）

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
git clone https://github.com/tyrantcwj/HAPowerStats.git
cd HAPowerStats
export WEB_PASSWORD='请改成你的密码'   # Windows PowerShell: $env:WEB_PASSWORD='请改成你的密码'
docker compose up -d --build
```

访问 `http://你的服务器IP:5000`，使用上面的密码登录。若未设置 `WEB_PASSWORD`，首次打开页面时需要自己设置登录密码。

也可以直接使用已发布镜像（需先登录 ghcr）：

```yaml
services:
  electricity-monitor:
    image: ghcr.io/tyrantcwj/hapowerstats:latest
    container_name: hapowerstats
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./data/electricity_data:/app/electricity_data
      - ./data/config:/app/config
    environment:
      - TZ=Asia/Shanghai
      - WEB_PASSWORD=请改成你的密码
```

### 方式二：本地运行

```bash
pip install -r requirements.txt
python home_electricity_monitor.py
```

## 配置说明

1. 打开 `http://localhost:5000` 并登录
2. 进入 **系统设置**
3. 填写 Home Assistant 地址、长期访问令牌
4. 填写或从列表选择 **功率实体 ID**（例如 `sensor.house_power`）
5. 可选：MQTT、MySQL
6. 保存。功率实体立即生效；MQTT / MySQL 需重启程序

| 配置项 | 说明 |
|--------|------|
| HA 地址 | 如 `http://192.168.1.100:8123` |
| 访问令牌 | HA 用户资料页生成的 Long-Lived Access Token |
| 功率实体 | 功率类传感器，单位 W 或 kW |
| MQTT | 可选，用于把统计结果自动发现到 HA |
| MySQL | 可选，长期存储 |
| WEB_PASSWORD | 环境变量，Docker 部署时建议设置 |

配置和状态分别保存在 `config/` 与 `electricity_data/`，Docker 下已通过 volume 持久化。

## 文件结构

```
├── home_electricity_monitor.py   # 主程序（采集+积分+MQTT）
├── web_server.py                 # Web 服务器（登录+配置+面板）
├── config_store.py               # 统一配置路径
├── templates/
│   ├── index.html                # 监控页面
│   └── login.html                # 登录页
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .github/workflows/docker-publish.yml
```

## 开发说明

```bash
git clone https://github.com/tyrantcwj/HAPowerStats.git
cd HAPowerStats
pip install -r requirements.txt
python home_electricity_monitor.py
```

构建镜像：

```bash
docker build -t hapowerstats:latest .
docker run -d -p 5000:5000 --name hapowerstats \
  -e WEB_PASSWORD='请改成你的密码' \
  -v ${PWD}/data/electricity_data:/app/electricity_data \
  -v ${PWD}/data/config:/app/config \
  hapowerstats:latest
```

## License

MIT License
