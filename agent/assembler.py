"""把引擎产出的物理 / 容器结果组装成「每设备一条（含嵌套容器）」的快照。"""


def build_snapshot(devices, physical, containers, timestamp, prometheus_url, physical_fields):
    # 容器按 instance 归组
    by_device = {}
    for (inst, _name), info in containers.items():
        by_device.setdefault(inst, []).append(info)

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
            "containers": sorted(by_device.get(dev, []), key=lambda c: c["name"]),
        })

    return {
        "timestamp": timestamp,
        "prometheus": prometheus_url,
        "device_count": len(records),
        "devices": records,
    }
