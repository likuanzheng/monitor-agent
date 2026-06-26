"""声明式 spec 引擎：把 config 里的 PromQL spec 跑成「按 instance / 容器」聚合好的字典。

新增监控字段只需在 config 里加一条 spec，本文件无需改动。
"""
import logging

log = logging.getLogger("engine")


def _label_pairs(keep_labels):
    """把 ["mountpoint:mount", "fstype"] 解析成 [(源标签, 输出别名), ...]。"""
    pairs = []
    for item in keep_labels or []:
        src, _, alias = item.partition(":")
        pairs.append((src, alias or src))
    return pairs


class Engine:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.rate = config.get("rate_window", "1m")

    def _query(self, promql):
        """替换 $RATE 占位符并查询；单条查询失败不影响其它字段。"""
        promql = promql.replace("$RATE", self.rate)
        try:
            return self.client.query(promql)
        except Exception as exc:  # noqa: BLE001 - 容错：坏 query 不该拖垮整轮
            log.warning("query failed, skipped: %s | %s", promql, exc)
            return []

    # ---- 设备发现 ----
    def discover_devices(self):
        spec = self.config["device_discovery"]
        label = spec.get("label", "instance")
        names = {r["labels"].get(label) for r in self._query(spec["promql"])}
        return sorted(n for n in names if n)

    # ---- 物理机指标 -> {instance: {field: 标量 / {key:val} / [..]}} ----
    def collect_physical(self):
        out = {}
        for spec in self.config.get("physical_metrics", []):
            field = spec["field"]
            kind = spec.get("kind", "scalar")
            rows = self._query(spec["promql"])

            if kind == "scalar":
                for r in rows:
                    inst = r["labels"].get("instance")
                    if inst:
                        out.setdefault(inst, {})[field] = r["value"]

            elif kind == "keyed":
                key_label = spec["key_label"]
                for r in rows:
                    inst = r["labels"].get("instance")
                    key = r["labels"].get(key_label)
                    if inst and key is not None:
                        out.setdefault(inst, {}).setdefault(field, {})[key] = r["value"]

            elif kind == "list":
                pairs = _label_pairs(spec.get("keep_labels"))
                value_field = spec.get("value_field", "value")
                for r in rows:
                    inst = r["labels"].get("instance")
                    if not inst:
                        continue
                    item = {alias: r["labels"].get(src) for src, alias in pairs}
                    item[value_field] = r["value"]
                    out.setdefault(inst, {}).setdefault(field, []).append(item)

            else:
                log.warning("unknown physical metric kind=%s field=%s", kind, field)
        return out

    # ---- 容器 -> {(instance, name): {name, state, id, image, field...}} ----
    def collect_containers(self):
        disc = self.config["container_discovery"]
        id_labels = disc.get("id_labels", [])
        containers = {}

        for r in self._query(disc["promql"]):
            inst = r["labels"].get("instance")
            name = r["labels"].get("name")
            if not inst or not name:
                continue
            info = {"name": name, "state": "running"}
            for lbl in id_labels:
                info[lbl] = r["labels"].get(lbl)
            containers[(inst, name)] = info

        for spec in self.config.get("container_metrics", []):
            field = spec["field"]
            for r in self._query(spec["promql"]):
                key = (r["labels"].get("instance"), r["labels"].get("name"))
                if key in containers:
                    containers[key][field] = r["value"]
        return containers
