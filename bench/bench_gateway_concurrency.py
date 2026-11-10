"""网关并发探测（§14 第 2 项）：并发/限流曲线，决定全量回填开几个 worker。

    python bench/bench_gateway_concurrency.py            # 默认测 1/2/4 三档
    python bench/bench_gateway_concurrency.py --levels 1,2,4,8

★ 成本控制：thinking OFF + max_tokens=32 + 一句话 prompt，单次调用 ~几十 token。
  默认 7 次调用总共 < 2k token。每档并发**同时**发出，量墙钟与单次延迟的比值：
    比值 ≈ 1  → 网关真并行，回填可以开 N 个 worker
    比值 ≈ N  → 网关串行/限流，多开 worker 没用
  任何一档出现 429/5xx/超时即停，不再往上加档。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import db  # noqa: E402
from app.core.config import get_settings, llm_token  # noqa: E402
from app.core.llm import HTTP_TIMEOUT, Endpoint, LLMError  # noqa: E402
from app.core.redact import redact, register_secrets  # noqa: E402

import httpx  # noqa: E402

PROMPT = "只回复两个字：收到"


def _call_once(client: LLMClientLike, endpoint: Endpoint) -> dict:
    started = time.perf_counter()
    try:
        response = client.post(
            f"{endpoint.base_url.rstrip('/')}/v1/messages",
            headers=_headers(endpoint),
            json={
                "model": endpoint.model,
                "max_tokens": 32,
                "thinking": {"type": "disabled"},   # 探测不需要 thinking，省钱省时
                "messages": [{"role": "user", "content": PROMPT}],
            },
        )
        elapsed = time.perf_counter() - started
        if response.status_code >= 400:
            return {"ok": False, "seconds": elapsed, "status": response.status_code,
                    "error": redact(response.text[:200])}
        raw = response.json()
        usage = raw.get("usage") or {}
        return {"ok": True, "seconds": elapsed, "status": 200,
                "in": int(usage.get("input_tokens") or 0),
                "out": int(usage.get("output_tokens") or 0)}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "seconds": time.perf_counter() - started,
                "status": exc.response.status_code,
                "error": redact(exc.response.text[:200])}
    except httpx.HTTPError as exc:
        return {"ok": False, "seconds": time.perf_counter() - started,
                "status": 0, "error": redact(f"{type(exc).__name__}: {exc}")[:200]}


LLMClientLike = httpx.Client


def _headers(endpoint: Endpoint) -> dict:
    token = llm_token(endpoint.endpoint_key)
    if not token:
        raise SystemExit(f"endpoint {endpoint.endpoint_key} 没配 token（§2.1）")
    register_secrets([token])
    return {"content-type": "application/json", "accept": "application/json",
            "authorization": f"Bearer {token}", "x-api-key": token}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", default="1,2,4", help="逗号分隔的并发档位")
    args = parser.parse_args()
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    db.init_pool(min_size=1, max_size=1)
    try:
        settings = get_settings()
        row = db.query_one(
            "SELECT endpoint_key, base_url, model, api_protocol, max_input_tokens"
            " FROM llm_endpoints WHERE endpoint_key=%s AND enabled",
            (settings.default_endpoint,))
        if not row:
            raise SystemExit(f"端点 {settings.default_endpoint} 不存在或未启用")
        endpoint = Endpoint(**row)
    finally:
        db.close_pool()

    print(f"端点 {endpoint.endpoint_key} ({endpoint.model})，档位 {levels}")
    total_in = total_out = 0
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        for level in levels:
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=level) as pool:
                results = list(pool.map(lambda _: _call_once(client, endpoint),
                                        range(level)))
            wall = time.perf_counter() - started
            oks = [r for r in results if r["ok"]]
            bad = [r for r in results if not r["ok"]]
            total_in += sum(r.get("in", 0) for r in results)
            total_out += sum(r.get("out", 0) for r in results)
            lat = " ".join(f"{r['seconds']:.1f}s" for r in results)
            speedup = (sum(r["seconds"] for r in oks) / len(oks) / wall) if oks else 0
            print(f"\n并发 {level}: 墙钟 {wall:.1f}s | 单次延迟 [{lat}] | "
                  f"成功 {len(oks)}/{level} | 并行度 {speedup:.2f}x")
            for r in bad:
                print(f"  ✗ HTTP {r['status']}: {r.get('error', '')[:150]}")
            if bad:
                print("\n★ 出现失败，停止加档。")
                break

    print(f"\n探测总消耗：in {total_in} / out {total_out} tok")
    print("判读：并行度 ≈1.0 ⇒ 网关串行，回填单 worker；≈N ⇒ 可开 N 个 worker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
