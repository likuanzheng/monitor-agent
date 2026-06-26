# Prometheus 监控聚合 Agent — 设计方案

## Context（为什么做这个）

现有 Prometheus（192.168.32.9:9090）已经接入了各设备的 node_exporter（物理机指标）和
cadvisor（容器指标），但这些是「按指标分散」的原始时间序列。后续的「设备管理平台」需要的是
**以设备为中心的结构化视图**：每个物理设备一条记录，里面既有这台机器的物理监控信息，也有它上
面跑的若干容器信息。

本 agent 就是这层「聚合/重组」：它**不在设备上采集**，而是定时调用 Prometheus 的 HTTP API
（PromQL），把 node_exporter + cadvisor 已采集的数据查出来，组装成「每设备一条（含嵌套容器）」
的 JSON，写到文件 + stdout。以 docker 容器形态长期跑在上位机上。

设计目标：**可伸缩** —— 加设备只改 targets json（Prometheus 自动纳管，agent 自动发现）；
加一个监控字段只在 config 里加一条 PromQL spec，不改代码。

## 部署形态

- 目录：`~/prometheus/agent/`
- docker 容器，加入现有 `~/prometheus/docker-compose.yml` 的同一网络，
  通过 `http://prometheus:9090` 访问 Prometheus（容器名解析，无需 IP）。
- 常驻 daemon：每 `INTERVAL` 秒（默认 15s）查一轮、组装、输出。

## 输出结构（每设备一条，容器嵌套）

每轮把所有设备汇总成一个快照：
- 写到挂载卷文件 `/data/devices.json`（覆盖式，最新快照；平台/人随时读）
- 同时打印到 stdout（`docker logs` 可看）

单条设备记录示意：
```json
{
  "device": "A100-train-01",
  "timestamp": 1719400000,
  "physical": {
    "cpu_package_temp_c": 55.0,
    "cpu_freq_mhz": 2400.0,
    "cpu_core_usage_percent": {"0": 12.3, "1": 8.0, "2": 5.1},
    "memory_available_bytes": 134217728000,
    "disks": [{"mount": "/", "fstype": "ext4", "free_bytes": 502000000000}]
  },
  "containers": [
    {"name": "trainer", "id": "a1b2c3", "image": "...", "state": "running",
     "cpu_percent": 180.5, "memory_bytes": 8500000000}
  ]
}
```
顶层快照：`{"timestamp":..., "prometheus":"http://prometheus:9090", "devices":[ ...上面的记录... ]}`

## 架构（可伸缩的核心：声明式 spec + 通用引擎）

```
~/prometheus/agent/
├── Dockerfile
├── requirements.txt          # requests, pyyaml
├── config.yaml               # Prometheus地址、间隔、输出、全部 PromQL spec
├── agent.py                  # 主循环：发现设备 → 跑 spec → 组装 → 输出 → sleep
├── prom_client.py            # PrometheusClient.query(promql) → [{labels, value}]
├── engine.py                 # 按 spec 跑查询，把结果路由进设备/容器记录
├── assembler.py              # 组装成「每设备一条 + 嵌套容器」
└── outputs.py                # 可插拔输出：stdout / 文件（JSON 快照）
```

### 设备发现（自动、可伸缩）
查 `up{job="node"}`，取 `instance` 标签集合 = 当前所有物理设备名
（注：prometheus.yml 里已用 relabel 把 node/cadvisor 的 instance 改成了可读 device 名，
node 与 cadvisor 用同一个 instance 值，因此可按 instance 关联同一台机器的物理与容器指标）。
加/删设备只改 `targets/*.json`，agent 下一轮自动跟随，无需改动。

### 声明式 spec（加字段不改代码）
`config.yaml` 里每个监控字段 = 一条 spec：
```yaml
physical_metrics:
  - field: cpu_package_temp_c
    promql: 'node_hwmon_temp_celsius{chip=~".*coretemp.*",sensor="temp1"}'
    kind: scalar              # 每设备一个标量，按 instance 落位
  - field: cpu_freq_mhz
    promql: 'avg by (instance) (node_cpu_scaling_frequency_hertz) / 1e6'
    kind: scalar
  - field: memory_available_bytes
    promql: 'node_memory_MemAvailable_bytes'
    kind: scalar
  - field: cpu_core_usage_percent
    promql: '100 - (avg by (instance,cpu)(rate(node_cpu_seconds_total{mode="idle"}[1m]))*100)'
    kind: keyed               # 按 cpu 标签展开成 {core: value}
    key_label: cpu
  - field: disks
    promql: 'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}'
    kind: list                # 多行 → 列表，保留 mountpoint/fstype 标签
    keep_labels: [mountpoint, fstype]

container_metrics:
  - field: memory_bytes
    promql: 'container_memory_usage_bytes{name!=""}'
    kind: scalar              # 按 (instance,name) 落到对应容器
  - field: cpu_percent
    promql: 'sum by (instance,name)(rate(container_cpu_usage_seconds_total{name!=""}[1m]))*100'
    kind: scalar
container_discovery:
  promql: 'container_last_seen{name!=""}'   # 枚举每设备上的容器(name,id,image)
```
`engine.py` 用统一逻辑处理 `kind`（scalar / keyed / list）并按 `instance`（及容器的 `name`）
把值路由进记录。**新增字段 = 加一条 spec；新增设备 = 改 targets json。两者都不动 Python 代码。**

> PromQL 注意：CPU package 温度的传感器命名各机器差异大（Intel coretemp 的 temp1 通常是 Package；
> AMD/ARM 不同）。因此温度 query 放在 config 里可改，取不到时该字段为 null，不影响其它字段。

## 主要文件与职责
- `prom_client.py`：封装 `GET /api/v1/query`，解析 `data.result` 向量为 `[{labels, value}]`，带超时/重试。
- `engine.py`：输入 spec 列表 → 逐条 query → 按 kind 聚合成 `{instance: {...}}` 与 `{(instance,name): {...}}`。
- `assembler.py`：合并物理结果与容器结果 → 每设备一条嵌套记录。
- `outputs.py`：`StdoutOutput`、`FileOutput`（写 `/data/devices.json`）。可插拔，后续要换 REST/推送只加一个 output 类。
- `agent.py`：读 config → 循环（发现设备 → engine.run → assembler → 各 output.write → sleep INTERVAL），`--once` 支持跑一次退出便于调试。
- `Dockerfile`：python:3.12-slim + pip install requirements + `CMD ["python","agent.py"]`。
- compose 新增服务（接到现有 `~/prometheus/docker-compose.yml`）：
  ```yaml
    monitor-agent:
      build: ./agent
      container_name: monitor-agent
      restart: unless-stopped
      environment:
        - PROM_URL=http://prometheus:9090
        - INTERVAL=15
      volumes:
        - ./agent/config.yaml:/app/config.yaml:ro
        - agent_data:/data
      depends_on: [prometheus]
  ```
  （volumes 末尾追加 `agent_data:`）

## 验证（端到端）
1. `cd ~/prometheus && docker compose up -d --build monitor-agent`
2. `docker logs -f monitor-agent` —— 看每轮打印的设备快照 JSON。
3. `docker exec monitor-agent cat /data/devices.json | python3 -m json.tool` —— 确认每设备一条、
   physical 字段齐全、containers 数组与该机实际容器对应。
4. 调试单轮：`docker run --rm --network prometheus_default -v $PWD/agent/config.yaml:/app/config.yaml monitor-agent python agent.py --once`
5. 对照核验：抽一台设备，手动 `curl 'http://192.168.32.9:9090/api/v1/query?query=node_memory_MemAvailable_bytes'`，
   值应与快照中该设备 `memory_available_bytes` 一致。

## 待定/默认项（实现时按默认，均可在 config 改）
- 轮询间隔默认 15s；每核使用率用 1m 速率窗口。
- 容器状态：cadvisor 只暴露在跑的容器，故 `state` 恒为 running；附带 cpu%/mem 作为状态信息。
- 磁盘：报所有真实文件系统（排除 tmpfs/overlay 等），不强行只筛 SSD（node_exporter 无可靠 SSD 标识）。
```
