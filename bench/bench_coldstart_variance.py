"""冷启动到底要多久：基准说 15s，但服务里连续三次实测 29.6 → 37.6 → 49.8s。

单次采样不作数（见 memory: feedback-no-single-sample-conclusions），所以：
  ① 冷启动在**独立进程**里跑 n=3，看方差、看 15s 还能不能复现
  ② 同一进程里量「第一次 encode」vs「稳态 encode」，验证 warmup 该不该排除在统计外
"""

import os
import statistics
import subprocess
import sys
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

CHILD = r"""
import os, time
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
t0 = time.perf_counter()
from sentence_transformers import SentenceTransformer
t_import = time.perf_counter() - t0
t0 = time.perf_counter()
m = SentenceTransformer("BAAI/bge-m3", device="cpu")
t_load = time.perf_counter() - t0
m.max_seq_length = 128
t0 = time.perf_counter()
m.encode(["预热 warmup 记忆系统 embedding"], normalize_embeddings=True, show_progress_bar=False)
t_first = time.perf_counter() - t0
q = "改签流程里 executeChange 返回码怎么判成功"
steady = []
for _ in range(20):
    t0 = time.perf_counter()
    m.encode([q], normalize_embeddings=True, show_progress_bar=False)
    steady.append(time.perf_counter() - t0)
steady.sort()
print(f"{t_import:.3f} {t_load:.3f} {t_first:.3f} {steady[10]:.3f} {steady[18]:.3f}")
"""


def free_ram_gb() -> float:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat.ullAvailPhys / 1024**3, stat.dwMemoryLoad


def main() -> int:
    avail, load = free_ram_gb()
    print(f"可用内存 {avail:.1f} GB / 内存压力 {load}%")
    print(f"{'轮次':<6}{'import':>9}{'load':>9}{'冷启动合计':>12}{'首次encode':>12}{'稳态p50':>10}{'稳态p95':>10}")
    cold, firsts, p50s = [], [], []
    for i in range(3):
        avail, load = free_ram_gb()
        out = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True)
        if out.returncode != 0:
            print(out.stderr[-800:])
            return 1
        t_import, t_load, t_first, p50, p95 = (float(x) for x in out.stdout.split())
        cold.append(t_import + t_load)
        firsts.append(t_first)
        p50s.append(p50)
        print(f"{i + 1:<6}{t_import:>8.2f}s{t_load:>8.2f}s{t_import + t_load:>11.2f}s"
              f"{t_first * 1000:>11.0f}ms{p50 * 1000:>9.0f}ms{p95 * 1000:>9.0f}ms"
              f"   (可用 {avail:.1f}G / {load}%)")

    print(f"\n冷启动 n=3: 中位 {statistics.median(cold):.1f}s  区间 {min(cold):.1f}-{max(cold):.1f}s")
    print(f"首次 encode 中位 {statistics.median(firsts) * 1000:.0f}ms  vs  稳态 p50 中位 "
          f"{statistics.median(p50s) * 1000:.0f}ms  →  倍数 "
          f"{statistics.median(firsts) / statistics.median(p50s):.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
