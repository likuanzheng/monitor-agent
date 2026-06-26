"""可插拔输出口。要换成 REST / 推送，只需新增一个带 write(snapshot) 的类并在 build_outputs 注册。"""
import datetime
import json
import logging
import os
import sys

log = logging.getLogger("output")

DASH = "—"  # 字段缺失时的占位


def _human_bytes(n):
    if n is None:
        return DASH
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _fmt_temp(v):
    return f"{v:.0f}°C" if v is not None else DASH


def _fmt_freq(v):
    return f"{v:.0f}MHz" if v is not None else DASH


def _fmt_cores(cores):
    """8 核 -> 'avg 18% (峰 core7 100%)'。"""
    if not cores:
        return DASH
    vals = list(cores.values())
    avg = sum(vals) / len(vals)
    peak_core = max(cores, key=cores.get)
    return f"{len(cores)}核 avg {avg:.0f}% (峰 core{peak_core} {cores[peak_core]:.0f}%)"


def _fmt_disks(disks):
    if not disks:
        return DASH
    return "  ".join(f"{d.get('mount')} {_human_bytes(d.get('free_bytes'))}空闲" for d in disks)


class StdoutOutput:
    """每轮把快照打成带缩进/换行的多行 JSON 到 stdout（docker logs 可见）。"""

    def write(self, snapshot):
        sys.stdout.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
        sys.stdout.flush()


class PrettyOutput:
    """把快照打成人类可读的设备/容器摘要（适合看 docker logs）。"""

    def write(self, snapshot):
        ts = datetime.datetime.fromtimestamp(snapshot["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "═" * 78,
            f" {ts}   设备 {snapshot['device_count']} 台   ←  {snapshot['prometheus']}",
            "═" * 78,
        ]
        for d in snapshot["devices"]:
            p = d["physical"]
            lines.append(f"● {d['device']}")
            lines.append(
                f"    温度 {_fmt_temp(p.get('cpu_package_temp_c'))}"
                f"   频率 {_fmt_freq(p.get('cpu_freq_mhz'))}"
                f"   内存可用 {_human_bytes(p.get('memory_available_bytes'))}"
                f"   CPU {_fmt_cores(p.get('cpu_core_usage_percent'))}"
            )
            lines.append(f"    磁盘 {_fmt_disks(p.get('disks'))}")

            conts = d["containers"]
            lines.append(f"    容器 {len(conts)} 个" + ("：" if conts else "：（无）"))
            for c in conts:
                cpu = c.get("cpu_percent")
                cpu_s = f"{cpu:5.1f}%" if cpu is not None else f"{DASH:>6}"
                lines.append(
                    f"      ✓ {c['name']:<22.22} cpu {cpu_s}"
                    f"   mem {_human_bytes(c.get('memory_bytes')):>9}"
                    f"   {c.get('image', '')}"
                )
            lines.append("")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()


class FileOutput:
    """覆盖式写入最新快照（先写临时文件再原子替换，避免读到半截）。"""

    def __init__(self, path):
        self.path = path

    def write(self, snapshot):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


def build_outputs(specs):
    outputs = []
    for spec in specs or [{"type": "stdout"}]:
        kind = spec.get("type")
        if kind == "pretty":
            outputs.append(PrettyOutput())
        elif kind == "stdout":
            outputs.append(StdoutOutput())
        elif kind == "file":
            outputs.append(FileOutput(spec.get("path", "/data/devices.json")))
        else:
            log.warning("unknown output type=%s, skipped", kind)
    return outputs
