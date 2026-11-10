"""量 mem-hook 的墙钟开销（P3 验收用，跑完即弃）。"""
import os
import pathlib
import shutil
import statistics
import subprocess
import time

HOOK = r"C:\Users\yong.xu\own\mem-xy\integrations\hooks\mem-hook.py"
CWD = "C:/Users/yong.xu/IdeaProjects/dida/gds-agg-processor-tiket"
EV = '{"session_id":"h","cwd":"%s","prompt":"Tiket 改签"}' % CWD
DEAD = "http://127.0.0.1:1"


def run(env_extra=None, n=15, fresh_home=False):
    ts = []
    for i in range(n):
        env = {**os.environ, **(env_extra or {})}
        if fresh_home:
            home = "/tmp/mh-%d" % i
            shutil.rmtree(home, ignore_errors=True)
            env["MEM_HOME"] = home
        t = time.perf_counter()
        subprocess.run(["python", HOOK, "recall"], input=EV,
                       capture_output=True, text=True, env=env)
        ts.append((time.perf_counter() - t) * 1000)
    return ts


def show(name, ts):
    s = sorted(ts)
    print("%-40s n=%2d p50=%5.0fms min=%5.0fms max=%5.0fms"
          % (name, len(ts), statistics.median(ts), s[0], s[-1]))


show("在线 recall（服务在跑）", run())
run(env_extra={"MEMXY_URL": DEAD}, n=2)          # 先跳闸
show("离线 recall（熔断已开）", run(env_extra={"MEMXY_URL": DEAD}))
show("离线 recall（无熔断，每轮新 HOME）", run(env_extra={"MEMXY_URL": DEAD}, fresh_home=True))
show("对照：python -c pass", [
    (lambda t0: (subprocess.run(["python", "-c", "pass"], capture_output=True),
                 (time.perf_counter() - t0) * 1000)[1])(time.perf_counter())
    for _ in range(15)])
