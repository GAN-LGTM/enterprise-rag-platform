# 监控与告警

指标已经在应用里埋好了（9 项，见 `backend/app/metrics.py`，端点 `/metrics`
—— 注意**没有** `/api` 前缀，`/api/metrics` 会 404），
但**只埋指标不算可观测**——出事时没有人盯着仪表盘看，必须有告警。本目录补齐这一层。

## 文件

| 文件 | 用途 |
| --- | --- |
| `prometheus.yml` | 抓取配置（应用 + 数据库 + 缓存 + 节点） |
| `alerts.yml` | 告警规则，按可用性 / LLM / 性能 / 安全 / 缓存 / 业务 六组划分 |

## 快速接入

```bash
# compose 创建的网络名形如 <项目名>_rag-internal，先查出来再接入
docker network ls | grep rag-internal

docker run -d --name prometheus --network <上一步查到的网络名> \
  -v $(pwd)/deploy/monitoring:/etc/prometheus \
  -p 9090:9090 prom/prometheus:v2.53.0
```

打开 `http://<host>:9090/alerts` 可看到规则是否已加载生效。

## 告警分级

| 级别 | 含义 | 响应要求 |
| --- | --- | --- |
| critical | 服务不可用、LLM 全挂、错误率 >10% | 立即处理 |
| warning | 性能劣化、持续超时、越权探测 | 当班处理 |
| info | 缓存命中率下跌、无流量 | 关注即可 |

## 接告警通知（Alertmanager）

Prometheus 只负责"判定"，通知要走 Alertmanager：

```yaml
# alertmanager.yml 片段
route:
  receiver: ops
  group_by: [alertname]
  group_wait: 30s
  repeat_interval: 4h
  routes:
    - matchers: [severity="critical"]
      receiver: ops
      repeat_interval: 30m
receivers:
  - name: ops
    webhook_configs:
      - url: http://your-webhook/alert   # 企业微信 / 钉钉 / 飞书机器人
```

## 关于 exporters

`prometheus.yml` 里 postgres / redis / node 三个 job 需要额外部署 exporter
（同样接入上面查到的 compose 网络）：

```bash
# PostgreSQL
docker run -d --name postgres-exporter --network <网络名> \
  -e DATA_SOURCE_NAME="postgresql://rag:<口令>@postgres:5432/ragdb?sslmode=disable" \
  quay.io/prometheuscommunity/postgres-exporter:v0.15.0

# Redis（生产启用了 requirepass，必须带口令连接）
docker run -d --name redis-exporter --network <网络名> \
  oliver006/redis_exporter:v1.55.0 --redis.addr redis://redis:6379 \
  --redis.password <REDIS_PASSWORD>
```

不部署也不影响告警——对应指标不存在时规则不会触发，只是少了容量视角。

## 备份的监控（重要）

备份是独立进程（crontab 里的 `scripts/backup.py`），**不会进 Prometheus**。
必须单独保证有人知道备份失败：

```bash
# 每天 2 点备份，失败时输出非 0 退出码；配合 cron 的 MAILTO 或日志采集告警
0 2 * * * cd /app/backend && /opt/venv/bin/python scripts/backup.py backup >> /data/logs/backup.log 2>&1
30 2 * * * cd /app/backend && /opt/venv/bin/python scripts/backup.py verify >> /data/logs/backup.log 2>&1
```

**务必定期做一次恢复演练**：能备份不等于能恢复，只有真的恢复过一次才算数。
