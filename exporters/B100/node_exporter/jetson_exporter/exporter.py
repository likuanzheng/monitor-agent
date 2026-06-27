#!/usr/bin/env python3
"""
Jetson GPU exporter.

Mirrors the ix-exporter (天数/Iluvatar) metric model so one Grafana dashboard /
alert set works across platforms.  Same label scheme (gpu / name / uuid), same
metric suffixes (utilization, mem_*, sm_clock, temperature, ...), same units
(MiB / MHz / % / C / W).  Only the prefix differs: ix-exporter uses `ix_`, this
uses a neutral `gpu_` (dashboards can match both with `{ix,gpu}_utilization`).

Endpoints (default port 32021, aligned with ix-exporter):
  /metrics   -> Prometheus text, labelled by gpu / name / uuid
  /gpu.json  -> {"gpus":[{index,name,uuid,gpu_util_percent,mem_used_mb,
                          mem_total_mb,mem_util_percent,temperature_c,power_w,
                          sm_clock_mhz,mem_clock_mhz}, ...]}

Jetson has a single integrated GPU sharing system RAM (unified memory), so mem_*
comes from the system RAM counters and gpu index is always 0.  Discrete-only
metrics (ECC / PCIe / XID) have no Jetson source and are reported as 0 so panels
and alerts referencing them don't go "no data" on this platform.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jtop import jtop
from prometheus_client import CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST

PORT = 32021              # aligned with ix-exporter
POLL_INTERVAL = 2          # seconds
PREFIX = 'gpu_'            # ix-exporter uses 'ix_'; flip here to match exactly

# ---- Prometheus metrics: same suffixes/labels/units as ix-exporter ----------
REGISTRY = CollectorRegistry()
LABELS = ['gpu', 'name', 'uuid']


def _g(suffix, help_text):
    return Gauge(PREFIX + suffix, help_text, LABELS, registry=REGISTRY)


# Dynamic metrics sourced from jtop.
G_UTIL      = _g('utilization',      'The utilization of GPU (%).')
G_SM_UTIL   = _g('sm_utilization',   'The utilization of SM (%).')
G_MEM_CLOCK = _g('mem_clock',        'Mem clock of GPU (MHz).')
G_MEM_FREE  = _g('mem_free',         'The free physical memory of GPU (MiB).')
G_MEM_TOTAL = _g('mem_total',        'The total physical memory of GPU (MiB).')
G_MEM_USED  = _g('mem_used',         'The used physical memory of GPU (MiB).')
G_MEM_UTIL  = _g('mem_utilization',  'The memory utilization of GPU (%).')
G_POWER     = _g('power_usage',      'The power usage of GPU (W).')
G_SM_CLOCK  = _g('sm_clock',         'Sm clock of GPU (MHz).')
G_TEMP      = _g('temperature',      'The temperature of the GPU (C).')

# Discrete-GPU-only metrics with no Jetson source -> reported as 0 for parity.
G_ECC_DBE   = _g('ecc_dbe_vol_status',  'Double-bit volatile ECC error status (0=ok).')
G_ECC_SBE   = _g('ecc_sbe_vol_status',  'Single-bit volatile ECC error status (0=ok).')
G_XID       = _g('xid_errors',          'The value of the last XID error encountered.')
G_PCIE_REPL = _g('pcie_replay_counter', 'The PCIe replay counter.')
G_PCIE_RX   = _g('pcie_rx_throughput',  'PCIe rx throughput (KB/s).')
G_PCIE_TX   = _g('pcie_tx_throughput',  'PCIe tx throughput (KB/s).')

ZERO_METRICS = [G_ECC_DBE, G_ECC_SBE, G_XID, G_PCIE_REPL, G_PCIE_RX, G_PCIE_TX]

# Latest snapshot shared between the jtop poll thread and the HTTP handlers.
_latest = {'gpus': []}
_lock = threading.Lock()


def safe(fn, default=None):
    try:
        v = fn()
        return v if v is not None else default
    except Exception:
        return default


def _round(v, n=1):
    return round(float(v), n) if v is not None else None


def as_map(x):
    """jtop 的 .gpu/.memory 是 Mapping-like 自定义对象、并非 dict 实例，
    用 isinstance(x, dict) 判会漏掉它们。统一转成真 dict 再取值。"""
    if isinstance(x, dict):
        return x
    try:
        return dict(x)
    except Exception:
        return {}


def read_snapshot(jetson):
    """Map a jtop snapshot to one unified GPU record (Jetson = single iGPU)."""
    board = safe(lambda: jetson.board, {}) or {}
    hw = board.get('hardware', {}) if isinstance(board, dict) else {}
    name = hw.get('Model') or hw.get('Module') or 'NVIDIA Jetson'
    serial = hw.get('Serial Number') or hw.get('SoC') or 'jetson-0'
    uuid = 'GPU-%s' % serial

    # utilization + SM clock (status.load 0-100; freq.cur kHz -> MHz)
    gpu_util = sm_clock = None
    g = as_map(safe(lambda: jetson.gpu))
    if g:
        first = as_map(next(iter(g.values())))
        gpu_util = safe(lambda: first['status']['load'])
        cur = safe(lambda: first['freq']['cur'])
        if cur is not None:
            sm_clock = float(cur) / 1000.0

    # memory (unified system RAM, KB -> MiB)
    mem_used = mem_total = mem_free = mem_util = None
    m = as_map(safe(lambda: jetson.memory))
    ram = as_map(m.get('RAM'))
    if ram:
        tot = ram.get('tot')
        used = ram.get('used')
        if tot:
            mem_total = float(tot) / 1024.0
        if used is not None:
            mem_used = float(used) / 1024.0
        if mem_total is not None and mem_used is not None:
            mem_free = mem_total - mem_used
            mem_util = mem_used / mem_total * 100.0 if mem_total else None

    # temperature (GPU sensor; 离线传感器 online=False/值=-256，跳过当无值)
    temp = None
    for k, v in as_map(safe(lambda: jetson.temperature)).items():
        if 'gpu' in k.lower():
            v = as_map(v)
            if v.get('online', True):
                temp = v.get('temp')
            break

    # power (prefer a GPU rail, else total; mW -> W)
    power = None
    p = as_map(safe(lambda: jetson.power))
    for k, v in as_map(p.get('rail')).items():
        if 'gpu' in k.lower():
            v = as_map(v)
            if v.get('power') is not None:
                power = float(v['power']) / 1000.0
            break
    if power is None:
        tot = as_map(p.get('tot'))
        if tot.get('power') is not None:
            power = float(tot['power']) / 1000.0

    # memory controller clock (EMC.cur kHz -> MHz; 注意 EMC 在 .memory 里，无 .emc 属性)
    mem_clock = None
    emc = as_map(m.get('EMC'))
    if emc.get('cur') is not None:
        mem_clock = float(emc['cur']) / 1000.0

    return {
        'index': '0', 'name': name, 'uuid': uuid,
        'gpu_util_percent': _round(gpu_util),
        'mem_used_mb': _round(mem_used),
        'mem_total_mb': _round(mem_total),
        'mem_free_mb': _round(mem_free),
        'mem_util_percent': _round(mem_util),
        'temperature_c': _round(temp),
        'power_w': _round(power),
        'sm_clock_mhz': _round(sm_clock),
        'mem_clock_mhz': _round(mem_clock),
    }


def update_metrics(rec):
    labels = (rec['index'], rec['name'], rec['uuid'])

    def s(gauge, val):
        if val is not None:
            gauge.labels(*labels).set(val)

    s(G_UTIL,      rec['gpu_util_percent'])
    s(G_SM_UTIL,   rec['gpu_util_percent'])   # Jetson iGPU: SM load == GPU load
    s(G_MEM_CLOCK, rec['mem_clock_mhz'])
    s(G_MEM_FREE,  rec['mem_free_mb'])
    s(G_MEM_TOTAL, rec['mem_total_mb'])
    s(G_MEM_USED,  rec['mem_used_mb'])
    s(G_MEM_UTIL,  rec['mem_util_percent'])
    s(G_POWER,     rec['power_w'])
    s(G_SM_CLOCK,  rec['sm_clock_mhz'])
    s(G_TEMP,      rec['temperature_c'])
    for gauge in ZERO_METRICS:
        gauge.labels(*labels).set(0)


def poll_loop():
    """Background thread: keep a jtop session open and refresh the snapshot."""
    while True:
        try:
            with jtop() as jetson:
                while jetson.ok():
                    rec = read_snapshot(jetson)
                    update_metrics(rec)
                    # JSON view keeps the original 11-field schema.
                    pub = {k: rec[k] for k in (
                        'index', 'name', 'uuid', 'gpu_util_percent',
                        'mem_used_mb', 'mem_total_mb', 'mem_util_percent',
                        'temperature_c', 'power_w', 'sm_clock_mhz', 'mem_clock_mhz')}
                    with _lock:
                        _latest['gpus'] = [pub]
                    time.sleep(POLL_INTERVAL)
        except Exception as e:
            print('jtop session error: %s (retrying)' % e, flush=True)
            time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith('/metrics'):
            self._send(200, CONTENT_TYPE_LATEST, generate_latest(REGISTRY))
        elif self.path == '/' or self.path.startswith('/gpu'):
            with _lock:
                body = json.dumps(_latest, ensure_ascii=False).encode('utf-8')
            self._send(200, 'application/json; charset=utf-8', body)
        else:
            self._send(404, 'text/plain; charset=utf-8', b'not found')

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    print('jetson_exporter on :%d  (/metrics, /gpu.json)' % PORT, flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
