"""常驻服务：`python -m app.commands.serve`。

★ **必须常驻，不能"用的时候起一个"**：bge-m3 冷启动实测 33s（n=3，中位 33.4s，
  区间 29.8–34.0s，会漂），而 hook 的预算是 400ms。模型只能活在这个进程里，
  `mem-hook` 是纯 HTTP 瘦客户端（§1.1 的两条硬约束之一）。

★ `reload` 一律关掉：它会起子进程监视文件，每次改动都重新加载一遍模型 = 又一次 33s。
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mem-xy 常驻服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--workers", type=int, default=1,
                        help="★ 只能 1：多进程会各加载一份 bge-m3（每份约 2GB 内存）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s %(message)s")
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

    from app.core.config import get_settings

    settings = get_settings()
    host = args.host or settings.http_host
    port = args.port or settings.http_port

    print(f"mem-xy 服务 → http://{host}:{port}")
    print(f"  /healthz                存活 + 模型是否预热完（预热约 33s，期间 recall 回 503）")
    print(f"  /v1/context/bootstrap   SessionStart 稳定层（不等模型）")
    print(f"  /v1/recall              每轮召回")
    print(f"  /v1/recall/debug        召回分解（不写审计）")
    print(f"  /v1/ingest/scan         Stop hook 触发；?sync=true 就地跑完")
    print("★ worker 随后端自启（workers.autostart，默认 2 个）；任务队列页可手动启停")

    import uvicorn

    uvicorn.run("app.api.main:app", host=host, port=port,
                workers=args.workers, reload=False, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
