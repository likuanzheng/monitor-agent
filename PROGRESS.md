# PROGRESS — 聚合 Agent 进度

> 「中断后重新同步」按 CLAUDE.md → 本文件 → `plans/` 的顺序读。
> 本文件记录**已做完 / 进行中 / 待办**，以及每次验证的真实结论。

## 最近一次同步：2026-06-27

### 本轮做完的（均已用真实 Prometheus 192.168.32.9 验证）

1. **新增 GPU 监控（天数智芯 / Iluvatar ix-exporter，`ix_*` 指标）——端到端打通**
   - `targets/gpu.json`：A100-infer-01 → `192.168.32.153:32021`。
   - `prometheus.yml` 新增 `job: gpu`（file_sd + relabel `device→instance`）。
   - `config.yaml` 新增 `gpu_discovery` / `gpu_metrics`：util、显存(used/total/util%)、温度、功率、SM/显存频率。
   - 哨兵值处理：驱动不可用时 `ix_temperature=255`、`ix_power_usage=65535`，用 `null_if` 映射成 `null`，不显示假值。
   - `outputs.py` pretty 输出加 GPU 段。
   - **验证结果**：A100-infer-01 上 1 块 `Iluvatar MR-V50`（16GB），util 0%、显存 68/16384MB、温度 27°C、功率 20W、SM 1000MHz / 显存 1600MHz。`gpu="0"` 标签 → 输出字段 `index`，映射正确。

2. **引擎泛化：`collect_containers` → `collect_entities(discovery, metric_specs)`**
   - 容器和 GPU 共用同一套「每设备下若干实体」逻辑：`key_label`(区分实体) / `key_field`(输出键名) / `meta_labels` / `constants` / `null_if`。
   - `assembler.build_snapshot` 改为接收 `gpus_by_device` + `containers_by_device`，设备记录里嵌 `gpus[]` 和 `containers[]`。
   - `agent.run_once` 对 GPU 做了 `if config.get("gpu_discovery")` 的可选保护——没配 GPU 的环境不报错。

3. **容器指标加厚**：在 `memory_bytes` 之外补了 `cpu_percent`、`memory_limit_bytes`（0=未限）、`disk_usage_bytes` / `disk_limit_bytes`、`net_rx/tx_bytes_per_sec`；pretty 输出加 `disk` 列。

4. **磁盘 fstype 过滤扩展**：`node_filesystem_avail_bytes` 排除项加 `nsfs|devtmpfs|fuse.*`，滤掉 k8s/calico 等噪声挂载。

5. **监控设备切换**：node/cadvisor/gpu 三个 target 全部从 A100-train-01(192.168.32.152) 改到 **A100-infer-01(192.168.32.153)**。四个 target 现均 `up`。

### 这台新设备(A100-infer-01)的实测特性

- **有磁盘指标了**：`/`(ext4) 正常上报 `free_bytes`，不像 A100-train-01 缺采。（仍会带出 `/run/calico/cgroup` 这类 cgroup2 噪声项，已被 fstype 过滤掉大部分。）
- `cpu_freq_mhz` 仍 `null`——该设备 node_exporter 也没出频率指标（cpufreq sysfs 未挂）。
- GPU 是国产 Iluvatar MR-V50，靠 ix-exporter，**不是 NVIDIA DCGM**；字段名按 `ix_*` 对齐。
- 偶见某核 rate 算成 `-0.02%` 这种微负值（瞬时 counter 抖动），非 bug。

## 待办 / 下一步

- [ ] **部署上线**：以上改动仍是工作区未提交状态，线上 monitor-agent 容器还没重建。
      `cd ~/prometheus && sudo docker compose up -d --build monitor-agent`（需用户在交互终端跑，zlk 无 docker 权限）。
      ⚠️ 因为也改了 `prometheus.yml`，prometheus 容器要 `--force-recreate`（见 CLAUDE.md「重大坑」）。
- [ ] `cpu_freq_mhz` 永远 null：要么设备侧补 cpufreq，要么在 config 里去掉该字段，别让消费方误以为是 bug。
- [ ] 多卡场景未实测（当前只 1 块 GPU）；`key_label=gpu` 已按多卡设计，等有多卡机器再验证同机多卡归组。
- [ ] A100-train-01(192.168.32.152) 是否还要继续纳管？当前已被从 targets 里换掉，若要双机需把它加回 `targets/*.json`。

## 历史结论（来自 A100-train-01，仍有参考价值）

- ARM 架构，温度传感器是 `soc:scpi_soc:scpi:sensors` 而非 x86 `coretemp`（config 已兼容两者）。
- 该机 node_exporter 缺磁盘+频率指标（未挂 `--path.rootfs=/host`）——是设备侧采集缺失，非 Agent bug。
- 同机跑 k8s+docker，`container_discovery` 用 id 正则只留 docker，滤掉 kubepods。
