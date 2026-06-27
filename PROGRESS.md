# PROGRESS — 聚合 Agent 进度

> 「中断后重新同步」按 CLAUDE.md → 本文件 → `plans/` 的顺序读。
> 本文件记录**已做完 / 进行中 / 待办**，以及每次验证的真实结论。

## 最近一次同步：2026-06-27（第二台设备 + 跨平台 GPU 统一）

### 本轮做完的

1. **纳管第二台设备 B100-01（192.168.32.210）**——node/cadvisor/gpu 三 target 均已上线 `up`。
   - 注意：「B100」是**自研开发板的内部型号名**，实际硬件是 **NVIDIA Jetson AGX Orin**。
   - `targets/{node,cadvisor,gpu}.json` 各加一条 B100-01。

2. **GPU 跨平台统一：agent 同时支持 `ix_*`（天数）和 `gpu_*`（Jetson）两套指标——零代码改动**
   - 背景：A100-infer-01 是天数离散卡（ix-exporter，`ix_*`）；B100-01 是 Jetson，
     用**自研 `jetson_exporter`**（见 `exporters/B100/`）。后者刻意镜像 ix 的指标模型：
     同 label（`gpu/name/uuid`）、同后缀、同单位，**只差前缀 `ix_` → `gpu_`**。
   - 改法：`config.yaml` 每个 GPU query 改成 `ix_X or gpu_X` 并集（同机不会同时有两种前缀，
     PromQL `or` 直接合流，engine 现有「按 instance+gpu 归组」原样吃下）。**engine 没动**。
   - 哨兵→null 扩展：温度 `null_if: [255, -256]`（255=ix 驱动不可用；-256=jetson 老版无值）。
   - 扩展性：以后加第三类 GPU exporter，只要它也按这套后缀/label 出指标，并集里再 `or 前缀_X` 即可。

3. **把设备侧 exporter 部署文件纳入版本库 `exporters/`**
   - `exporters/A100/` —— 天数机上的 node_exporter+cadvisor+ix-exporter compose + readme。
   - `exporters/B100/node_exporter/` —— Jetson 那套：compose + README + 自研 `jetson_exporter/`
     （`exporter.py` 用 jtop 读 Orin GPU，按 `gpu_*` 暴露；Dockerfile = python3.10-slim + jetson-stats）。
   - `.gitignore` 已忽略 `__pycache__/` 和 `*.pyc`（不提交 jetson_exporter 的字节码）。

### 验证结论（真实 Prometheus 192.168.32.9，本轮 agent --once）

- `device_count: 2`。**A100-infer-01 GPU 经并集正常聚合**（Iluvatar MR-V50：util 0%、显存 68/16384MB、27°C、20W）。
- 并集查询逐条核过：`ix_temperature or gpu_temperature` 等都能同时返回两台设备的 series。
- **B100-01 当前 GPU 0 块**——因为它线上跑的还是**旧版 jetson_exporter**（只出 8 个离散指标、温度 -256、
  无 `gpu_utilization`/`gpu_mem_total`）。发现靠 `gpu_mem_total`，旧版没有 → 暂不计入。
  **换上仓库里这版 `exporter.py` 后会自动出现**（新版从 jtop 取 util/统一内存/频率/温度/功率）。

## 待办 / 下一步（按顺序）

- [ ] **① 在 B100-01(Orin) 上重部署修好的 jetson_exporter**（需用户在 Jetson 宿主 sudo）。
      已修 `exporter.py` 一个真 bug：jtop 的 `.gpu`/`.memory` 是 Mapping-like 对象、**非 dict 实例**，
      原代码用 `isinstance(x, dict)` 当闸门 → util/显存/频率整段被跳过（只有 temperature/power 是真
      dict 才出值）。改用 `as_map()` 鸭子类型转 dict；并修 EMC 频率取自 `.memory['EMC']['cur']`
      （`.emc` 属性不存在）；温度尊重 `online`（GPU 传感器离线值 -256 跳过当 null）。
      ⚠️ Jetson 构建坑：必须用经典构建器，别用 `compose build`（详见同目录 README.md）：
      ```
      # 先把上位机仓库里这版 exporter.py 同步到 Jetson，再：
      cd ~/node_exporter/jetson_exporter
      sudo DOCKER_BUILDKIT=0 docker build -t jetson_exporter:local .
      cd ~/node_exporter && sudo docker compose up -d --no-build jetson_exporter
      curl -s localhost:32021/gpu.json   # util/显存/频率应有真实值
      ```
      踩坑提醒：重建时若旧容器没替换干净会出现多个 jetson_exporter / 野容器抢占 32021，
      先 `sudo docker ps -a --filter name=jetson_exporter -q | xargs -r sudo docker rm -f` 清掉再起。
- [ ] **② 重部署 agent + 重建 prometheus**（本轮改了 prometheus 无关，但之前那次改了 prometheus.yml）：
      ```
      cd ~/prometheus && sudo docker compose up -d --build monitor-agent
      ```
      （targets/*.json 改动 file_sd 30s 自动生效，无需重建 prometheus。）
- [x] **③ B100-01 容器为 0 —— 已定位根因（非 agent 问题）**：
      不是正则问题。容器 scope 本就是标准 `/system.slice/docker-<id>.scope`（cgroup v2 + systemd 驱动）。
      根因是 B100 用的 **`zcube/cadvisor:latest` 镜像太旧、不认 Docker 29 的 overlay2 存储布局**：
      cadvisor 去找 `image/overlayfs/`（实为 `overlay2/`）→「failed to identify the read-write layer ID」
      → 容器 handler 创建失败 → `name`/`image` 全空 → agent 的 `name!=""` 过滤掉 → 0 条。
      **修法**：已把 `exporters/B100/.../docker-compose.yaml` 的 cadvisor 换成官方多架构
      `gcr.io/cadvisor/cadvisor:latest`（与 A100 一致）。待在 B100 上 `sudo docker compose up -d cadvisor`
      重拉后，复查 `container_last_seen{instance="B100-01",name!=""}` 应有值。
- [ ] `cpu_freq_mhz` 永远 null（设备侧未出频率指标）——要么补采，要么从 config 去掉该字段。
- [ ] 多卡场景仍未实测（两台都是单卡）；`key_label=gpu` 已按多卡设计。

## 历史结论（来自 A100-train-01，已从 targets 换出，仍有参考价值）

- ARM 架构，温度传感器是 `soc:scpi_soc:scpi:sensors` 而非 x86 `coretemp`（config 已兼容两者）。
- 该机 node_exporter 缺磁盘+频率指标（未挂 `--path.rootfs=/host`）——设备侧采集缺失，非 Agent bug。
  对比：A100-infer-01 的 exporter compose 挂了 `--path.rootfs=/host`，所以磁盘正常。
- 同机跑 k8s+docker，`container_discovery` 用 id 正则只留 docker，滤掉 kubepods。
