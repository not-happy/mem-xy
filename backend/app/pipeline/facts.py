"""§5.5 四动作写入器：ADD / UPDATE / DELETE(软) / NOOP。

★ **批量，不是每条一次 LLM**：按 (scope, org_key, project_key, subject, role) 分桶，
  一桶一次调用。逐条判会让"同一批里两条互为重复"永远发现不了。

★ **事务边界**（保证真幂等）：
    [事务外] 取候选 → LLM 判定 → 合并内容的 embedding
    [事务内] 写 memories + memory_events
  LLM 在事务里会把连接占住几十秒，池子(8)很快就干涸。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from psycopg import Connection

from ..core import db
from ..core.config import get_settings
from ..core.embedding import get_embedding_service
from ..core.hashing import content_hash
from ..core.llm import LLMParseError, get_llm_client
from ..prompts import write_facts as write_prompt

log = logging.getLogger(__name__)

CANDIDATES_PER_CHANNEL = 20   # 方案定的：向量 + 关键词各 top20
FINAL_CANDIDATES = 5          # reranker 之后真正喂给 LLM 的条数
MAX_FACTS_PER_CALL = 20       # 一桶太大就再切，prompt 长度要有上界
MAX_BUCKET_CANDIDATES = 20    # 一桶里所有新事实的候选取并集后的上界

_DENSE_SQL = """
SELECT id, content, keywords, mention_count, revision_count
  FROM memories
 WHERE status = 'active' AND embedding IS NOT NULL AND {bucket}
 ORDER BY embedding <=> %(vec)s::vector
 LIMIT %(limit)s
"""

_KEYWORD_SQL = """
SELECT id, content, keywords, mention_count, revision_count,
       (SELECT count(*) FROM unnest(keywords) w WHERE w = ANY(%(kw)s)) AS overlap
  FROM memories
 WHERE status = 'active' AND keywords && %(kw)s AND {bucket}
 ORDER BY overlap DESC, id
 LIMIT %(limit)s
"""

_BUCKET_WHERE = ("scope = %(scope)s AND subject = %(subject)s AND role = %(role)s"
                 " AND org_key IS NOT DISTINCT FROM %(org)s"
                 " AND project_key IS NOT DISTINCT FROM %(project)s")


@dataclass(frozen=True)
class Bucket:
    scope: str
    org_key: str | None
    project_key: str | None
    subject: str
    role: str

    def params(self) -> dict:
        return {"scope": self.scope, "subject": self.subject, "role": self.role,
                "org": self.org_key, "project": self.project_key}


@dataclass
class Fact:
    """一条待写入的新事实（来自 `write_facts` job 的 payload）。"""

    index: int
    source_msg: int
    content: str
    scope: str
    subject: str
    role: str
    keywords: list[str]
    why: str | None
    bucket: Bucket
    embedding: list[float] = field(default_factory=list, repr=False)
    candidates: list[dict] = field(default_factory=list, repr=False)


@dataclass
class WriteStats:
    items: int = 0
    buckets: int = 0
    calls: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    noop: int = 0
    dup_blocked: int = 0   # 被 UNIQUE(source_msg, content_hash) 挡掉 = 重放，正是它该干的活
    unjudged: int = 0      # LLM 漏判的 → 一律 ADD（宁可多一条重复，也不能丢素材）
    parse_failed: int = 0
    seconds: float = 0.0

    def merge(self, other: "WriteStats") -> None:
        for name, value in other.__dict__.items():
            setattr(self, name, getattr(self, name) + value)

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["seconds"] = round(out["seconds"], 2)
        return out


def bucket_of(item: dict, org_key: str | None, project_key: str | None) -> Bucket:
    """§6.1 的可见性条件反过来用：scope 决定这两个键**必须**填成什么。

    ★ `scope='user'` 一律不带 org_key——它要全局穿透到每个会话，
      带上 org 就会被 §6.1 的 org 分支过滤掉一半场景。
    """
    scope = item["scope"]
    return Bucket(
        scope=scope,
        org_key=None if scope == "user" else org_key,
        project_key=project_key if scope == "project" else None,
        subject=item["subject"],
        role=item["role"],
    )


def _facts_from(payload: dict) -> list[Fact]:
    org_key = payload.get("org_key")
    project_key = payload.get("project_key")
    facts = []
    for index, item in enumerate(payload.get("items") or []):
        bucket = bucket_of(item, org_key, project_key)
        if bucket.scope == "project" and not bucket.project_key:
            # memories 的 CHECK 约束会直接拒绝整批写入，所以在这里就拦下来
            log.warning("scope=project 但没有 project_key，跳过这条事实：%s", item.get("content"))
            continue
        facts.append(Fact(index=index, source_msg=item["source_msg"],
                          content=item["content"], scope=bucket.scope,
                          subject=item["subject"], role=item["role"],
                          keywords=item.get("keywords") or [], why=item.get("why"),
                          bucket=bucket))
    return facts


def _rrf(rankings: list[list[int]], k: int) -> list[tuple[int, float]]:
    """§6.2：只看排名、尺度无关。两路分数量纲不同（余弦 vs 重叠词数），加权和要反复调参。"""
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, memory_id in enumerate(ranking, start=1):
            scores[memory_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def _candidates(conn: Connection, fact: Fact) -> list[dict]:
    where = _BUCKET_WHERE
    params = {"vec": fact.embedding, "kw": fact.keywords,
              "limit": CANDIDATES_PER_CHANNEL, **fact.bucket.params()}
    dense = conn.execute(_DENSE_SQL.format(bucket=where), params).fetchall()
    keyed = (conn.execute(_KEYWORD_SQL.format(bucket=where), params).fetchall()
             if fact.keywords else [])
    by_id = {row["id"]: dict(row) for row in list(dense) + list(keyed)}
    fused = _rrf([[r["id"] for r in dense], [r["id"] for r in keyed]],
                 get_settings().recall_rrf_k)
    out = []
    for memory_id, score in fused[:MAX_BUCKET_CANDIDATES]:
        row = by_id[memory_id]
        row["rrf"] = score
        out.append(row)
    return out


def rerank(fact: Fact, limit: int = FINAL_CANDIDATES) -> list[dict]:
    """★ **这里本该是 bge-reranker-v2-m3**（方案 §2.2/§2.4 ③：只进写路径，独立 worker 进程）。
    那个模型约 2GB，本机 HF 缓存里没有、HF 主站又被墙，**没有授权就不下载**，
    所以当前退化成"按 RRF 名次截断"。

    退化的代价是可恢复的：近义重复的候选可能排在 top5 之外，本该 NOOP 的被 ADD 成重复行，
    下一批写入还有机会合并。不像漏抽素材那样不可逆。
    """
    return fact.candidates[:limit]


def _prompt_user_message(facts: list[Fact]) -> str:
    new_lines = []
    for position, fact in enumerate(facts):
        keywords = "、".join(fact.keywords) if fact.keywords else "（无）"
        new_lines.append(f"[{position}] ({fact.scope}/{fact.subject}/{fact.role}) "
                         f"{fact.content}\n     关键词：{keywords}")
    seen: dict[int, dict] = {}
    for fact in facts:
        for row in rerank(fact):
            seen.setdefault(row["id"], row)
    if seen:
        old_lines = [f"- id={row['id']} (提及 {row['mention_count']} 次) {row['content']}"
                     for row in seen.values()]
    else:
        old_lines = ["（这个分区里还没有任何记忆，全部按 ADD 处理）"]
    return ("【新事实】\n" + "\n".join(new_lines) +
            "\n\n【已有记忆】（同一分区，按相关度排序）\n" + "\n".join(old_lines))


def _judge(facts: list[Fact], llm) -> dict[int, object]:
    """一桶一次 LLM。返回 {新事实在本批里的位置: 判定}，漏判的不在字典里。"""
    result = llm.structured(
        "write_facts",
        system=write_prompt.SYSTEM,
        messages=[{"role": "user", "content": _prompt_user_message(facts)}],
        tool=write_prompt.TOOL,
        schema=write_prompt.WriteResult,
    )
    return {d.index: d for d in result.decisions if 0 <= d.index < len(facts)}


def write_facts(payload: dict, *, llm=None) -> WriteStats:
    """★ 自己管连接：读候选一个短事务，写入另一个短事务，LLM 调用夹在两者之间。
    不接受外部 `conn`——把 LLM 关在调用方的事务里正是 §5.5 明确禁止的。"""
    stats = WriteStats()
    facts = _facts_from(payload)
    stats.items = len(facts)
    if not facts:
        return stats

    service = get_embedding_service()
    vectors = service.encode([f.content for f in facts])   # 内部按 batch_size=32 分批
    for fact, vector in zip(facts, vectors):
        fact.embedding = vector

    with db.transaction() as tx:
        db.enable_iterative_scan(tx)
        for fact in facts:
            fact.candidates = _candidates(tx, fact)

    client = llm or get_llm_client()
    buckets: dict[Bucket, list[Fact]] = defaultdict(list)
    for fact in facts:
        buckets[fact.bucket].append(fact)
    stats.buckets = len(buckets)

    decisions: dict[int, object] = {}
    for bucket, group in buckets.items():
        for start in range(0, len(group), MAX_FACTS_PER_CALL):
            chunk = group[start:start + MAX_FACTS_PER_CALL]
            started = time.perf_counter()
            try:
                judged = _judge(chunk, client)
            except LLMParseError as exc:
                # 判不出就整桶 ADD：多几条重复记忆下次还能合并，丢掉素材捡不回来。
                stats.parse_failed += 1
                log.error("桶 %s 的输出不合 schema，整桶按 ADD 落库：%s", bucket, exc)
                judged = {}
            stats.calls += 1
            stats.seconds += time.perf_counter() - started
            for position, fact in enumerate(chunk):
                if position in judged:
                    decisions[fact.index] = judged[position]

    merged = _embed_merged(decisions, facts, service)
    with db.transaction() as tx:
        _apply_all(tx, facts, decisions, merged, payload, stats)
    return stats


def _embed_merged(decisions: dict, facts: list[Fact], service) -> dict[int, list[float]]:
    """UPDATE 会改写 content，向量必须跟着重算——放在事务外批量算。"""
    targets = []
    for fact in facts:
        decision = decisions.get(fact.index)
        if decision is not None and decision.action == "UPDATE":
            targets.append((fact.index, decision.merged_content))
    if not targets:
        return {}
    vectors = service.encode([text for _, text in targets])
    return dict(zip((index for index, _ in targets), vectors))


def _apply_all(conn: Connection, facts: list[Fact], decisions: dict,
               merged: dict[int, list[float]], payload: dict, stats: WriteStats) -> None:
    prompt_ver = payload.get("prompt_ver")
    for fact in facts:
        decision = decisions.get(fact.index)
        if decision is None:
            stats.unjudged += 1
            _add(conn, fact, fact.content, fact.embedding, prompt_ver, stats)
            continue
        action = decision.action
        if action == "NOOP":
            stats.noop += 1
            conn.execute(
                "UPDATE memories SET mention_count = mention_count + %s, last_seen_at = now()"
                " WHERE id = %s AND status = 'active'",
                (decision.mention_delta, decision.memory_id))
            _event(conn, decision.memory_id, "NOOP", None, None, fact.source_msg)
        elif action == "UPDATE":
            old = conn.execute("SELECT content FROM memories WHERE id=%s",
                               (decision.memory_id,)).fetchone()
            stats.updated += 1
            conn.execute(
                "UPDATE memories SET content=%s, content_hash=%s, embedding=%s, keywords=%s,"
                "                    why=coalesce(%s, why), revision_count=revision_count+1,"
                "                    updated_at=now(), last_seen_at=now(), prompt_ver=%s"
                " WHERE id=%s",
                (decision.merged_content, content_hash(decision.merged_content),
                 merged.get(fact.index), fact.keywords, fact.why, prompt_ver,
                 decision.memory_id))
            _event(conn, decision.memory_id, "UPDATE",
                   old["content"] if old else None, decision.merged_content, fact.source_msg)
        elif action == "DELETE":
            # 矛盾：软删旧的 + 把新事实 ADD 进去。旧内容留在 memory_events 里，
            # 这样"曾经以为 X，后来发现是 Y"这条轨迹是可追溯的。
            old = conn.execute("SELECT content FROM memories WHERE id=%s",
                               (decision.memory_id,)).fetchone()
            stats.deleted += 1
            conn.execute("UPDATE memories SET status='deleted', updated_at=now() WHERE id=%s",
                         (decision.memory_id,))
            _event(conn, decision.memory_id, "DELETE",
                   old["content"] if old else None, fact.content, fact.source_msg)
            _add(conn, fact, fact.content, fact.embedding, prompt_ver, stats)
        else:
            _add(conn, fact, fact.content, fact.embedding, prompt_ver, stats)


def _add(conn: Connection, fact: Fact, content: str, embedding: list[float],
         prompt_ver: str | None, stats: WriteStats) -> None:
    row = conn.execute(
        """
        INSERT INTO memories (scope, org_key, project_key, subject, role, content,
                              content_hash, keywords, why, embedding, source_msg, prompt_ver)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_msg, content_hash) DO NOTHING
        RETURNING id
        """,
        (fact.scope, fact.bucket.org_key, fact.bucket.project_key, fact.subject, fact.role,
         content, content_hash(content), fact.keywords, fact.why, embedding,
         fact.source_msg, prompt_ver),
    ).fetchone()
    if row is None:
        # ★ 这就是"硬幂等"：job 重试、崩溃重放到这里被唯一约束吃掉，不产生第二行。
        stats.dup_blocked += 1
        return
    stats.added += 1
    _event(conn, row["id"], "ADD", None, content, fact.source_msg)


def _event(conn: Connection, memory_id: int, action: str, old: str | None, new: str | None,
           source_msg: int) -> None:
    conn.execute(
        "INSERT INTO memory_events (memory_id, action, old_content, new_content, source_msg)"
        " VALUES (%s,%s,%s,%s,%s)",
        (memory_id, action, old, new, source_msg),
    )
