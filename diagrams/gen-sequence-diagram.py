import sys, unicodedata

ACTORS = [
    ("你 · Web UI",              90,   "#ff7b9c"),
    ("Claude Code hooks",        260,  "#4ea1ff"),
    ("FastAPI 核心",             430,  "#4ea1ff"),
    ("PostgreSQL + pgvector",    600,  "#6fcf97"),
    ("jobs worker",              770,  "#f0a35e"),
    ("本地模型 bge-m3/reranker", 940,  "#c792ea"),
    ("LLM 网关 dida-gateway",    1110, "#f0c088"),
]
X = {a[0]: a[1] for a in ACTORS}
W = 1200
PAD = 10

def ch_w(ch, fs):
    return fs * (1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.55)

def text_w(s, fs):
    return sum(ch_w(c, fs) for c in s)

def wrap(s, maxw, fs):
    lines, cur = [], ""
    for tok in s.split(" "):
        cand = cur + (" " if cur else "") + tok if cur else tok
        if text_w(cand, fs) <= maxw or not cur:
            cur = cand
        else:
            lines.append(cur); cur = tok
        while text_w(cur, fs) > maxw and len(cur) > 1:      # 硬切超长 CJK 串
            lo, hi = 1, len(cur)
            while lo < hi:
                mid = (lo + hi) // 2
                if text_w(cur[:mid], fs) <= maxw: lo = mid + 1
                else: hi = mid
            cut = max(1, lo - 1)
            lines.append(cur[:cut]); cur = cur[cut:]
    if cur: lines.append(cur)
    return lines

seq = []
def phase(t): seq.append(("phase", t))
def m(f, t, label, note=None): seq.append(("msg", f, t, label, note))
def s(who, label, note=None): seq.append(("self", who, label, note))

phase("阶段 0 · 会话开始（SessionStart hook，同步）")
m("Claude Code hooks", "FastAPI 核心", "GET /bootstrap {cwd, gitBranch}")
m("FastAPI 核心", "PostgreSQL + pgvector", "查 workspace_map / org_match_rules → 定 org + project",
  "纯规则、零 LLM；未命中 → 默认 personal + resolved_by='default'，不阻塞抽取")
m("FastAPI 核心", "PostgreSQL + pgvector",
  "读 living_doc(confirmed/pinned，按 org+project 裁剪) + user 级稳定 memories")
m("FastAPI 核心", "Claude Code hooks", "渲染好的 Markdown（~2500 tok）")
s("Claude Code hooks", "写哑缓存 cache/bootstrap-&lt;org&gt;-&lt;project&gt;.md",
  "只读派生物、永不回写库；服务离线时 hook 直接 cat 它并标注「快照可能过期」")
m("Claude Code hooks", "你 · Web UI", "注入会话上下文 → 规则生效")

phase("阶段 1 · 每轮提问（UserPromptSubmit hook，同步 · p95 &lt; 400ms · 零 LLM 调用）")
m("你 · Web UI", "Claude Code hooks", "输入问题")
m("Claude Code hooks", "FastAPI 核心", "POST /recall {query, org_key, project_key}")
m("FastAPI 核心", "本地模型 bge-m3/reranker", "bge-m3 encode(query)")
m("本地模型 bge-m3/reranker", "FastAPI 核心", "dense 1024 维 + sparse 词权重")
m("FastAPI 核心", "PostgreSQL + pgvector", "三通道并行 top-50 + RRF(k=60) + 加权",
  "SET LOCAL hnsw.iterative_scan=relaxed_ordering（pgvector ≥ 0.8 已确认）；"
  "可见性谓词 user 穿透 / org / org+project")
m("PostgreSQL + pgvector", "FastAPI 核心", "top-K（带 memory_id）",
  "加权 = scope(project 1.0 / org 0.85 / user 0.7) × 时间衰减 × "
  "LEAST(1.6, 1+0.15·ln(1+mention_count))")
m("FastAPI 核心", "PostgreSQL + pgvector", "写 recall_log（评测与调参依据）")
m("FastAPI 核心", "Claude Code hooks", "top-K 记忆（~600 tok）")
m("Claude Code hooks", "你 · Web UI", "注入 additionalContext → 模型据此作答")

phase("阶段 2 · 摄取与抽取（Stop hook 之后，全异步 · 不阻塞对话）")
m("你 · Web UI", "Claude Code hooks", "会话结束（Stop hook）")
m("Claude Code hooks", "FastAPI 核心", "提示可扫描（watcher 亦可自行 tail）")
m("FastAPI 核心", "PostgreSQL + pgvector", "tail JSONL → §3 过滤 → INSERT conversation_log",
  "信噪比 ~14%：丢 tool_result / tool_use；留 user text、assistant text、thinking（两级筛）、compaction summary")
m("FastAPI 核心", "PostgreSQL + pgvector", "入队 jobs(kind='extract')，立即返回",
  "UNIQUE(source, src_uuid) + jobs.dedup_key 双幂等")
m("jobs worker", "PostgreSQL + pgvector", "FOR UPDATE SKIP LOCKED 认领 + 写 lease_until")
m("jobs worker", "LLM 网关 dida-gateway", "extract：窗口 = user + assistant text + thinking")
m("LLM 网关 dida-gateway", "jobs worker", "候选原子事实 / 规范线索")
m("jobs worker", "LLM 网关 dida-gateway", "route：分类 + 选 scope / subject / role（thinking ON）",
  "主判据 = 这条是不是每次开工都要无条件提醒 AI？是 → observation（规矩线索）；否 → atomic_fact（零碎事实）。★ 模型不再输出 section_key。thinking ON：3+3 实测分类准确率 73% vs 关掉 47%，召回两边都是 15/15；写入全异步，24.6s 延迟落在后台")
m("jobs worker", "本地模型 bge-m3/reranker", "栏位匹配：content 向量 vs doc_sections.label + aliases 取 top-1",
  "★ 取代 LLM 自选。实测模型自选三次跑飘（Lombok → dependency/code_style/dependency），栏位一飘，下面那次「同 section 查重」和「否决记忆」就在错分区里比，双双失效。22 行顺序扫描，同步做")
s("jobs worker", "门控（observation 不设门控 / atomic_fact 0.85 / discard 0.95）",
  "★ 规矩线索 100% 落 pending_items 等人裁决，卡片预选 observation。实测判错的样本 confidence 全在 0.93–1.0，0.95 阈值一条也拦不住，门控形同虚设。量扛得住：72 会话仅 24 条规矩信号　【中断点 ②】")
m("jobs worker", "PostgreSQL + pgvector", "observation 落库前两次向量比对",
  "① vs living_doc 同 section → 相似即 NOOP；② vs raw_observations(polarity='negative') → 相似即 discard(rejected_before)。只查库，不读任何外部规则文件")
m("jobs worker", "本地模型 bge-m3/reranker", "reranker 重排相似候选 top-20",
  "reranker 只在写路径；无 GPU 时读路径跑不动（30 候选 1–3s）")
m("jobs worker", "LLM 网关 dida-gateway", "四动作判定（带已有记忆做对比）")
m("LLM 网关 dida-gateway", "jobs worker", "ADD / UPDATE / DELETE / NOOP")
m("jobs worker", "本地模型 bge-m3/reranker", "bge-m3 encode(新记忆正文)")
m("jobs worker", "PostgreSQL + pgvector", "写 memories + memory_events，推进 extraction_cursor　【中断点 ①】",
  "UNIQUE(source_msg, content_hash) 硬幂等，崩溃重放不产生重复；NOOP 累加 mention_count，UPDATE 累加 revision_count")
m("jobs worker", "PostgreSQL + pgvector",
  "未 consumed observations / 该 section 现有内容量 ≥ 阈值 → 入队 summarize",
  "比例触发不是条数触发：新 section 3 条即触发，成熟 section 20 条；另有 UI 手动触发")
m("jobs worker", "LLM 网关 dida-gateway", "summarize：归纳成体系规范")
m("jobs worker", "PostgreSQL + pgvector", "写 living_doc(draft) + doc_proposals　【中断点 ③】",
  "AI 只能改 draft；confirmed/pinned 锁定，任何改动必须走 diff 提案")

phase("阶段 3 · 人在环（异步 · 你裁决 · 结果反哺 prompt）")
m("你 · Web UI", "FastAPI 核心", "打开待办中心 / 待审 diff")
m("FastAPI 核心", "你 · Web UI", "pending_items + doc_proposals 列表")
m("你 · Web UI", "FastAPI 核心", "裁决：批准 / 否决 / 改 scope·role·section")
m("FastAPI 核心", "PostgreSQL + pgvector", "条件 UPDATE ... WHERE status='pending' → 'writing'",
  "状态转移靠条件 UPDATE 保证并发安全，重复点击不会双写")
m("jobs worker", "PostgreSQL + pgvector", "apply_decision → 写库 → status='done'",
  "失败 → 'failed' + last_error，UI 可见可手动重投")
m("FastAPI 核心", "PostgreSQL + pgvector",
  "否决 doc_proposal → 写 raw_observations(polarity='negative')",
  "否决记忆：此后同类 observation 在落库前被比对② 丢弃，冷启动噪声期靠它收敛。可在观察池页查看/撤销")
m("FastAPI 核心", "PostgreSQL + pgvector", "写 route_decisions（标注资产）",
  "作为 few-shot 反哺路由 prompt ⇒ 挂起率随时间下降；同时是 confidence 校准的数据源")
m("你 · Web UI", "FastAPI 核心", "MCP memory_correct：纠错某条记忆")
m("FastAPI 核心", "PostgreSQL + pgvector", "软删 / UPDATE + negative observation + memory_feedback",
  "否决项以 negative polarity 存入，避免汇总器重复犯同一个错")

# ── 两遍布局：先算高度，再绘制 ──
LFS, NFS = 11.0, 10.0
LMAX, NMAX = 430, 520
items = []
y = 112
for it in seq:
    if it[0] == "phase":
        items.append({"k": "phase", "y": y, "t": it[1], "h": 42}); y += 42; continue
    if it[0] == "self":
        _, who, label, note = it
        x = X[who]
        avail = W - PAD - (x + 54)
        ll = wrap(label, min(LMAX, avail), LFS)
        nl = wrap(note, min(NMAX, avail), NFS) if note else []
        h = 22 + 14 * (len(ll) - 1) + (6 + 13 * len(nl) if nl else 0) + 16
        items.append({"k": "self", "y": y, "x": x, "ll": ll, "nl": nl, "h": h}); y += h; continue
    _, f, t, label, note = it
    x1, x2 = X[f], X[t]
    mid = (x1 + x2) / 2
    span = abs(x2 - x1)
    ll = wrap(label, max(span + 130, min(LMAX, span + 260)), LFS)
    nl = wrap(note, max(span + 130, min(NMAX, span + 300)), NFS) if note else []
    h = 20 + 14 * (len(ll) - 1) + (5 + 13 * len(nl) if nl else 0) + 16
    items.append({"k": "msg", "y": y, "x1": x1, "x2": x2, "mid": mid,
                  "ret": x2 < x1, "ll": ll, "nl": nl, "h": h}); y += h

H = y + 26
LIFE_TOP, LIFE_BOT = 54, H - 16

def clamp(cx, w):
    return max(PAD + w / 2, min(W - PAD - w / 2, cx))

out = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" '
       f'font-family="ui-sans-serif,\'Segoe UI\',\'Microsoft YaHei\',sans-serif">',
       '''<defs>
<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#98a1b1"/></marker>
<marker id="arb" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#5f6878"/></marker>
</defs>''',
       f'<rect x="0" y="0" width="{W}" height="{H}" fill="#0f1115"/>']

for name, x, c in ACTORS:
    out.append(f'<line x1="{x}" y1="{LIFE_TOP}" x2="{x}" y2="{LIFE_BOT}" stroke="#2c313c" '
               f'stroke-width="1" stroke-dasharray="3,4"/>')

for r in items:
    y0 = r["y"]
    if r["k"] == "phase":
        out.append(f'<rect x="12" y="{y0}" width="{W-24}" height="26" rx="5" fill="#1b2029" stroke="#2c313c"/>')
        out.append(f'<text x="24" y="{y0+17}" fill="#c9d1de" font-size="12" font-weight="700">{r["t"]}</text>')
    elif r["k"] == "self":
        x = r["x"]
        out.append(f'<path d="M{x},{y0+14} H{x+46} V{y0+30} H{x+6}" fill="none" stroke="#98a1b1" '
                   f'stroke-width="1.2" marker-end="url(#ar)"/>')
        ty = y0 + 8
        for ln in r["ll"]:
            out.append(f'<text x="{x+54}" y="{ty}" fill="#e6e8ec" font-size="11">{ln}</text>'); ty += 14
        if r["nl"]:
            ty += 1
            for ln in r["nl"]:
                out.append(f'<text x="{x+54}" y="{ty}" fill="#7f8899" font-size="10">{ln}</text>'); ty += 13
    else:
        x1, x2, mid = r["x1"], r["x2"], r["mid"]
        ay = y0 + 14
        col, mk = ("#5f6878", "arb") if r["ret"] else ("#98a1b1", "ar")
        dash = ' stroke-dasharray="5,4"' if r["ret"] else ''
        out.append(f'<line x1="{x1}" y1="{ay}" x2="{x2}" y2="{ay}" stroke="{col}" '
                   f'stroke-width="1.3"{dash} marker-end="url(#{mk})"/>')
        lw = max(text_w(l, LFS) for l in r["ll"]) + 12
        cx = clamp(mid, lw)
        out.append(f'<rect x="{cx-lw/2:.0f}" y="{y0-2}" width="{lw:.0f}" height="{14*len(r["ll"])+2}" '
                   f'rx="3" fill="#0f1115" opacity="0.92"/>')
        ty = y0 + 9
        fill = "#b6bdca" if r["ret"] else "#e6e8ec"
        for ln in r["ll"]:
            out.append(f'<text x="{cx:.0f}" y="{ty}" fill="{fill}" font-size="11" '
                       f'text-anchor="middle">{ln}</text>'); ty += 14
        if r["nl"]:
            nw = max(text_w(l, NFS) for l in r["nl"]) + 12
            nx = clamp(mid, nw)
            ny = ay + 6
            out.append(f'<rect x="{nx-nw/2:.0f}" y="{ny-9}" width="{nw:.0f}" '
                       f'height="{13*len(r["nl"])+3}" rx="3" fill="#0f1115" opacity="0.92"/>')
            ty = ny + 1
            for ln in r["nl"]:
                out.append(f'<text x="{nx:.0f}" y="{ty}" fill="#7f8899" font-size="10" '
                           f'text-anchor="middle">{ln}</text>'); ty += 13

for name, x, c in ACTORS:
    bw = 158
    out.append(f'<rect x="{x-bw//2}" y="12" width="{bw}" height="40" rx="7" fill="#1e222b" '
               f'stroke="{c}" stroke-width="1.4"/>')
    out.append(f'<text x="{x}" y="37" fill="#e6e8ec" font-size="11.5" font-weight="600" '
               f'text-anchor="middle">{name}</text>')
out.append('</svg>')
svg = "\n".join(out)

page = f'''<style>
  .sq {{ background:#0f1115; color:#e6e8ec; padding:18px;
        font-family:ui-sans-serif,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  .sq h1 {{ font-size:17px; margin:0 0 2px; }}
  .sq .sub {{ color:#9aa3b2; font-size:12px; margin-bottom:14px; line-height:1.5; }}
  .sq .wrap {{ overflow-x:auto; border:1px solid #2c313c; border-radius:10px; }}
  .sq svg {{ display:block; min-width:1000px; }}
  .sq .lg {{ display:flex; gap:20px; flex-wrap:wrap; margin-top:12px; font-size:11px;
            color:#9aa3b2; align-items:center; }}
  .sq .lg i {{ display:inline-block; }}
  .sq .ln {{ width:26px; border-top:1.4px solid #98a1b1; }}
  .sq .lnd {{ width:26px; border-top:1.4px dashed #5f6878; }}
  .sq .lnc {{ width:12px; height:9px; border:1.4px solid #98a1b1; border-right:none; }}
</style>
<div class="sq">
  <h1>记忆系统 · 数据流时序图（现行方案）</h1>
  <div class="sub">一轮对话从 hook 触发到记忆入库的完整时序。阶段 0–1 是<b>同步段</b>（阻塞用户，零 LLM 调用，p95 &lt; 400ms）；阶段 2–3 是<b>异步段</b>（jobs 队列，LLM 调用都在这里）。【中断点 ①②③】标记的三处全部落表，崩溃后可从此续跑。</div>
  <div class="wrap">{svg}</div>
  <div class="lg">
    <span><i class="ln"></i> 请求 / 调用</span>
    <span><i class="lnd"></i> 返回</span>
    <span><i class="lnc"></i> 内部判定</span>
  </div>
</div>'''

for p in sys.argv[1:]:
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(page)
    print("wrote", p)
print("svg height", H, "rows", len(items))
