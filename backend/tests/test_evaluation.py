"""app/evaluation.py 的验收：指标纯函数 + 候选文件行协议 + 导入幂等 + runner 端到端。

纯函数与文件协议不需要库；import/run_eval gated on 测试库（conftest 隔离，#103）。
★ **不加载 bge-m3**：embedding 打桩成"哈希词袋"（test_recall.py 同款做法），
确定性、毫秒级——这里验的是评测管道本身算得对，不是召回质量。
"""

import hashlib

import pytest

from app import evaluation
from app.retrieve import recall as recall_mod

# ─────────────────── 指标纯函数（手算用例） ───────────────────


class TestHitAtK:
    def test_hit_inside_k(self):
        assert evaluation.hit_at_k([7], [1, 3, 7, 9], k=5)

    def test_hit_outside_k_is_miss(self):
        assert not evaluation.hit_at_k([7], [1, 3, 5, 9, 11, 7], k=5)

    def test_any_expected_counts(self):
        assert evaluation.hit_at_k([7, 8], [1, 8], k=5)

    def test_empty_expect_never_hits(self):
        assert not evaluation.hit_at_k([], [1, 2], k=5)


class TestReciprocalRank:
    def test_first_place(self):
        assert evaluation.reciprocal_rank([5], [5, 1, 2]) == 1.0

    def test_third_place(self):
        assert evaluation.reciprocal_rank([5], [1, 2, 5]) == pytest.approx(1 / 3)

    def test_first_hit_wins(self):
        # 9 在第 2 名、5 在第 4 名——取名次最靠前的命中
        assert evaluation.reciprocal_rank([5, 9], [1, 9, 3, 5]) == 0.5

    def test_beyond_cap_is_zero(self):
        ranked = list(range(1, 12)) + [99]
        assert evaluation.reciprocal_rank([99], ranked, cap=10) == 0.0


class TestLeaked:
    def test_leak(self):
        assert evaluation.leaked([3], [1, 3, 5], k=5)

    def test_no_leak(self):
        assert not evaluation.leaked([3], [1, 5, 7], k=5)

    def test_leak_outside_k_ignored(self):
        # 口径与 hit@k 对齐：只看 top-k
        assert not evaluation.leaked([3], [1, 5, 7, 9, 11, 3], k=5)


class TestPercentile:
    def test_empty_is_none(self):
        # 待测就是待测，不编 0（§10 方法论）
        assert evaluation.percentile([], 50) is None

    def test_p50_of_four(self):
        assert evaluation.percentile([10, 30, 20, 40], 50) == 20

    def test_p95_nearest_rank(self):
        values = list(range(1, 101))          # 1..100
        assert evaluation.percentile(values, 95) == 95

    def test_single_value(self):
        assert evaluation.percentile([42], 95) == 42


# ─────────────────── 候选文件行协议 ───────────────────

_ROUND_TRIP = [
    evaluation.CaseDraft(query="改签验价要先调哪个接口", expect=[113], org="dida",
                         project=None, label="记忆#113 换说法", note="原记忆 #113：..."),
    evaluation.CaseDraft(query="这个说法在别的项目也适用吗", must_not=[64],
                         org="personal", project="personal/mem-xy", label="跨org串台"),
    evaluation.CaseDraft(query="同时有正反两面的用例", expect=[1], must_not=[2]),
]


class TestCandidateFile:
    def test_round_trip(self):
        text = evaluation.render_candidate_file(_ROUND_TRIP, ["# 头"])
        drafts, errors = evaluation.parse_candidate_file(text)
        assert errors == []
        assert len(drafts) == 3
        first = drafts[0]
        assert first.query == "改签验价要先调哪个接口"
        assert first.expect == [113] and first.must_not == []
        assert first.org == "dida" and first.project is None
        assert first.label == "记忆#113 换说法"
        assert first.note == "原记忆 #113：..."
        assert drafts[1].must_not == [64] and drafts[1].expect == []
        assert drafts[2].expect == [1] and drafts[2].must_not == [2]

    def test_query_containing_colon_survives(self):
        text = "## case\n- query: 规矩是啥：先 RetrievePnr 还是先验价\n- expect: 5\n"
        drafts, errors = evaluation.parse_candidate_file(text)
        assert errors == []
        assert drafts[0].query == "规矩是啥：先 RetrievePnr 还是先验价"

    def test_bad_blocks_reported_good_blocks_kept(self):
        text = (
            "## case\n- query: \n- expect: 1\n"          # 缺 query
            "## case\n- query: 好块\n- expect: 9\n"
            "## case\n- query: 坏 id\n- expect: abc\n"   # id 解析失败
            "## case\n- query: 没有判定意义\n"            # expect/must_not 全空
        )
        drafts, errors = evaluation.parse_candidate_file(text)
        assert [d.query for d in drafts] == ["好块"]
        assert len(errors) == 3
        assert all("第" in e for e in errors)   # 每条错误带块号

    def test_multiple_ids(self):
        text = "## case\n- query: q\n- expect: 3, 7,11\n- must_not: 2\n"
        drafts, _ = evaluation.parse_candidate_file(text)
        assert drafts[0].expect == [3, 7, 11] and drafts[0].must_not == [2]


# ─────────────────── 需要测试库的部分 ───────────────────

DIM = 1024
ORG = "pytest-eval-org"
SESSION = "pytest-eval"


class FakeEmbedder:
    """词袋哈希：字面重叠越多余弦越高（test_recall.py 同款）。"""

    def encode(self, texts, *a, **k):
        return [_hash_vector(t) for t in texts]

    def encode_one(self, text):
        return _hash_vector(text)


def _hash_vector(text: str) -> list[float]:
    vector = [0.0] * DIM
    for token in text.replace("，", " ").replace("、", " ").split():
        slot = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM
        vector[slot] += 1.0
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


@pytest.fixture
def fake_model(monkeypatch):
    from app.core import embedding as embedding_mod
    monkeypatch.setattr(recall_mod, "get_embedding_service", lambda: FakeEmbedder())
    # run_eval 预热用的是函数内 import，得 patch 源模块
    monkeypatch.setattr(embedding_mod, "get_embedding_service", lambda: FakeEmbedder())


@pytest.fixture
def clean_tables():
    from app.core import db
    orgs = (ORG, "pytest-eval-other")
    db.execute("DELETE FROM eval_runs")
    db.execute("DELETE FROM eval_recall_cases")
    db.execute("DELETE FROM memory_events WHERE memory_id IN"
               " (SELECT id FROM memories WHERE org_key = ANY(%s))", (list(orgs),))
    db.execute("DELETE FROM memories WHERE org_key = ANY(%s)", (list(orgs),))
    yield
    db.execute("DELETE FROM eval_runs")
    db.execute("DELETE FROM eval_recall_cases")
    db.execute("DELETE FROM memory_events WHERE memory_id IN"
               " (SELECT id FROM memories WHERE org_key = ANY(%s))", (list(orgs),))
    db.execute("DELETE FROM memories WHERE org_key = ANY(%s)", (list(orgs),))


def _insert_memory(content: str, keywords: list[str], org: str = ORG) -> int:
    from app.core import db
    return db.query_one(
        "INSERT INTO memories (scope, org_key, subject, role, content, content_hash,"
        " keywords, status, embedding, prompt_ver)"
        " VALUES ('org', %s, 'project', 'fact', %s, %s, %s, 'active', %s, 'test')"
        " RETURNING id",
        (org, content, hashlib.sha256(content.encode()).hexdigest(),
         keywords, _hash_vector(content)))["id"]


class TestImportAndRun:
    """导入幂等 + runner 端到端。全部跑在测试库的专用分区 ORG 里。"""

    @pytest.fixture(autouse=True)
    def _gate(self, request):
        import conftest
        if not conftest.DB_AVAILABLE:
            pytest.skip("no database")

    def test_import_is_idempotent(self, clean_tables):
        drafts = [evaluation.CaseDraft(query="导入幂等用例", expect=[1], org=ORG)]
        first = evaluation.import_cases(drafts)
        second = evaluation.import_cases(drafts)
        assert first == {"inserted": 1, "skipped": 0}
        assert second == {"inserted": 0, "skipped": 1}
        from app.core import db
        assert db.query_one("SELECT count(*) n FROM eval_recall_cases")["n"] == 1

    def test_run_eval_computes_and_persists(self, clean_tables, fake_model):
        from app.core import db
        mem_a = _insert_memory("pytest-eval 改签验价必须先 RetrievePnr", ["改签", "验价"])
        mem_b = _insert_memory("pytest-eval 婴儿票要人工出票", ["婴儿票"])
        # 另一个 org 下与 query 词面高度重叠的记忆——org 过滤器健康时不可见；
        # 过滤器被改坏时它必然冲进 top-k，否定例变红（§6.1 串台的防御形式）。
        mem_c = _insert_memory("pytest-eval-other 改签验价必须先 RetrievePnr",
                               ["改签"], org="pytest-eval-other")
        evaluation.import_cases([
            # 词面高度重叠 → 桩向量必命中第 1 名
            evaluation.CaseDraft(query="pytest-eval 改签验价必须先 RetrievePnr",
                                 expect=[mem_a], org=ORG),
            # org 上下文不匹配 → mem_b 不可见 → 必 miss。
            # ★ 不能用"不相干 query"造 miss：dense 通道是无阈值的 top-N，
            #   只要分区里有记忆就会排满 top-k（真实行为，评测正是要量它）。
            evaluation.CaseDraft(query="pytest-eval 婴儿票要人工出票",
                                 expect=[mem_b], org="pytest-eval-other"),
            # 否定例：ORG 上下文里绝不该带出别的 org 的 mem_c
            evaluation.CaseDraft(query="pytest-eval 改签验价必须先 RetrievePnr",
                                 must_not=[mem_c], org=ORG),
        ])
        summary = evaluation.run_eval(k=5)

        assert summary.total == 3
        assert summary.positives == 2 and summary.negatives == 1
        assert summary.hit_at_5 == pytest.approx(0.5)      # 一中一脱
        assert summary.mrr_at_10 == pytest.approx(0.5)     # 命中那条排第 1 → RR=1，均值 0.5
        assert summary.leak_count == 0 and summary.leak_rate == 0.0
        assert summary.p50_ms is not None and summary.p95_ms is not None
        assert len(summary.missed) == 1
        assert summary.missed[0]["expected"] == [mem_b]

        run = evaluation.latest_run()
        assert run["id"] == summary.run_id
        assert float(run["hit_at_5"]) == pytest.approx(0.5)
        assert db.query_one("SELECT count(*) n FROM recall_log"
                            " WHERE session_id='eval-runner'")["n"] == 0   # 不污染审计

    def test_run_eval_on_empty_table_raises(self, clean_tables):
        with pytest.raises(RuntimeError, match="为空"):
            evaluation.run_eval()


# ─────────────────── B1：content_hash 判定（id 钉死的替代） ───────────────────


class TestHashPureFunctions:
    """hash 判定纯函数手算用例（不碰库）。"""

    def test_hashes_hit_prefix_match(self):
        # expected 是 12 位前缀，ranked 里是完整 64 位 hash——前缀命中
        assert evaluation.hashes_hit(["b2c7f64891a2"],
                                     ["", "b2c7f64891a2deadbeef", "4867b0"], k=5)

    def test_hashes_hit_outside_k_ignored(self):
        ranked = [""] * 5 + ["b2c7f64891a2deadbeef"]
        assert not evaluation.hashes_hit(["b2c7f64891a2"], ranked, k=5)

    def test_first_hit_rank_prefers_earliest_across_dimensions(self):
        # id 在第 2 名命中，hash 没命中 → 取 2
        assert evaluation.first_hit_rank([5], [1, 5, 9],
                                         expected_hashes=["aaaaaaaaaaaa"],
                                         ranked_hashes=["", "", "b2c7f64891a2"]) == 2
        # hash 在第 2 名命中（expected 全为假记忆 id，hash 维命中才算）
        # ★ ranked 与 ranked_hashes 必须同长——hash 维是"第 rank 名的记忆 hash"
        assert evaluation.first_hit_rank([999], [101, 102, 103],
                                         expected_hashes=["b2c7f64891a2"],
                                         ranked_hashes=["", "b2c7f64891a2fff", ""]) == 2
        # 双维都命中时取最先的那次
        assert evaluation.first_hit_rank([7], [7, 1],
                                         expected_hashes=["4867b0112233"],
                                         ranked_hashes=["4867b0112233aa", ""]) == 1

    def test_first_hit_rank_miss_is_zero(self):
        assert evaluation.first_hit_rank([5], [1, 2, 3]) == 0
        assert evaluation.first_hit_rank([], [],
                                         expected_hashes=["b2c7f64891a2"],
                                         ranked_hashes=["", "4867b0"]) == 0

    def test_first_hit_rank_beyond_cap(self):
        ranked = list(range(1, 12))
        hashes = [""] * 11 + ["b2c7f64891a2"]
        assert evaluation.first_hit_rank([99], ranked + [99],
                                         expected_hashes=["b2c7f64891a2"],
                                         ranked_hashes=hashes + ["b2c7f64891a2"]) == 0

    def test_hashes_leaked(self):
        assert evaluation.hashes_leaked(["b2c7f64891a2"],
                                        ["x", "b2c7f64891a2ab"], k=5)


class TestHashFileProtocol:
    """hash 行的 render/parse round-trip 与坏块。"""

    def test_hash_round_trip(self):
        drafts = [
            evaluation.CaseDraft(query="hash 正例", expect_hashes=["b2c7f64891a2"], org=ORG),
            evaluation.CaseDraft(query="hash 否定例", must_not_hashes=["4867b0112233"]),
            evaluation.CaseDraft(query="id+hash 双标", expect=[1], expect_hashes=["cccc00001111"]),
        ]
        text = evaluation.render_candidate_file(drafts, ["# 头"])
        parsed, errors = evaluation.parse_candidate_file(text)
        assert errors == []
        assert parsed[0].expect_hashes == ["b2c7f64891a2"] and parsed[0].expect == []
        assert parsed[1].must_not_hashes == ["4867b0112233"]
        assert parsed[2].expect == [1] and parsed[2].expect_hashes == ["cccc00001111"]

    def test_short_hash_rejected(self):
        text = "## case\n- query: q\n- expect_hash: b2c7f6\n"   # <12 位
        drafts, errors = evaluation.parse_candidate_file(text)
        assert drafts == []
        assert len(errors) == 1 and "12" in errors[0]

    def test_all_empty_still_rejected_even_with_hash_check(self):
        text = "## case\n- query: q\n"   # expect/must_not/hash 全空
        drafts, errors = evaluation.parse_candidate_file(text)
        assert drafts == [] and len(errors) == 1


class TestHashJudgment:
    """hash 判定的库链路：import 判重 / run_eval 命中 / stale 检出 / config 快照。"""

    @pytest.fixture(autouse=True)
    def _gate(self, request):
        import conftest
        if not conftest.DB_AVAILABLE:
            pytest.skip("no database")

    def test_import_dedup_includes_hashes(self, clean_tables):
        base = [evaluation.CaseDraft(query="hash 判重用例",
                                     expect_hashes=["b2c7f64891a2"], org=ORG)]
        assert evaluation.import_cases(base) == {"inserted": 1, "skipped": 0}
        assert evaluation.import_cases(base) == {"inserted": 0, "skipped": 1}
        # hash 变了 = 新用例（内容指纹不同，判的就不是同一条记忆）
        changed = [evaluation.CaseDraft(query="hash 判重用例",
                                        expect_hashes=["4867b0112233"], org=ORG)]
        assert evaluation.import_cases(changed) == {"inserted": 1, "skipped": 0}
        from app.core import db
        assert db.query_one("SELECT count(*) n FROM eval_recall_cases")["n"] == 2

    def test_run_eval_hash_hit_and_stale(self, clean_tables, fake_model):
        from app.core import db
        content = "pytest-eval-hash 改签验价必须先 RetrievePnr"
        _insert_memory(content, ["改签"])
        hash12 = hashlib.sha256(content.encode()).hexdigest()[:12]
        evaluation.import_cases([
            evaluation.CaseDraft(query="pytest-eval-hash 改签验价必须先 RetrievePnr",
                                 expect_hashes=[hash12], org=ORG),
        ])
        summary = evaluation.run_eval(k=5)
        assert summary.positives == 1
        assert summary.hit_at_5 == 1.0 and summary.mrr_at_10 == 1.0
        assert summary.missed == []
        # 内容还在（哪怕记忆被合并改写）→ hash 命中 → 不 stale
        assert evaluation.stale_cases() == []

        run = evaluation.latest_run()
        cfg = run["config"]
        for key in ("k", "pool", "rrf_k", "scope_weights", "half_life_days",
                    "mention_cap", "mention_coef", "cases", "positives", "negatives"):
            assert key in cfg, key

        # 记忆软删 → 内容没了 → expect_hash 失效（红）；id 失效同样红
        db.execute("UPDATE memories SET status='deleted' WHERE org_key=%s", (ORG,))
        stale = evaluation.stale_cases()
        assert len(stale) == 1
        assert stale[0]["expected_hashes"] == [hash12]


class TestHarvest:
    """B6：--harvest 的复查池语义——用户标过 'useless' 的行优先全采。"""

    @pytest.fixture(autouse=True)
    def _gate(self, request):
        import conftest
        if not conftest.DB_AVAILABLE:
            pytest.skip("no database")

    SESSION = "pytest-eval-harvest"

    def _seed_log(self, query: str, feedback: str | None, session: str | None = None):
        from app.core import db
        return db.query_one(
            "INSERT INTO recall_log (session_id, query_text, hits, injected_ids, feedback)"
            " VALUES (%s, %s, '[]'::jsonb, '{}', %s) RETURNING id",
            (session or self.SESSION, query, feedback))["id"]

    def test_useless_rows_go_first_and_normal_rows_fill_the_rest(self, tmp_path):
        from app.commands import eval as eval_cmd
        from app.core import db
        u1 = self._seed_log("pytest-eval 负标记的一号", "useless")
        self._seed_log("pytest-eval 负标记的一号", None)      # 同 query 的普通行，应被负标记挤掉
        self._seed_log("pytest-eval 普通采样二号", None)
        self._seed_log("pytest-eval 普通采样三号", None)
        # eval-runner 的行不采（runner 自产的 query 不是考题）
        self._seed_log("pytest-eval runner 自产", None, session="eval-runner")
        try:
            out = tmp_path / "candidates.md"
            assert eval_cmd._harvest(2, out) == 0
            text = out.read_text(encoding="utf-8")
            assert "pytest-eval 负标记的一号" in text
            # n=2：负标记占 1 席，剩 1 席从普通行补——且 runner 行绝不出现
            assert "pytest-eval runner 自产" not in text
            blocks = [ln for ln in text.splitlines() if ln.startswith("## ")]
            assert len(blocks) == 2
            pos = {q: i for i, q in enumerate(blocks)}
            assert pos.get("## pytest-eval 负标记的一号", 99) < pos.get(
                "## pytest-eval 普通采样二号", 99) or \
                "pytest-eval 普通采样二号" not in text
        finally:
            db.execute("DELETE FROM recall_log WHERE session_id=%s", (self.SESSION,))
