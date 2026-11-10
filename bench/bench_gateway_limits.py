"""网关上下文上限 + 长 prompt 延迟曲线探测（plan §14 item2 第 1/3 项）。

    python bench/bench_gateway_limits.py                     # 默认档 48k/64k/96k/128k
    python bench/bench_gateway_limits.py --levels 48000,64000
    python bench/bench_gateway_limits.py --refine            # 失败后二分细化上限

背景：llm_endpoints.max_input_tokens=32000 是 seed.py 自认的"保守占位值"，
调用路径无人消费、无本地拦截；生产 route 实际送出过 42338 tok 且成功——
真实上限 ≥42k 但未测。本探测把上限钉死，回填配置。

★ 直接 httpx 发请求（与 bench_gateway_concurrency 同款），不走 LLMClient——
  避免探测调用写进 llm_usage 污染生产监控均值（评测 runner 用 write_log=False
  是同一个理由）。token 消耗在末尾自报。
★ 填充文本用中文重复句（qwen 中文压缩率高，字符量省）；先小样本标定
  字符/token 比，再按目标 token 构造。实际 input_tokens 以网关 usage 为准。
★ read 超时给 300s（业务侧 HTTP_TIMEOUT read=180s；大 prompt prefill 可能更久，
  探测别把"慢"误判成"死"）。
★ 任何一档 HTTP 400 且报错含上下文超限关键词 → 上限在 (上一档, 本档]，停。
  非超限错误（429/5xx/超时）也停，但标注"非上限结论"。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import db  # noqa: E402
from app.core.config import get_settings, llm_token  # noqa: E402
from app.core.llm import Endpoint  # noqa: E402
from app.core.redact import redact, register_secrets  # noqa: E402

import httpx  # noqa: E402

INSTRUCTION = "\n\n以上是一段填充文本，无需总结。请只回复两个字：收到"
FILLER_UNIT = "个人记忆系统在单机环境下运行，数据每天自动加密备份并同步到私有仓库。"
# 上下文超限错误的识别关键词（anthropic 协议网关常见文案，大小写不敏感匹配）
LIMIT_WORDS = ("context", "length", "exceed", "maximum", "too long", "token")
TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)


def _headers(endpoint: Endpoint) -> dict:
    token = llm_token(endpoint.endpoint_key)
    if not token:
        raise SystemExit(f"endpoint {endpoint.endpoint_key} 没配 token（§2.1）")
    register_secrets([token])
    return {"content-type": "application/json", "accept": "application/json",
            "authorization": f"Bearer {token}", "x-api-key": token}


def _send(client: httpx.Client, endpoint: Endpoint, text: str) -> dict:
    """单次调用；任何异常都收敛成 dict（探测不能半途崩）。"""
    body = {
        "model": endpoint.model,
        "max_tokens": 32,
        "thinking": {"type": "disabled"},   # 探测不需要 thinking：省钱、去噪
        "messages": [{"role": "user", "content": text}],
    }
    started = time.perf_counter()
    try:
        resp = client.post(f"{endpoint.base_url.rstrip('/')}/v1/messages",
                           headers=_headers(endpoint), json=body)
    except httpx.HTTPError as exc:
        return {"ok": False, "seconds": time.perf_counter() - started, "status": 0,
                "error": redact(f"{type(exc).__name__}: {exc}")[:200]}
    seconds = time.perf_counter() - started
    if resp.status_code >= 400:
        return {"ok": False, "seconds": seconds, "status": resp.status_code,
                "error": redact(resp.text[:300])}
    try:
        raw = resp.json()
    except ValueError:
        # ★ 200 但 body 非 JSON = 网关"空响应"形态之一（记忆 #2461），单独记录
        return {"ok": False, "seconds": seconds, "status": 200,
                "error": f"non-JSON body ({len(resp.content)} bytes)"}
    content = raw.get("content") or []
    has_text = any(b.get("type") == "text" and b.get("text", "").strip()
                   for b in content)
    usage = raw.get("usage") or {}
    # ★ 网关支持 prompt cache：重复前缀进 cache_read_input_tokens，
    #   usage.input_tokens 只报未缓存增量（2026-11-10 实测：64k 档只报 15870）。
    #   上下文占用 = 三者之和，上限判定必须用 in_total。
    in_new = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    return {"ok": True, "seconds": seconds, "status": 200,
            "in": in_new, "cache_read": cache_read, "cache_create": cache_create,
            "in_total": in_new + cache_read + cache_create,
            "out": int(usage.get("output_tokens") or 0),
            "empty_content": not has_text}


def _filler(n_chars: int) -> str:
    repeats = n_chars // len(FILLER_UNIT) + 1
    return (FILLER_UNIT * repeats)[:n_chars] + INSTRUCTION


def _is_context_limit(error: str) -> bool:
    low = error.lower()
    return any(w in low for w in LIMIT_WORDS)


def _calibrate(client: httpx.Client, endpoint: Endpoint) -> float:
    """小样本标定字符/token 比：发 1500 字符，用网关回的 input_tokens 反推。"""
    probe_chars = 1500
    r = _send(client, endpoint, _filler(probe_chars))
    if not r["ok"]:
        raise SystemExit(f"标定失败: HTTP {r['status']} {r.get('error', '')[:150]}")
    overhead = len(INSTRUCTION)
    ratio = (probe_chars - overhead) / max(r["in"] - 20, 1)  # 20 ≈ 指令+信封开销
    print(f"标定：{probe_chars} 字符 → {r['in']} tok，约 {ratio:.2f} 字符/token")
    return ratio


def _probe_level(client: httpx.Client, endpoint: Endpoint, target: int,
                 ratio: float) -> dict:
    chars = int(target * ratio)
    r = _send(client, endpoint, _filler(chars))
    r["target"] = target
    return r


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", default="48000,64000,96000,128000",
                        help="逗号分隔的目标 input token 档位")
    parser.add_argument("--refine", action="store_true",
                        help="细化模式：在 --levels 给的 [lo,hi] 两档间二分")
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

    print(f"端点 {endpoint.endpoint_key} ({endpoint.model})，"
          f"配置 max_input_tokens={endpoint.max_input_tokens}，"
          f"档位 {levels}{'（二分细化）' if args.refine else ''}")
    total_in = total_out = 0
    last_ok: dict | None = None
    first_bad: dict | None = None
    with httpx.Client(timeout=TIMEOUT) as client:
        ratio = _calibrate(client, endpoint)
        pending = list(levels)
        while pending:
            target = pending.pop(0)
            r = _probe_level(client, endpoint, target, ratio)
            total_in += r.get("in_total", r.get("in", 0))
            total_out += r.get("out", 0)
            if r["ok"]:
                mark = "（★ content 为空 = 网关空响应形态）" if r.get("empty_content") else ""
                cache = (f" | cache读 {r['cache_read']}" if r.get("cache_read") else "")
                print(f"  ✓ 目标 {target:>7} → 实际 {r['in_total']:>7} tok{cache} | "
                      f"{r['seconds']:6.1f}s{mark}")
                last_ok = r
            else:
                print(f"  ✗ 目标 {target:>7} | HTTP {r['status']} | "
                      f"{r['seconds']:.1f}s | {r.get('error', '')[:150]}")
                if r["status"] == 400 and _is_context_limit(r.get("error", "")):
                    if first_bad is None or target < first_bad["target"]:
                        first_bad = r
                    if args.refine and last_ok and target - last_ok["target"] > 4000:
                        # 二分细化：在 (last_ok, target) 中点再试一档
                        pending.insert(0, last_ok["target"] + (target - last_ok["target"]) // 2)
                        continue
                    print("\n★ 命中上下文超限，停止加档。")
                    break
                first_bad = r
                print("\n★ 非超限失败（429/5xx/超时/空响应），停止加档——非上限结论。")
                break

    print(f"\n探测总消耗：in {total_in} / out {total_out} tok")
    if last_ok and first_bad and first_bad["status"] == 400:
        print(f"结论：真实上下文上限 ∈ ({last_ok.get('in_total', last_ok['target'])}, "
              f"{first_bad['target']}]（实际 tok 口径见上表）")
    elif last_ok and not first_bad:
        print(f"结论：≥ {last_ok.get('in_total', last_ok['target'])} tok 可用，未探到上限；"
              f"可加档 --levels {last_ok['target'] * 2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
