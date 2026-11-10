import React, { useEffect } from 'react'
import { api } from '../api.js'
import { useFetch } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { enumItem } from '../i18n/dict.js'

// 指标页（plan §10）：一屏看全"记忆系统健康不健康、该调哪个参数"。
// ★ 全只读，数据来自 GET /v1/metrics（纯 SQL 聚合）。三类指标分开标注，
//   别让"待测"被当成"0"：能直接算的给数字，人工抽样的只给候选池，没 runner 的明确标待测。

const pct = (r) => (r === null || r === undefined ? '—' : `${(r * 100).toFixed(1)}%`)

export default function Metrics({ partition }) {
  const { data, error, loading, reload } = useFetch(
    () => api.metrics({ org_key: partition.org_key }),
    [partition.org_key],
  )

  // 队列有活任务（回填中）时每 30s 自动刷新，token 消耗和进度不用手点；安静后停止轮询。
  const live = data
    ? (data.llm_usage?.queue?.pending || 0) + (data.llm_usage?.queue?.running || 0)
    : 0
  useEffect(() => {
    if (!live) return undefined
    const t = setInterval(reload, 30000)
    return () => clearInterval(t)
  }, [live, reload])

  return (
    <div>
      {error && <div className="alert alert-error">{error}</div>}
      <div className="card row">
        <span className="section-label">指标 · §10</span>
        <span className="meta">
          {partition.org_key ? `org=${partition.org_key}` : '全库'} · 纯 SQL 只读聚合
        </span>
        <div className="spacer" />
        <button className="btn" onClick={reload} disabled={loading}>
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      {!data && !error && <EmptyState title={loading ? '加载中…' : '暂无数据'} />}
      {data && (
        <>
          <div className="card-grid mb-12">
            <SuspendCard m={data.suspend_rate} />
            <SectionPendingCard m={data.section_pending_rate} />
            <LatencyCard m={data.latency} />
            <CorrectionCard m={data.corrections} />
            <EmptySectionCard m={data.empty_sections} />
            <RecallEvalCard m={data.recall_eval} />
          </div>
          <OverrideCard m={data.override_rate} />
          <ThroughputCard m={data.review_throughput} />
          <RuleMissCard m={data.rule_miss} />
          <LlmUsageCard m={data.llm_usage} />
        </>
      )}
    </div>
  )
}

const fmtTok = (n) => (n === null || n === undefined ? '—' : Number(n).toLocaleString())

// LLM token 消耗（llm_usage 表，migration 0003）：回填/日常的成本视角。
// ★ 全局指标，不随 org 分区裁剪——一次 route 调用服务整个窗口，拆不到分区头上。
function LlmUsageCard({ m }) {
  if (!m) return null
  const t = m.total || {}
  const q = m.queue || {}
  const live = (q.pending || 0) + (q.running || 0)
  const qTotal = live + (q.done || 0) + (q.failed || 0) + (q.cancelled || 0)
  return (
    <div className="card mb-12">
      <div className="row">
        <span className="section-label">LLM token 消耗{m.today_only ? '（今天）' : '（累计）'}</span>
        <span className="meta">
          {fmtTok(t.calls)} 次调用（失败 {fmtTok(t.failed)}） · in {fmtTok(t.input_tokens)} / out{' '}
          {fmtTok(t.output_tokens)} = <b>{fmtTok(t.total_tokens)}</b> tok · avg {fmtTok(t.avg_ms)}ms
        </span>
        <div className="spacer" />
        {live > 0 && <Badge tone="warn">回填进行中 · 本页每 30s 自动刷新</Badge>}
      </div>
      {qTotal > 0 && <QueueProgress q={q} total={qTotal} />}
      <HourChart series={m.series} />
      {m.by_task && m.by_task.length > 0 && (
        <table className="tbl my-8">
          <thead>
            <tr>
              <th>任务</th><th>次数</th><th>输入 tok</th><th>输出 tok</th><th>合计</th>
              <th>avg ms</th><th>失败</th>
            </tr>
          </thead>
          <tbody>
            {m.by_task.map((r) => (
              <tr key={r.task}>
                <td><code>{r.task}</code></td>
                <td>{fmtTok(r.calls)}</td>
                <td>{fmtTok(r.input_tokens)}</td>
                <td>{fmtTok(r.output_tokens)}</td>
                <td><b>{fmtTok(r.total_tokens)}</b></td>
                <td>{fmtTok(r.avg_ms)}</td>
                <td>{r.failed > 0 ? <Badge tone="danger">{r.failed}</Badge> : '0'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// 队列堆叠进度条：done 占比 = 回填完成率。逐条任务明细去「任务队列」页看。
const Q_COLOR = {
  done: 'var(--q-done)', running: 'var(--q-running)', pending: 'var(--q-pending)',
  failed: 'var(--q-failed)', cancelled: 'var(--q-cancelled)',
}

function QueueProgress({ q, total }) {
  const parts = ['done', 'running', 'pending', 'failed', 'cancelled']
    .map((k) => ({ k, n: q[k] || 0 }))
    .filter((p) => p.n > 0)
  return (
    <div className="my-8">
      <div className="row meta">
        <span>任务队列：<b>{q.done || 0}</b> / {total} 完成</span>
        <div className="spacer" />
        {parts.map((p) => <span key={p.k}>{p.k} {p.n}　</span>)}
      </div>
      <div style={{ display: 'flex', height: 12, borderRadius: 6, overflow: 'hidden', marginTop: 6 }}>
        {parts.map((p) => (
          <div key={p.k} title={`${p.k}: ${p.n}`}
               style={{ width: `${(p.n / total) * 100}%`, background: Q_COLOR[p.k] }} />
        ))}
      </div>
    </div>
  )
}

// 近 24h 逐小时 token 柱状图。手写 SVG，不引图表库（前端一直零第三方 UI 依赖）。
function HourChart({ series }) {
  if (!series || series.length === 0) return null
  const W = 640
  const H = 88
  const gap = 3
  const max = Math.max(...series.map((s) => s.tokens), 1)
  const bw = (W - gap * (series.length - 1)) / series.length
  return (
    <div className="my-8">
      <div className="meta">近 24 小时逐小时 token（峰值 {fmtTok(max)}，悬停看明细）</div>
      <svg viewBox={`0 0 ${W} ${H + 16}`} style={{ width: '100%', maxWidth: W, display: 'block' }}>
        {series.map((s, i) => {
          const h = Math.max(Math.round((s.tokens / max) * (H - 14)), s.tokens > 0 ? 2 : 0)
          return (
            <rect key={s.hour} x={i * (bw + gap)} y={H - h} width={bw} height={h} rx="2"
                  fill="var(--accent)" opacity="0.75">
              <title>{`${s.hour} · ${fmtTok(s.tokens)} tok · ${s.calls} 次调用`}</title>
            </rect>
          )
        })}
        <text x="0" y={H + 12} fontSize="10" fill="var(--text-3)">{series[0].hour}</text>
        <text x={W} y={H + 12} fontSize="10" fill="var(--text-3)" textAnchor="end">
          {series[series.length - 1].hour}
        </text>
      </svg>
    </div>
  )
}

// 顶部一排小卡：一个大数字 + 一句人话解释 + 出处。
function Stat({ label, value, hint, tone }) {
  const numTone = tone === 'bad' ? 'tone-danger' : tone === 'warn' ? 'tone-warning' : undefined
  return (
    <div className="card">
      <div className="meta">{label}</div>
      <div className={`stat-num my-4 ${numTone || ''}`}>{value}</div>
      <div className="meta">{hint}</div>
    </div>
  )
}

function SuspendCard({ m }) {
  return (
    <Stat label="挂起率" value={pct(m.rate)}
          tone={m.rate > 0.5 ? 'warn' : undefined}
          hint={`${m.pending} 条待办 / ${m.total_routed} 条抽取判定。越高说明系统越拿不准、越打扰人。`} />
  )
}

function SectionPendingCard({ m }) {
  return (
    <Stat label="栏位待裁决率" value={pct(m.rate)}
          tone={m.rate > 0.3 ? 'warn' : undefined}
          hint={`${m.section_pending} 条卡在选栏位。★ 高 = 向量 top-1 分低或和 top-2 太近，直接信号是 aliases 缺词。`} />
  )
}

function LatencyCard({ m }) {
  const val = m.samples === 0 ? '待测' : `${m.p50_ms} / ${m.p95_ms}`
  return (
    <Stat label={`召回延迟 p50/p95（近 ${m.window_days} 天）`} value={val}
          tone={m.p95_ms > 400 ? 'bad' : undefined}
          hint={m.samples === 0 ? '还没有真实召回记录（recall_log 为空）。'
            : `${m.samples} 次召回，单位 ms。p95 超 hook 预算（400ms）会击穿降级。`} />
  )
}

function CorrectionCard({ m }) {
  const kinds = Object.entries(m.by_kind)
  return (
    <div className="card">
      <div className="meta">纠错次数（memory_feedback）</div>
      <div className="stat-num my-4">{m.corrected}</div>
      <div className="meta">
        {kinds.length === 0
          ? '还没有任何人工反馈。'
          : kinds.map(([k, v]) => `${enumItem('feedback_kind', k).zh}:${v}`).join(' · ')}
      </div>
    </div>
  )
}

function EmptySectionCard({ m }) {
  return (
    <Stat label="空栏位" value={`${m.empty_count} / ${m.enabled_total}`}
          tone={m.empty_count === m.enabled_total ? 'warn' : undefined}
          hint={m.empty_count === m.enabled_total
            ? '★ 全空——成文规范一条都没生成。看汇总器有没有跑、观察池有没有料。'
            : `还有 ${m.empty_count} 个 enabled 栏位没有 living_doc。长期空的（如 local_env）说明判据有洞。`} />
  )
}

function RecallEvalCard({ m }) {
  const measured = m.hit_at_5 !== null && m.hit_at_5 !== undefined
  const value = measured
    ? `hit@5 ${(m.hit_at_5 * 100).toFixed(0)}% / MRR ${m.mrr_at_10.toFixed(2)}`
    : m.status
  const hint = m.cases === 0
    ? '★ eval_recall_cases 为空，无法算召回质量。先攒评测用例再跑 runner，别写"暂定 X"。'
    : measured
      ? `${m.cases} 条用例，最近跑批 ${m.run_at}；召回延迟 p95=${m.p95_ms}ms（预算 400ms）。重跑：python -m app.commands.eval --run`
      : `${m.cases} 条用例已就绪，但 hit@5/MRR 要靠召回 runner 跑一遍才有数。`
  return (
    <Stat label="召回评测 hit@5 / MRR@10" value={value}
          tone={measured ? undefined : 'warn'} hint={hint} />
  )
}

// 改判率：必须按 kind 分开看。高置信却被人改判 = 最危险，会静默污染记忆库。
function OverrideCard({ m }) {
  const rows = Object.entries(m.by_kind)
  return (
    <div className="card">
      <div className="row">
        <span className="section-label">改判率（按 <Term f="kind" /> 分开）</span>
        <div className="spacer" />
        <span className="meta">
          阈值：{Object.entries(m.thresholds).map(([k, v]) => `${enumItem('kind', k).zh}≥${v}`).join('，') || '无'}
          （观察线索不设阈值 #51，丢弃不进待办）
        </span>
      </div>
      <div className="meta my-6">
        ★ 最危险的指标：模型高置信却被人工改判，这些会静默污染记忆库。「高置信改判率」只统计 confidence ≥ 该 kind 阈值的判定。
      </div>
      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th><Term f="kind" /></th><th className="num">判定数</th><th className="num">改判数</th>
              <th className="num">改判率</th><th className="num">高置信数</th><th className="num">高置信改判</th>
              <th className="num">高置信改判率</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={7} className="muted">还没有路由判定记录</td></tr>}
            {rows.map(([k, b]) => (
              <tr key={k}>
                <td><Badge enum="kind" value={k} /></td>
                <td className="num">{b.total}</td>
                <td className="num">{b.overridden}</td>
                <td className="num">{pct(b.override_rate)}</td>
                <td className="num">{b.threshold === null ? <span className="muted">无阈值</span> : b.high_conf}</td>
                <td className="num">{b.threshold === null ? <span className="muted">—</span> : b.high_conf_overridden}</td>
                <td className="num">
                  {b.threshold === null
                    ? <span className="muted">—</span>
                    : (
                      <span className={`stat-num ${b.high_conf_override_rate > 0 ? 'tone-danger' : ''}`}
                            style={{ fontSize: 13, fontWeight: b.high_conf_override_rate > 0 ? 700 : 400 }}>
                        {pct(b.high_conf_override_rate)}
                      </span>
                    )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// 规矩类人审吞吐：规矩类 100% 进人审，积压涨说明"全审"扛不住了。
function ThroughputCard({ m }) {
  return (
    <div className="card">
      <div className="row">
        <span className="section-label">规矩类人审吞吐（近 {m.window_days} 天）</span>
        <div className="spacer" />
        {m.sustained_growth
          ? <Badge tone="danger">积压增长中</Badge>
          : <Badge tone="success">跟得上</Badge>}
      </div>
      <div className="meta my-6">
        ★ 规矩类（模型判定=观察线索）100% 进人审。积压持续增长说明"全审"扛不住——
        该回头给 prompt 加 few-shot 提准确率，<b>不是</b>把阈值加回来。
      </div>
      <div className="row gap-20">
        <MiniStat label="当前积压" value={m.backlog} />
        <MiniStat label="日均新增" value={m.daily_new} />
        <MiniStat label="日均处理" value={m.daily_resolved} />
        <MiniStat label="净增长" value={m.net_backlog_growth}
                  tone={m.net_backlog_growth > 0 ? 'bad' : 'ok'} />
      </div>
    </div>
  )
}

function MiniStat({ label, value, tone }) {
  const cls = tone === 'bad' ? 'tone-danger' : tone === 'ok' ? 'tone-success' : undefined
  return (
    <div>
      <div className="meta">{label}</div>
      <div className={`stat-num fs-20 ${cls || ''}`}>{value ?? '—'}</div>
    </div>
  )
}

// 规矩漏判率：比率算不出来（要人工看），只给候选池 + 样本。
function RuleMissCard({ m }) {
  return (
    <div className="card">
      <div className="row">
        <span className="section-label">规矩漏判率 · 候选池（人工抽样）</span>
        <div className="spacer" />
        <Badge tone="warning">{m.candidate_count} 条待抽</Badge>
      </div>
      <div className="meta my-6">
        ★ 唯一能发现"静默丢失的规矩"的手段。下面这些生效中的记忆正文含规范性措辞
        （{m.words.join(' / ')}），本该被路由成观察线索走人审；若被误判成零碎事实
        就只靠相似度召回、<b>永不注入</b>。比率靠人工看每条到底是不是规矩，系统算不出来。
      </div>
      {m.sample.length === 0
        ? <EmptyState title="没有命中规范性措辞的生效记忆" />
        : m.sample.map((s) => (
            <div key={s.id} className="list-row">
              <span className="meta mono">#{s.id}</span>
              <Badge enum="scope" value={s.scope} />
              <Badge enum="subject" value={s.subject} />
              <Badge enum="role" value={s.role} />
              <div style={{ width: '100%', fontSize: 13, marginTop: 4, whiteSpace: 'pre-wrap' }}>{s.content}</div>
            </div>
          ))}
    </div>
  )
}
