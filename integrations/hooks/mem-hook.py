#!/usr/bin/env python3
"""mem-hook：Claude Code hooks 的瘦 HTTP 客户端（§1.1）。

★ **两条硬约束，改这个文件之前先读一遍**：

1. **只用标准库**。这个脚本每轮对话都要起一次进程，而 `import sentence_transformers`
   就要 28–32s（bge-m3 冷启动实测 33s，§2.4）。模型只能活在 `app.commands.serve`
   那个常驻进程里，这里只准发 HTTP。连 `httpx` 都不用——`urllib` 省掉约 100ms 的导入。
2. **400ms 超时 + 失败静默降级**。记忆服务挂了、还在预热、返回 5xx，一律**退出码 0、
   不打印错误**。hook 报错会打断对话，而"这次没注入记忆"根本不该让用户看见。
   bootstrap 降级走哑缓存（§4.3.3），recall 降级成什么都不注入。

   ★ 超时不是"最坏情况"，是服务没起时的**每轮常态**：Windows 上连一个没人监听的
   127.0.0.1 端口收不到 RST，实测耗满 402ms（`localhost` 更糟，先试 `::1` 再试
   `127.0.0.1`，801ms）。所以有个 30s 的哑熔断，见 `_breaker_open`。

接入（`~/.claude/settings.json`）：

    {"hooks": {
       "SessionStart":     [{"hooks":[{"type":"command","command":"python <绝对路径>/mem-hook.py bootstrap"}]}],
       "UserPromptSubmit": [{"hooks":[{"type":"command","command":"python <绝对路径>/mem-hook.py recall"}]}],
       "Stop":             [{"hooks":[{"type":"command","command":"python <绝对路径>/mem-hook.py flush"}]}]}}

环境变量（都可选）：`MEMXY_URL`、`MEMXY_TIMEOUT_MS`、`MEM_HOME`。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

# ★ `urllib` 故意**不在**这里 import：它连带拉进 email/http/ssl，实测约 100ms，
#   而熔断开着的离线轮次根本不需要它。延迟到 `_call` 里再导，离线的每轮就从
#   255ms 降到 ~150ms。在线那轮总量不变（该付的还是要付）。

# ★ 写死 127.0.0.1 不写 localhost：localhost 会先解析出 ::1，IPv6 上没人监听时要白等
#   一轮超时才回落到 IPv4，离线代价从 400ms 变成 800ms（实测）。
DEFAULT_URL = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT_MS = 400
BREAKER_COOLDOWN_SECONDS = 30.0
_BREAKER_FILE = "offline-since"

OFFLINE_BANNER = "⚠ 记忆服务离线，以下为上次缓存的规则快照（可能过期）"


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("MEM_HOME") or (pathlib.Path.home() / ".memxy"))


def _cache_dir() -> pathlib.Path:
    return _home() / "cache"


def _base_url() -> str:
    return os.environ.get("MEMXY_URL", DEFAULT_URL).rstrip("/")


def _timeout() -> float:
    try:
        return int(os.environ.get("MEMXY_TIMEOUT_MS", DEFAULT_TIMEOUT_MS)) / 1000
    except ValueError:
        return DEFAULT_TIMEOUT_MS / 1000


def _read_stdin() -> dict:
    """hook 的输入是 stdin 上的一坨 JSON（含 session_id / cwd / prompt / transcript_path）。

    ★ 读不到就当空 dict，不抛：某些事件（或手工调用）根本没有 stdin。
    """
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """返回 (status_code, payload)。★ 任何异常都变成 (0, {})，由调用方降级。"""
    if _breaker_open():
        return 0, {}        # 冷静期内不再白等 400ms，也不再付 urllib 的导入钱
    import urllib.error
    import urllib.request

    url = f"{_base_url()}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            _reset_breaker()
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # 503 model_loading 走这条路：服务活着只是模型没热，**不**触发熔断，
        # 否则预热那 11–33s 会被误判成宕机、冷静期内一直不注入。
        return exc.code, {}
    except Exception:  # noqa: BLE001 — 超时/拒连/DNS/JSON 全都静默
        _trip_breaker()
        return 0, {}


def _breaker_open() -> bool:
    """上次连不上到现在还没过冷静期 → 跳过网络调用。

    ★ 和 §4.3.3 的哑缓存一个思路：只有一个时间戳文件，没有状态机、没有重试计数。
      代价是服务刚起来的头 30s 里可能少注入几轮记忆——比每轮白交 400ms 划算。
    """
    try:
        raw = (_cache_dir() / _BREAKER_FILE).read_text(encoding="utf-8").strip()
        return (time.time() - float(raw)) < BREAKER_COOLDOWN_SECONDS
    except Exception:  # noqa: BLE001 — 文件不存在/内容坏了都当"没跳闸"
        return False


def _trip_breaker() -> None:
    try:
        directory = _cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / _BREAKER_FILE).write_text(str(time.time()), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _reset_breaker() -> None:
    try:
        path = _cache_dir() / _BREAKER_FILE
        if path.exists():
            path.unlink()
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────── SessionStart ───────────────────────────

def _reset_session_state(session_id: str) -> None:
    """★ A1（S2）：会话重新开始时清掉上一次的注入状态。

    Claude Code 在 /compact 或 resume 后会重新触发 SessionStart——压缩后上下文
    已经没了，此前记下的"已注入"名单会让 recall 把这些记忆继续排除掉，
    结果是压缩后的会话**再也收不到任何已注入过的记忆**（S2 的静默失效）。
    新会话从空白状态开始，正常轮次仍由 exclude_ids 防重复注入，行为不变。
    """
    try:
        (_cache_dir() / f"session-{session_id}.json").unlink()
    except Exception:  # noqa: BLE001 — 文件不存在/删不掉都当"已经干净"
        pass


def bootstrap(event: dict) -> int:
    session_id = event.get("session_id") or "anonymous"
    # ★ A1：压缩/恢复后的新会话开始，先清上一轮的注入名单（见 _reset_session_state）。
    _reset_session_state(session_id)
    status, payload = _call("GET", "/v1/context/bootstrap?" + _query({
        "cwd": event.get("cwd"),
    }))
    markdown = (payload.get("markdown") or "").strip()

    if status == 200 and payload:
        cache_key = payload.get("cache_key")
        if cache_key and markdown:
            _write_cache(cache_key, markdown)
        if markdown:
            print(markdown)
        return 0

    # ★ 降级：读上次的快照。缓存是**只读派生物，永不回写库**，所以不构成第二真相源
    #   （§4.3.3：20 行代码替代整套双向同步）。
    cached = _read_cache(_guess_cache_key(event))
    if cached:
        print(f"{OFFLINE_BANNER}\n\n{cached}")
    return 0


def _query(params: dict) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v})


def _guess_cache_key(event: dict) -> str | None:
    """离线时拿不到服务端算的 cache_key，只能自己按 cwd 猜一个去找。

    ★ 猜不中就什么都不注入——**绝不**去遍历 cache 目录随便挑一个文件，
      那会把别的项目的规矩注入进来，正是 §6.1 要防的串台。
    """
    cwd = (event.get("cwd") or "").strip()
    if not cwd:
        return None
    normalized = cwd.replace("\\", "/").rstrip("/")
    repo = normalized.rsplit("/", 1)[-1]
    hits = sorted(_cache_dir().glob(f"bootstrap-*-{repo}.md"))
    return hits[0].stem if len(hits) == 1 else (hits[0].stem if hits else None)


def _write_cache(cache_key: str, markdown: str) -> None:
    try:
        directory = _cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{cache_key}.md").write_text(markdown, encoding="utf-8")
    except Exception:  # noqa: BLE001 — 写缓存失败不值得打断会话开始
        pass


def _read_cache(cache_key: str | None) -> str:
    if not cache_key:
        return ""
    try:
        return (_cache_dir() / f"{cache_key}.md").read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


# ─────────────────────────── UserPromptSubmit ───────────────────────────

def _clamp_prompt(prompt: str) -> str:
    """★ A3（S7 前置）：超长 prompt 取头 2000 + 尾 1000 再请求。

    后端 RecallRequest 对 query 的上限是 4000 字符（api/read.py），超了直接
    422——而 hook 的约定是"失败 = 静默零注入"，用户只会看到"这轮没有记忆"，
    根本想不到是 prompt 太长。头尾保留是经验：记忆匹配的锚点通常在开头的
    主题陈述和结尾的最新指令里，中间的粘贴正文贡献最小。
    """
    if len(prompt) <= 4000:
        return prompt
    return prompt[:2000] + "\n…（中间过长已截断）…\n" + prompt[-1000:]


def _last_assistant_summary(transcript_path: str | None) -> str | None:
    """★ A2-①（S1/H2）：从 transcript JSONL 尾部提取最后一条 assistant 文本。

    hook 的 stdin 里带 transcript_path，但此前从没读过——查询增广在服务端
    是死代码。这里只读文件**尾部 64KB**（大 transcript 不能整读，读盘+解析
    要抢 400ms 预算），从后往前找第一条带正文文本的 assistant 消息，
    截 2000 字符（后端 RecallRequest 的上限）。任何异常都返回 None，
    降级成"没有摘要"，不影响主流程。
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            raw = fh.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):          # 尾部向前；首行可能残缺，坏了就跳过
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            is_assistant = (entry.get("type") == "assistant"
                            or message.get("role") == "assistant")
            if not is_assistant:
                continue
            content = message.get("content")
            texts = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text") or "")
            text = "\n".join(t for t in texts if t).strip()
            if text:
                return text[:2000]
    except Exception:  # noqa: BLE001 — 读不到就当没有
        return None
    return None


def recall(event: dict) -> int:
    prompt = (event.get("prompt") or "").strip()
    if not prompt:
        return 0
    session_id = event.get("session_id") or "anonymous"

    status, payload = _call("POST", "/v1/recall", {
        "query": _clamp_prompt(prompt),
        "session_id": session_id,
        "cwd": event.get("cwd"),
        "source": "hook",
        # ★ A2-①：把上一条 assistant 回复的摘要交给服务端做查询增广
        #   （服务端再截 2000；recall 侧分段编码，见 retrieve/recall.py A2 注释）。
        "prev_assistant_summary": _last_assistant_summary(event.get("transcript_path")),
        # ★ 排除本 session 已注入过的：同一条记忆每轮都塞一遍，
        #   600 token 的预算（§1.2）几轮就烧光了，而且模型会开始忽略它。
        "exclude_ids": _session_state(session_id).get("injected", []),
    })
    items = payload.get("items") or [] if status == 200 else []
    if not items:
        return 0        # 降级 = 什么都不注入，不解释、不道歉

    print("<!-- mem-xy 召回的相关记忆（按相关度排序，可能过期，与当前代码冲突时以代码为准）-->")
    for item in items:
        print(f"- {item.get('text', '')}")
    _remember_injected(session_id, [i["memory_id"] for i in items])
    return 0


def _session_state(session_id: str) -> dict:
    try:
        return json.loads((_cache_dir() / f"session-{session_id}.json")
                          .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"injected": []}


def _remember_injected(session_id: str, ids: list[int]) -> None:
    """★ 上限 200 个 id：一个长会话可能召回几百次，状态文件不能无限长。
    截断的是最老的——它们也最可能被再次召回，但重复注入的代价远小于状态文件膨胀。"""
    seen = _session_state(session_id).get("injected", [])
    merged = list(dict.fromkeys([*seen, *ids]))[-200:]
    try:
        _cache_dir().mkdir(parents=True, exist_ok=True)
        (_cache_dir() / f"session-{session_id}.json").write_text(
            json.dumps({"injected": merged}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────── Stop ───────────────────────────

def flush(event: dict) -> int:
    """对话结束 → 让服务端扫一遍 transcript 并排队抽取（§7.1 `/v1/ingest/scan`）。

    ★ 服务端立刻回 202，真正的解析和 LLM 抽取在队列里干。这里**不等结果**：
      Stop hook 阻塞会让用户觉得"对话结束了但卡住了"。
    """
    if event.get("stop_hook_active"):
        return 0        # 避免 hook 自己触发的 Stop 再排一次
    _call("POST", "/v1/ingest/scan?sync=false", {})
    return 0


COMMANDS = {"bootstrap": bootstrap, "recall": recall, "flush": flush}


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    handler = COMMANDS.get(command)
    if handler is None:
        # ★ 唯一会说话的地方：命令名写错是配置错误，静默会让用户以为记忆系统在工作。
        sys.stderr.write(f"mem-hook: 未知命令 {command!r}，可用：{sorted(COMMANDS)}\n")
        return 2
    try:
        return handler(_read_stdin())
    except Exception:  # noqa: BLE001 — 最后一道兜底：hook 绝不能让对话失败
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
