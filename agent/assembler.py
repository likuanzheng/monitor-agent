"""把引擎产出的物理 / GPU / 容器结果组装成「每设备一条（含嵌套 GPU、容器）」的快照。

containers_by_device / gpus_by_device 均为引擎 collect_entities 产出的 {instance: [实体...]}。
"""


def build_snapshot(devices, physical, gpus_by_device, containers_by_device,
                   timestamp, prometheus_url, physical_fields):
    records = []
    for dev in devices:
        phys = dict(physical.get(dev, {}))
        # 补齐缺失字段为 null，保证输出 schema 稳定
        for field in physical_fields:
            phys.setdefault(field, None)

        records.append({
            "device": dev,
            "timestamp": timestamp,
            "physical": phys,
            "gpus": sorted(gpus_by_device.get(dev, []), key=lambda g: str(g.get("index"))),
            "containers": sorted(containers_by_device.get(dev, []), key=lambda c: str(c.get("name"))),
        })

    return {
        "timestamp": timestamp,
        "prometheus": prometheus_url,
        "device_count": len(records),
        "devices": records,
    }
