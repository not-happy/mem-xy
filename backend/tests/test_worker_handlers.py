"""worker 的 handler 注册表。

★ 不 gated on DB：注册表是纯静态的，而 `extract --run-jobs` 走的正是这张表。
  少注册一个 kind，job 不会静默跳过，而是被 `run_worker` 标成 failed
  （"no handler registered"）——症状是队列一直排空不掉，所以钉一组便宜的测试。
"""

from app import worker

SENTINEL = object()   # 代表 worker 递进来的连接：谁碰到它谁就错了


def test_the_p2_job_kinds_are_registered():
    assert set(worker.HANDLERS) >= {"route", "write_facts", "apply_decision"}


def test_the_p3_scan_kind_is_registered():
    """Stop hook → `/v1/ingest/scan` → 队列里那条 `scan` job。漏注册的症状是
    hook 每次都"成功"排了队，但队列永远排不空，而用户那边完全看不出来。"""
    assert "scan" in worker.HANDLERS


def test_the_p5_summarize_kind_is_registered():
    """§5.6 汇总器。漏注册的症状同上：观察池攒够了却永远排不出草稿。"""
    assert "summarize" in worker.HANDLERS


def test_the_summarize_handler_passes_filters_and_owns_its_transactions(monkeypatch):
    """★ 同 route/write_facts：里面要调 LLM，所以只接 payload、自管连接，
    绝不把 worker 的连接递下去（SENTINEL 被碰到就是 bug）。"""
    from app.pipeline import summarize

    seen = {}
    monkeypatch.setattr(summarize, "run",
                        lambda **kw: seen.update(kw) or summarize.SummarizeStats())
    worker._handle_summarize(
        {"id": 9, "payload": {"org_key": "dida", "section_key": "dependency",
                              "force": True}}, SENTINEL)
    assert seen == {"org_key": "dida", "project_key": None,
                    "section_key": "dependency", "force": True}


def test_the_summarize_handler_defaults_to_a_full_scan(monkeypatch):
    from app.pipeline import summarize

    seen = {}
    monkeypatch.setattr(summarize, "run",
                        lambda **kw: seen.update(kw) or summarize.SummarizeStats())
    worker._handle_summarize({"payload": None}, SENTINEL)
    assert seen == {"org_key": None, "project_key": None,
                    "section_key": None, "force": False}


def test_the_scan_handler_defaults_to_not_following_subagents(monkeypatch):
    from app.ingest import scan

    seen = []
    monkeypatch.setattr(scan, "scan_and_queue",
                        lambda **kw: seen.append(kw) or {"route_queued": 0})
    worker._handle_scan({"payload": None}, SENTINEL)
    assert seen == [{"include_subagents": False}]


def test_the_scan_handler_passes_the_flag_through(monkeypatch):
    from app.ingest import scan

    seen = []
    monkeypatch.setattr(scan, "scan_and_queue",
                        lambda **kw: seen.append(kw) or {"route_queued": 0})
    worker._handle_scan({"payload": {"include_subagents": True}}, SENTINEL)
    assert seen == [{"include_subagents": True}]


def test_the_route_handler_does_not_hand_its_connection_down(monkeypatch):
    """★ §5.5：LLM 调用绝不能待在别人的事务里。`route_session` 自己开会话级事务，
    所以这里**故意不传 conn**——传了就会把几十秒的 LLM 调用锁在一个事务里。"""
    from app.pipeline import router

    seen = {}
    monkeypatch.setattr(router, "route_session",
                        lambda session_id, **kw: seen.update(session_id=session_id, **kw))
    worker._handle_route({"payload": {"session_id": "s1", "limit": 3}}, SENTINEL)
    assert seen["session_id"] == "s1"
    assert seen["source"] == "claude-code"
    assert seen["limit"] == 3
    assert seen["on_window"] is None      # 没有 job id 就别去动队列


def test_a_routed_session_renews_its_lease_once_per_window(monkeypatch):
    """★ 一个会话的路由 ≈8 分钟（单次 LLM 53s × 平均 9 窗口），远超 300s 租约。
    租约过期 ⇒ 被其它 worker 抢走重跑 ⇒ 同一批素材排出两套待办卡（`pending_items` 没有幂等约束）。"""
    from app.pipeline import router
    from app.queue import jobs

    seen = {}
    renewed = []
    progressed = []
    monkeypatch.setattr(router, "route_session",
                        lambda session_id, **kw: seen.update(session_id=session_id, **kw))
    monkeypatch.setattr(jobs, "renew_lease", lambda job_id, **kw: renewed.append(job_id))
    monkeypatch.setattr(jobs, "set_progress",
                        lambda job_id, prog: progressed.append((job_id, prog)))

    worker._handle_route({"id": 42, "payload": {"session_id": "s1"}}, SENTINEL)
    seen["on_window"](3, 9)
    assert renewed == [42]
    assert progressed == [(42, {"window": 3, "windows_total": 9})]


def test_the_write_facts_handler_passes_the_payload_straight_through(monkeypatch):
    from app.pipeline import facts

    seen = {}
    monkeypatch.setattr(facts, "write_facts", lambda payload, **kw: seen.update(payload))
    worker._handle_write_facts({"payload": {"session_id": "s1", "items": []}}, SENTINEL)
    assert seen == {"session_id": "s1", "items": []}


def test_the_apply_decision_handler_lets_review_own_its_transactions(monkeypatch):
    """失败标记必须比抛错先落盘，所以 `apply_decision` 也只接 payload、自管事务。"""
    from app.pipeline import review

    calls = []
    monkeypatch.setattr(review, "apply_decision", lambda payload: calls.append(payload))
    worker._handle_apply_decision({"payload": {"pending_id": 7}}, SENTINEL)
    assert calls == [{"pending_id": 7}]


def test_the_p5_apply_proposal_kind_is_registered():
    """§5.7 提案采纳/否决的落库 job。漏注册 ⇒ 采纳了却永远写不进 living_doc。"""
    assert "apply_proposal" in worker.HANDLERS


def test_the_apply_proposal_handler_owns_its_transactions(monkeypatch):
    """reject 分支要算 embedding（否决记录），所以只接 payload、自管连接，
    绝不把 worker 的连接递下去（SENTINEL 被碰到就是 bug）。"""
    from app.pipeline import proposals

    calls = []
    monkeypatch.setattr(proposals, "apply_proposal", lambda payload: calls.append(payload))
    worker._handle_apply_proposal({"payload": {"proposal_id": 5}}, SENTINEL)
    assert calls == [{"proposal_id": 5}]
