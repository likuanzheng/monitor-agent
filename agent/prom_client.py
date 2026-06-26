"""对 Prometheus HTTP API 的最小封装。"""
import logging

import requests

log = logging.getLogger("prom")


class PrometheusClient:
    def __init__(self, base_url, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query(self, promql):
        """执行一次 instant query，返回 [{'labels': {...}, 'value': float}, ...]。

        只处理 vector / scalar 结果；NaN 和无法解析的值被跳过。
        """
        resp = requests.get(
            f"{self.base_url}/api/v1/query",
            params={"query": promql},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"query failed: {payload.get('error')}")

        results = []
        for series in payload["data"]["result"]:
            labels = series.get("metric", {})
            raw = series["value"][1]
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value != value:  # NaN
                continue
            results.append({"labels": labels, "value": value})
        return results
