"""monitor-agent 主入口。

定时调用 Prometheus PromQL API，把 node_exporter / cadvisor 的原始指标
重组成「每设备一条（含嵌套容器）」的快照，写到配置的各输出口。
"""
import argparse
import logging
import os
import sys
import time

import yaml

from assembler import build_snapshot
from engine import Engine
from outputs import build_outputs
from prom_client import PrometheusClient

log = logging.getLogger("agent")


def load_config(path):
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    # 环境变量覆盖（容器里用 PROM_URL / INTERVAL 注入更方便）
    config["prometheus_url"] = os.environ.get(
        "PROM_URL", config.get("prometheus_url", "http://localhost:9090")
    )
    if os.environ.get("INTERVAL"):
        config["interval_seconds"] = int(os.environ["INTERVAL"])
    return config


def run_once(engine, config, outputs):
    devices = engine.discover_devices()
    physical = engine.collect_physical()
    gpus = (engine.collect_entities(config["gpu_discovery"], config.get("gpu_metrics", []))
            if config.get("gpu_discovery") else {})
    containers = engine.collect_entities(config["container_discovery"], config.get("container_metrics", []))
    physical_fields = [s["field"] for s in config.get("physical_metrics", [])]

    snapshot = build_snapshot(
        devices=devices,
        physical=physical,
        gpus_by_device=gpus,
        containers_by_device=containers,
        timestamp=int(time.time()),
        prometheus_url=config["prometheus_url"],
        physical_fields=physical_fields,
    )
    for out in outputs:
        out.write(snapshot)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description="Prometheus 监控聚合 agent")
    parser.add_argument("--config", default=os.environ.get("CONFIG", "config.yaml"))
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出（调试用）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,  # 日志走 stderr，stdout 留给快照 JSON
    )

    config = load_config(args.config)
    client = PrometheusClient(config["prometheus_url"], timeout=config.get("timeout_seconds", 10))
    engine = Engine(client, config)
    outputs = build_outputs(config.get("outputs"))
    interval = config.get("interval_seconds", 15)

    if args.once:
        run_once(engine, config, outputs)
        return

    log.info("monitor-agent started: prometheus=%s interval=%ss", config["prometheus_url"], interval)
    while True:
        start = time.time()
        try:
            snap = run_once(engine, config, outputs)
            log.info("collected %d devices", snap["device_count"])
        except Exception:  # noqa: BLE001 - 单轮失败不退出，下一轮继续
            log.exception("collection cycle failed")
        time.sleep(max(0.0, interval - (time.time() - start)))


if __name__ == "__main__":
    main()
