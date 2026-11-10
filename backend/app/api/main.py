"""FastAPI application（§7.1）。读路径 + 摄取 + 记忆库/待办；/healthz 留在根。

★ 进程收敛（EXPERT-REVIEW §5.2）：前端构建产物（frontend/dist）由本应用直接托管，
UI 与 API 同源同进程——日常运行不再需要常驻的 `npm run dev`（Vite 开发服务器
只在改前端代码时用，见 service.ps1 start frontend）。前端全部请求走同源 /v1
（api.js），tab 路由用 location.hash（不上服务器），所以挂 StaticFiles(html=True)
即可，无需 SPA fallback。
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core import db
from ..core.config import get_settings
from ..core.embedding import get_embedding_service
from ..core.redact import redact
from .attribution import router as attribution_router
from .conversations import router as conversations_router
from .docs import router as docs_router
from .ingest import router as ingest_router
from .jobs import router as jobs_router
from .memories import router as memories_router
from .metrics import router as metrics_router
from .orgs import router as orgs_router
from .read import router as read_router
from .replay import router as replay_router
from .sections import router as sections_router
from .settings import router as settings_router
from .workers import router as workers_router

log = logging.getLogger(__name__)


def _iterative_scan_ok(server_version: int, pgvector_version: str | None) -> bool:
    """`hnsw.iterative_scan` needs pgvector >= 0.8.0 (plan §6.3).

    Compared numerically: the string test `"0.10.0" >= "0.8"` is False, which would
    silently disable post-filtering on a future pgvector release.
    """
    if server_version < 120000 or not pgvector_version:
        return False
    parts = tuple(int(p) for p in re.findall(r"\d+", pgvector_version)[:3])
    return parts >= (0, 8, 0)


def _load_embedding_async() -> None:
    """Cold start is 15s; serve HTTP immediately and report readiness via /healthz.

    Until ready, callers get 503 `model_loading`, which is exactly the signal the hook
    needs to fall back to the dumb cache (plan §1.1, §4.3.3).
    """
    def worker() -> None:
        try:
            get_embedding_service().load()
        except Exception:  # noqa: BLE001
            log.exception("embedding model failed to load")

    threading.Thread(target=worker, name="embedding-loader", daemon=True).start()


def _autostart_workers(settings) -> None:
    """后端起来就把 worker 补齐到 `workers.autostart`（默认 2，0=关）。

    ★ 为什么放在 lifespan 而不是靠人工点 UI：worker 是独立进程，后端重启不会带上它，
      而"没人跑 job"是**静默故障**——route/apply_decision 全积压在 pending，
      UI 无报错、hook 无感知，直到人发现观察池/记忆不再增长（2026-11-02 实际踩过）。
    ★ 失败只记日志不抛：worker 起不来不该拖死后端本体（读路径不依赖 worker）。
    """
    try:
        from .. import workers_ctl
        target = settings.worker_autostart
        if target <= 0:
            return
        workers = workers_ctl.ensure(target)
        alive = [w["pid"] for w in workers if w["alive"]]
        log.info("workers autostarted: target=%s alive=%s", target, alive)
    except Exception:  # noqa: BLE001
        log.exception("worker autostart failed; 后端继续，手动 POST /v1/workers/start")


def _start_daily_backup(settings) -> None:
    """backend 内置每日备份调度（SASE 拦计划任务链的绕行，见 backup_daily 模块注释）。

    ★ 失败只记日志不抛：备份调度起不来不该拖死后端本体。
    """
    try:
        from .. import backup_daily
        backup_daily.start(settings.backup_daily_at)
    except Exception:  # noqa: BLE001
        log.exception("daily backup scheduler failed to start; 备份需手动 .\\backup.ps1 backup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db.init_pool()
    _load_embedding_async()
    _autostart_workers(settings)
    _start_daily_backup(settings)
    log.info("service up on %s:%s", settings.http_host, settings.http_port)
    yield
    db.close_pool()


app = FastAPI(title="mem-xy", version="0.1.0", lifespan=lifespan)

# ★ 读路径挂在 /v1 下（§7.1）。/healthz 留在根：hook 在降级判断里要调它，
#   而它的语义是"这个进程活着吗"，不属于业务接口。
app.include_router(read_router)
app.include_router(ingest_router)
app.include_router(memories_router)
app.include_router(docs_router)
app.include_router(attribution_router)
app.include_router(metrics_router)
app.include_router(jobs_router)
app.include_router(conversations_router)
app.include_router(orgs_router)
app.include_router(replay_router)
app.include_router(sections_router)
app.include_router(settings_router)
app.include_router(workers_router)


@app.get("/healthz")
def healthz() -> JSONResponse:
    service = get_embedding_service()
    ready = service.ready
    payload = {
        "status": "ok" if ready else "model_loading",
        "embedding": {
            "loaded": ready,
            "loading": service.loading,
            "max_seq_length": service.stats.max_seq_length,
            "modules": service.stats.modules,
            "sparse_available": service.stats.sparse_available,
            "load_seconds": service.stats.load_seconds,
            "avg_ms_per_row": round(service.stats.avg_ms_per_row, 2),
        },
    }
    status_code = 200 if ready else 503
    try:
        num, ver, pgvector = db.server_version()
        payload["db"] = {
            "server_version": ver,
            "pgvector": pgvector,
            "iterative_scan_ok": _iterative_scan_ok(num, pgvector),
        }
    except Exception as exc:  # noqa: BLE001
        status_code = 503
        # psycopg error strings carry the connection string, i.e. the DB password.
        payload["db"] = {"error": redact(str(exc))[:200]}
    return JSONResponse(payload, status_code=status_code)


# ── 前端静态托管（进程收敛）────────────────────────────────────────────
# ★ 必须放在所有 router 注册之后：Starlette 按注册顺序匹配，挂 "/" 的 mount
#   会吃掉一切未匹配路径——放最后则 /v1、/healthz 先命中，静态文件兜底。
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def mount_frontend(target: FastAPI, dist: Path | None = None) -> bool:
    """把前端构建产物挂到 "/"；dist 缺失时只记日志不挂载（API 照常工作）。

    抽成函数是为了可测试：单测传临时目录，不依赖真实 npm build。
    返回是否挂载成功，供测试与调用方判断。
    """
    dist = dist or FRONTEND_DIST
    if not (dist / "index.html").is_file():
        log.warning(
            "未找到前端构建产物（%s/index.html）：UI 不可用，API 不受影响。"
            "构建：cd frontend && npm run build；开发模式：service.ps1 start frontend",
            dist)
        return False
    target.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    log.info("前端 UI 已挂载（%s）", dist)
    return True


mount_frontend(app)
