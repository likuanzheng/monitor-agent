# 进度

更新于 2026-06-26。

## 已完成

- [x] Prometheus 栈：docker-compose + prometheus.yml + targets/{node,cadvisor}.json
- [x] prometheus.yml 用 relabel 把 node/cadvisor 的 instance 改成可读 device 名
- [x] 聚合 Agent 全部代码（agent/ 下 8 个文件）
- [x] Agent 接入 docker-compose.yml（monitor-agent 服务 + agent_data 卷）
- [x] 用真实 Prometheus（192.168.32.9:9090）`--once` 验证通过：
      温度 62°C、内存可用 ~14GB、8 核使用率、4 个 docker 容器(cpu%/mem/镜像) 均正常
- [x] 输出口三选：stdout(多行缩进JSON) / pretty(易读摘要) / file(/data/devices.json)
      当前 config 用 stdout + file

## 待办 / 下一步

- [ ] 正式部署：`sudo docker compose up -d --build monitor-agent`（宿主机 docker 需 sudo 或加入 docker 组）
- [ ] 设备侧补采集（A100-train-01 等所有设备的 node_exporter）：
      - 磁盘：挂宿主机根目录 + `--path.rootfs=/host`，才有 `node_filesystem_avail_bytes`
      - 频率：cpufreq sysfs 可读才有；部分 ARM 由固件调频可能本就没有
- [ ] 可选：容器 `id` 现为完整 cgroup 路径，可截成 12 位短 id 更易读
- [ ] 后续平台对接：决定最终上报口（现为写文件 /data/devices.json；可加 REST/推送 output 类）

## 已知问题（非 bug）

- A100-train-01 是 ARM；`cpu_freq_mhz` 和 `disks` 恒 null，因设备侧 node_exporter 未上报对应指标。
  Agent 端 query 已就绪，设备补采后自动生效。
