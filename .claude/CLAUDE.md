# 项目记忆 — Prometheus 监控栈 + 聚合 Agent

> 这个文件是「中断后重新同步」的入口。新会话先读它，再看 `PROGRESS.md` 和 `plans/`。

## 这个项目是什么

在**上位机 192.168.32.9** 上跑一套 Prometheus 监控栈，外加一个自研的**聚合 Agent**：
Agent 不在设备上采集，而是定时调 Prometheus 的 PromQL API，把 node_exporter（物理机）+
cadvisor（容器）的原始指标，重组成「**每物理设备一条记录（含嵌套 docker 容器）**」的 JSON，
供后续的「设备管理平台」消费。

## ⚠️ 重大坑：改 prometheus.yml 后必须「重建容器」而非 reload

线上 Prometheus 把 `prometheus.yml` 以**单文件 bind mount** 挂进容器。编辑器（含本工具）改文件是
**原子替换=换新 inode**，而容器绑死在旧 inode 上（mountinfo 显示 `prometheus.yml//deleted`），
所以 **`curl -X POST .../-/reload` 返回 200 也加载不到改动**。
- 改 `prometheus.yml` 后必须：`cd ~/prometheus && sudo docker compose up -d --force-recreate prometheus`
- 改 `targets/*.json` 不受影响（targets 是**目录**挂载，新增/改文件容器能看到，reload 或 file_sd 自动生效）。
- 当前用户 zlk **无 docker 权限、sudo 需密码**，docker 操作得用户在交互终端执行。

## 目录与组件

```
~/prometheus/
├── docker-compose.yml      # prometheus + monitor-agent 两个服务
├── prometheus.yml          # job：node / cadvisor / gpu / prometheus；relabel 把 instance 改成可读 device 名
├── targets/
│   ├── node.json           # 各设备 node_exporter 地址(:9100) + device 标签
│   ├── cadvisor.json       # 各设备 cadvisor 地址(:8080) + device 标签
│   └── gpu.json            # 各设备 ix-exporter 地址(:32021) + device 标签
└── agent/                  # 聚合 Agent（Python，容器化）
    ├── agent.py            # 主循环：发现设备→查询→组装→输出→sleep；支持 --once
    ├── prom_client.py      # 封装 GET /api/v1/query
    ├── engine.py           # 声明式 spec 引擎（scalar/keyed/list 三种 kind 通用路由）
    ├── assembler.py        # 组装「每设备一条 + 嵌套容器」
    ├── outputs.py          # 可插拔输出：stdout(多行JSON) / pretty(易读摘要) / file(JSON快照)
    ├── config.yaml         # Prometheus地址、间隔、全部 PromQL spec、输出口
    ├── Dockerfile / requirements.txt
```

## 关键设计约定（为什么这么做）

- **可伸缩 = 两个不改代码的扩展点**：
  - 加/删设备 → 只改 `targets/*.json`（Prometheus 30s 自动纳管，Agent 下一轮自动发现）。
  - 加监控字段 → 只在 `config.yaml` 加一条 PromQL spec（engine 通用处理 scalar/keyed/list）。
- **instance = device 名**：`prometheus.yml` 用 relabel 把 node/cadvisor 两个 job 的 `instance`
  都改成 `device` 标签值，因此同一台机器的物理指标与容器指标能按 instance 关联。
  改 prometheus.yml 后需热重载：`curl -X POST http://192.168.32.9:9090/-/reload`（返回 200 即成功）。
- **Agent 用容器名访问 Prometheus**：compose 里 `PROM_URL=http://prometheus:9090`（同网络）。
  宿主机本地调试时用 `PROM_URL=http://192.168.32.9:9090`。

## 环境实情（重要，避免重复踩坑）

- 历史首台设备 **A100-train-01**（IP 192.168.32.152，现已从 targets 换出）是 **ARM 架构服务器**；下列是它的踩坑结论，换回时仍适用。
- 温度传感器不是 x86 的 `coretemp`，而是 `soc:scpi_soc:scpi:sensors`（temp1≈62°C）。
  config 里温度 query 已兼容两者。
- 该设备的 node_exporter **没有上报频率和磁盘指标**（Prometheus 里查不到 `node_*frequency*` /
  `node_filesystem_*`），所以 Agent 输出里 `cpu_freq_mhz` 和 `disks` 恒为 `null`。
  **这是设备侧采集缺失，不是 Agent 的 bug**；Agent 的 query 已按标准指标名写好，设备一上报即自动填充。
  修法：设备侧 node_exporter 要挂宿主机根目录 + `--path.rootfs=/host` 才有磁盘；频率需 cpufreq sysfs。
- 该设备同时跑 k8s 和 docker；Agent 的 `container_discovery` 已用 id 正则只保留 docker 容器
  （`/system.slice/docker-*` 或 `/docker/*`），过滤掉 k8s 的 kubepods。

## 常用命令

```bash
cd ~/prometheus
sudo docker compose up -d --build monitor-agent      # 部署/重建 agent（docker 需 sudo 或加 docker 组）
sudo docker logs -f monitor-agent                     # 看每轮快照
sudo docker exec monitor-agent cat /data/devices.json # 最新 JSON 快照
# 本地不进容器调试一轮：
cd ~/prometheus/agent && PROM_URL=http://192.168.32.9:9090 python3 agent.py --once
```

## 当前状态（详见 PROGRESS.md，每次同步以那里为准）

当前纳管设备：**A100-infer-01（192.168.32.153）**，node/cadvisor/gpu 三个 target 均 `up`。
Agent 已用真实 Prometheus 验证：温度/内存/每核使用率/磁盘/docker 容器/**GPU（Iluvatar ix-exporter）** 均正常。
`cpu_freq_mhz` 仍 null（设备侧未出频率指标）。
⚠️ 上述改动**尚未部署**——线上容器还没重建；改了 prometheus.yml，重建时 prometheus 要 `--force-recreate`。
