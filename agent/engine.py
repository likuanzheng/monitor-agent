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

    # ---- 通用「实体」采集：容器、GPU 等「每设备下若干个」的东西共用 ----
    # discovery 描述如何枚举实体并取元信息，metric_specs 给每个实体补字段。
    # 返回 {instance: [实体dict, ...]}，按 instance 归组好，供 assembler 直接嵌进设备记录。
    def collect_entities(self, discovery, metric_specs):
        key_label = discovery["key_label"]          # 用哪个标签区分同一设备内的实体(name/gpu)
        key_field = discovery.get("key_field", key_label)  # 输出里这个 key 叫什么
        meta_labels = discovery.get("meta_labels", [])     # 一并抓取的元信息标签
        constants = discovery.get("constants", {})         # 固定附加字段(如 state: running)

        entities = {}
        for r in self._query(discovery["promql"]):
            inst = r["labels"].get("instance")
            key = r["labels"].get(key_label)
            if not inst or key is None:
                continue
            info = {key_field: key, **constants}
            for lbl in meta_labels:
                info[lbl] = r["labels"].get(lbl)
            entities[(inst, key)] = info

        for spec in metric_specs:
            field = spec["field"]
            null_if = set(spec.get("null_if", []))  # 驱动「不可用」哨兵值映射成 null
            for r in self._query(spec["promql"]):
                ek = (r["labels"].get("instance"), r["labels"].get(key_label))
                if ek in entities:
                    value = r["value"]
                    entities[ek][field] = None if value in null_if else value

        by_instance = {}
        for (inst, _key), info in entities.items():
            by_instance.setdefault(inst, []).append(info)
        return by_instance
