"""指标端点（§7.1 的 `GET /v1/metrics`，P6）。

★ 和 attribution.py / docs.py 同构：这一层只校验形状、翻译错误码、序列化。
  业务规则全在 `app/metrics.py`。纯 SQL 聚合，不碰模型，所以不需要 503 拦截。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import metrics
from .memories import _guard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


@router.get("/metrics")
def get_metrics(
        org_key: str | None = None,
        latency_window_days: int = Query(default=30, ge=1, le=365),
        throughput_window_days: int = Query(default=14, ge=1, le=365),
        sample_limit: int = Query(default=30, ge=1, le=200)) -> JSONResponse:
    """§10 全部指标的只读聚合。给了 `org_key` 就按分区裁剪。"""
    return _guard(metrics.overview, org_key=org_key,
                  latency_window_days=latency_window_days,
                  throughput_window_days=throughput_window_days,
                  sample_limit=sample_limit)
