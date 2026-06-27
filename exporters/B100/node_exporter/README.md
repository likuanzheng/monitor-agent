# node_exporter 监控栈（含 Jetson GPU exporter）

一套面向 Jetson 设备的 Prometheus 采集编排：复用社区的 `node_exporter` + `cadvisor`，
并自带一个 `jetson_exporter`，把 Jetson 的 GPU 指标按统一模型暴露出来。

## 目录结构

```
node_exporter/
├── docker-compose.yaml      # 三服务编排
├── README.md                # 本文档
└── jetson_exporter/
    ├── exporter.py          # 自研 Jetson GPU exporter
    └── Dockerfile           # python:3.10-slim + jetson-stats
```

## 三个服务

| 服务 | 镜像 | 端口 | 作用 |
|------|------|------|------|
| `node_exporter` | `prom/node-exporter:latest` | 9100 | 主机 CPU/内存/磁盘/温度等系统指标 |
| `cadvisor` | `zcube/cadvisor:latest` | 8080 | 容器级资源指标（用 zcube 版以兼容 Jetson/ARM） |
| `jetson_exporter` | `jetson_exporter:local`（本地构建） | 32021 | Jetson GPU 指标 |

`node_exporter` 用 `--collector.disable-defaults` 关掉默认采集器，再按需逐个开启
（cpu / loadavg / hwmon / meminfo / filesystem / diskstats），减少无用指标。

---

## 设计思路：jetson_exporter

### 目标：跨平台指标对齐

集群里既有天数/Iluvatar 离散卡（由 `ix-exporter` 暴露 `ix_*` 指标），也有 Jetson。
为了 **一套 Grafana 面板 / 告警规则能同时覆盖两类设备**，`jetson_exporter` 刻意
镜像 `ix-exporter` 的指标模型：

- **相同 label**：`gpu` / `name` / `uuid`
- **相同后缀**：`utilization` / `mem_used` / `sm_clock` / `temperature` / `power_usage` …
- **相同单位**：MiB / MHz / % / ℃ / W
- **仅前缀不同**：`ix-exporter` 用 `ix_`，本 exporter 用中性的 `gpu_`，
  面板里用 `{ix,gpu}_utilization` 这类正则即可同时命中两边。

### Jetson 的特殊性如何处理

- **统一内存**：Jetson 是集成 GPU，与系统共享 RAM，没有独立显存。
  因此 `mem_*` 直接取系统 RAM 计数器，GPU index 恒为 `0`。
- **离散卡专属指标置零**：ECC / PCIe / XID 这些指标在 Jetson 上没有数据源。
  为了让引用它们的面板和告警 **不出现 "no data"**，统一上报 `0`，保持 schema 一致。
- **数据来源 jtop**：后台线程常驻一个 `jtop` 会话，每 2 秒轮询一次快照，
  写入共享的 `_latest` 内存结构；HTTP 请求只读这份快照，互不阻塞。
  jtop 依赖宿主的 `/run/jtop.sock`，因此容器挂载了该 socket。

### 两个端点

- `GET /metrics` —— Prometheus 文本格式，按 `gpu/name/uuid` 打标签。
- `GET /gpu.json` —— 11 字段 JSON 快照，便于人工查看或脚本消费。

---

## 起停手册

> Jetson 宿主上 `docker` 需要 `sudo`。先确认 docker 在跑：
> `sudo systemctl status docker`，未启动则 `sudo systemctl start docker`。

### 首次启动

```bash
cd ~/node_exporter
sudo docker compose build jetson_exporter   # 构建本地镜像 jetson_exporter:local
sudo docker compose up -d                   # 拉取 node_exporter/cadvisor 并后台启动全部
sudo docker compose ps                       # 查看状态
```

> ⚠️ **Jetson 构建坑（重要）**：部分 Jetson 上 `docker compose build` 走的是
> buildx **bake** 流程，镜像只进 buildkit 缓存、**不会 load 进守护进程镜像库**，
> 表现为 build 显示成功、`up` 却报 `No such image: jetson_exporter:local`。
> 判断方法：build 后 `sudo docker images | grep jetson` 查不到镜像。
> **解决**：改用经典构建器单独构建，再用 `--no-build` 启动：
>
> ```bash
> cd ~/node_exporter/jetson_exporter
> sudo DOCKER_BUILDKIT=0 docker build -t jetson_exporter:local .
> sudo docker images | grep jetson            # 确认 jetson_exporter:local 出现
> cd ~/node_exporter
> sudo docker compose up -d --no-build
> ```
>
> 之后凡是改了 `exporter.py`/`Dockerfile`，都用上面这套经典构建流程重建，
> 不要用 `docker compose build` / `up --build`。

### 验证

```bash
curl -s localhost:9100/metrics | head        # node_exporter
curl -s localhost:8080/metrics | head        # cadvisor
curl -s localhost:32021/metrics | head       # jetson_exporter（Prometheus）
curl -s localhost:32021/gpu.json             # jetson_exporter（JSON）
```

### 修改后重启

- **改了 `exporter.py` 或 `Dockerfile`**（镜像内容变了）—— 必须重建镜像。
  Jetson 上用经典构建器（见上方"构建坑"），不要用 `compose build`：

  ```bash
  sudo DOCKER_BUILDKIT=0 docker build -t jetson_exporter:local ./jetson_exporter
  sudo docker compose up -d --no-build jetson_exporter
  ```

  > 坑点：代码是 `COPY` 进镜像的，只 `restart` 不会生效，必须重建镜像。

- **只改了 `docker-compose.yaml`**（端口/参数/挂载）—— 不用 build：

  ```bash
  sudo docker compose up -d
  ```

- **没改任何东西、只想重启**：

  ```bash
  sudo docker compose restart jetson_exporter   # 单个
  sudo docker compose restart                    # 全部
  ```

### 日志与关停

```bash
sudo docker compose logs -f jetson_exporter   # 跟踪日志
sudo docker compose down                        # 停止并删除容器（镜像保留）
sudo docker compose down --rmi local            # 顺带删掉本地构建的 jetson_exporter:local
```

---

## 注意事项

- compose 里 `jetson_exporter` 同时配了 `image:` + `build:` + `pull_policy: never`：
  本地构建后打 `jetson_exporter:local` 标签，启动时只用本地镜像、绝不远程拉取。
- 启动前确认宿主 `jtop` 服务正常：`systemctl status jtop`，否则 GPU 指标无数据。
- `node_exporter` 与 `cadvisor` 使用主机网络 / 特权挂载，仅适合受信内网环境。
