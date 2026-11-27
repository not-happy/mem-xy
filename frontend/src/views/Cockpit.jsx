import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import Badge from '../components/Badge.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { fmtDateTime } from '../utils/format.js'

/* 驾驶舱：自动化的第一屏。不是「待办中心」的换皮——它回答的是
 * 「机器现在干到哪了、哪里需要我」。三个信息层：
 *   ① 流水线 flow：采集 → 路由 → 双判共识 → 写入 → 召回，节点亮灯 = 有活动；
 *   ② KPI 行：自动化率 / 召回质量 / token 消耗 / 队列积压；
 *   ③ 例外队列 + 系统体征：人需要处理的东西只有这里出现的几行。
 * 数据全部来自既有只读端点（healthz / workers / jobs / pending / proposals / metrics）。
 * 安静时 30s 轮询，队列有活时 10s——与 Jobs/Metrics 页同款策略。 */

const fmtTok = (n) => (n === null || n === undefined ? '—' : Number(n).toLocaleString())
const sumByStatus = (byStatus, pred) => Object.entries(byStatus || {})
  .filter(([k]) => pred(k))
  .reduce((n, [, v]) => n + v, 0)

export default function Cockpit({ onNavigate }) {
  const [snap, setSnap] = useState(null)
  const [error, setError] = useState(null)
  const [updatedAt, setUpdatedAt] = useState(null)

  const load = () => {
    // 六路只读并发；任何一路炸了不拖垮整屏（单独置 null 显示 —）。
    Promise.allSettled([
      api.health(),
      api.listWorkers(),
      api.listJobs({ limit: 1 }),
      api.listPending({ limit: 1 }),
      api.listProposals({ status: 'pending', limit: 1 }),
      api.metrics({}),
    ]).then(([h, w, j, p, pr, m]) => {
      setSnap({
        health: h.status === 'fulfilled' ? h.value : null,
        workers: w.status === 'fulfilled' ? w.value : null,
        jobs: j.status === 'fulfilled' ? j.value?.summary : null,
        pending: p.status === 'fulfilled' ? p.value : null,
        proposals: pr.status === 'fulfilled' ? pr.value : null,
        metrics: m.status === 'fulfilled' ? m.value : null,
      })
      setUpdatedAt(new Date())
      setError(h.status === 'rejected' ? '后端不可达（uvicorn 停了吗？）' : null)
    })
  }

  useEffect(() => { load() }, [])
  // 队列有活（route/回填在跑）就 10s 快轮询，安静后 30s。
  const live = (snap?.jobs?.pending || 0) + (snap?.jobs?.running || 0)
  useEffect(() => {
    const t = setInterval(load, live > 0 ? 10000 : 30000)
    return () => clearInterval(t)
  }, [live])

  const pend = snap?.pending
  const pendingNow = sumByStatus(pend?.by_status, (k) => k.startsWith('pending/'))
  const failedNow = sumByStatus(pend?.by_status, (k) => k.startsWith('failed/'))
  const proposalsNow = snap?.proposals?.total ?? 0
  const jobs = snap?.jobs || {}
  const jobsLive = (jobs.pending || 0) + (jobs.running || 0)
  const workersList = snap?.workers?.workers || []
  const aliveWorkers = workersList.filter((w) => w.alive).length
  const maxWorkers = snap?.workers?.max
  const m = snap?.metrics
  const suspend = m?.suspend_rate?.rate
  const autoRate = typeof suspend === 'number' ? 1 - suspend : null
  const lat = m?.latency
  const evalr = m?.recall_eval
  const usage = m?.llm_usage || {}
  const total24 = (usage.series || []).reduce((n, s) => n + (s.tokens || 0), 0)
  const calls24 = (usage.series || []).reduce((n, s) => n + (s.calls || 0), 0)
  const emb = snap?.health?.embedding || {}

  return (
    <div>
      {error && <div className="alert alert-error">{error}</div>}

      {/* ① 流水线 flow */}
      <div className="flow mb-12">
        <FlowNode
          label="采集 · 消费"
          num={maxWorkers ? `${aliveWorkers}/${maxWorkers}` : '—'}
          sub={aliveWorkers > 0 ? 'worker 在岗消费' : '无 worker 在岗'}
          tone={aliveWorkers > 0 ? 'ok' : 'bad'}
          dot={aliveWorkers > 0 ? 'ok' : 'bad'}
          live={aliveWorkers > 0}
        />
        <FlowLink live={jobsLive > 0} />
        <FlowNode
          label="路由 · 判定"
          num={fmtNum(jobsLive)}
          sub={jobsLive > 0 ? 'LLM 任务在跑' : '队列安静'}
          tone={jobsLive > 0 ? 'accent' : undefined}
          dot={jobsLive > 0 ? '' : 'off'}
          live={jobsLive > 0}
        />
        <FlowLink live={pendingNow > 0} warn />
        <FlowNode
          label="双判 · 共识"
          num={fmtNum(pendingNow)}
          sub={pendingNow > 0 ? '分歧卡等人裁' : '两遍一致，自动落库'}
          tone={pendingNow > 0 ? 'warn' : 'ok'}
          dot={pendingNow > 0 ? 'warn' : ''}
          live={pendingNow > 0}
          onClick={() => onNavigate('pending')}
        />
        <FlowLink live={failedNow + (jobs.failed || 0) > 0} bad />
        <FlowNode
          label="写入 · 落库"
          num={fmtNum(failedNow + (jobs.failed || 0))}
          sub={failedNow + (jobs.failed || 0) > 0 ? '有失败，需处理' : '写入正常'}
          tone={failedNow + (jobs.failed || 0) > 0 ? 'bad' : 'ok'}
          dot={failedNow + (jobs.failed || 0) > 0 ? 'bad' : ''}
          live={failedNow + (jobs.failed || 0) > 0}
          onClick={() => onNavigate('jobs')}
        />
        <FlowLink live={(lat?.p95_ms || 0) > 400} bad />
        <FlowNode
          label="召回 · 注入"
          num={lat && lat.samples > 0 ? `${lat.p95_ms}ms` : '—'}
          sub={lat && lat.samples > 0 ? `p95 · 预算 400ms` : '暂无召回记录'}
          tone={(lat?.p95_ms || 0) > 400 ? 'bad' : 'ok'}
          dot={(lat?.p95_ms || 0) > 400 ? 'bad' : ''}
          live={(lat?.p95_ms || 0) > 400}
          onClick={() => onNavigate('recall')}
        />
      </div>

      {/* ② KPI 行 */}
      <div className="card-grid mb-12">
        <Kpi
          label="自动化率"
          value={autoRate == null ? '—' : `${(autoRate * 100).toFixed(1)}%`}
          tone={autoRate != null && autoRate < 0.9 ? 'warn' : 'accent'}
          hint={m?.suspend_rate
            ? `挂起 ${m.suspend_rate.pending} / 抽取判定 ${m.suspend_rate.total_routed}。1 − 挂起率 = 双判自动落库的占比。`
            : '来自指标页挂起率。'}
        />
        <Kpi
          label="召回质量"
          value={evalr && evalr.hit_at_5 != null ? `hit@5 ${(evalr.hit_at_5 * 100).toFixed(0)}%` : (evalr?.status || '—')}
          hint={evalr && evalr.cases > 0
            ? `${evalr.cases} 条用例 · MRR ${evalr.mrr_at_10?.toFixed?.(2) ?? '—'}`
            : '还没有评测用例。'}
        />
        <Kpi
          label="近 24h token"
          value={fmtTok(total24)}
          hint={`LLM 调用 ${fmtTok(calls24)} 次 · 累计 ${fmtTok(usage.total?.total_tokens)} tok`}
        />
        <Kpi
          label="队列积压"
          value={fmtNum(jobsLive)}
          tone={jobsLive > 0 ? 'warn' : 'ok'}
          hint={`失败 ${jobs.failed || 0} · 取消 ${jobs.cancelled || 0}`}
        />
      </div>

      {/* ③ 例外队列 + 系统体征 */}
      <div className="card-grid">
        <div className="card card-col">
          <div className="row">
            <span className="section-label">例外队列</span>
            <span className="meta">自动化剩下的部分——只有这几行需要人</span>
            <div className="spacer" />
            {updatedAt && <span className="meta" title="下次轮询自动刷新">采样 {fmtDateTime(updatedAt.toISOString())}</span>}
          </div>
          <div className="mt-8">
            <ExRow num={pendingNow} tone={pendingNow > 0 ? 'warn' : 'ok'}
                   title="分歧卡 · 待确认" hint="两遍判定不一致才轮到你，键盘流 j/k/1/2/3"
                   onClick={() => onNavigate('pending')} />
            <ExRow num={failedNow} tone={failedNow > 0 ? 'bad' : 'ok'}
                   title="落库失败" hint="批过了但写库炸了，比分歧卡更急"
                   onClick={() => onNavigate('pending')} />
            <ExRow num={proposalsNow} tone={proposalsNow > 0 ? 'warn' : 'ok'}
                   title="待审 diff · 成文规范" hint="汇总器合成的规范改动，逐段采纳/否决"
                   onClick={() => onNavigate('proposals')} />
            {pendingNow + failedNow + proposalsNow === 0 && (
              <EmptyState title="例外队列清空" hint="系统全自动运转中，无事需人处理。" />
            )}
          </div>
        </div>

        <div className="card card-col">
          <div className="row">
            <span className="section-label">系统体征</span>
            <div className="spacer" />
            {live > 0 && <Badge tone="info">10s 自动刷新</Badge>}
          </div>
          <div className="kv-list mt-8">
            <Kv k="后端" v={snap?.health?.status === 'ok' ? '在线' : (snap?.health?.status || '—')}
                tone={snap?.health?.status === 'ok' ? 'ok' : 'bad'} />
            <Kv k="embedding" v={emb.loaded
              ? `热 · 平均 ${emb.avg_ms_per_row ?? '—'}ms/行`
              : (snap?.health?.status === 'unreachable' ? '不可达' : `加载中（冷启动 ${emb.load_seconds ?? '—'}s）`)}
                tone={emb.loaded ? 'ok' : 'warn'} />
            <Kv k="worker" v={maxWorkers ? `${aliveWorkers}/${maxWorkers} 在岗` : '—'}
                tone={aliveWorkers > 0 ? 'ok' : 'bad'} />
            <Kv k="栏位空置" v={m?.empty_sections ? `${m.empty_sections.empty_count} / ${m.empty_sections.enabled_total}` : '—'}
                tone={m?.empty_sections && m.empty_sections.empty_count === m.empty_sections.enabled_total ? 'warn' : undefined} />
            <Kv k="高置信改判" v={m?.override_rate ? highConfSummary(m.override_rate) : '—'}
                tone={m?.override_rate && hasHighConfOverride(m.override_rate) ? 'bad' : 'ok'} />
          </div>
          {usage.series && usage.series.length > 0 && <TokenSpark series={usage.series} />}
        </div>
      </div>
    </div>
  )
}

const fmtNum = (n) => (n == null ? '—' : Number(n).toLocaleString())

function FlowNode({ label, num, sub, tone, dot, live, onClick }) {
  // 节点状态边框：bad 最优先，其次 warn/live 亮青绿；纯 ok 不亮（安静态）。
  const cls = [
    'flow-node',
    tone === 'bad' ? 'is-bad' : tone === 'warn' || live ? 'is-live' : '',
    onClick ? 'pointer' : '',
  ].filter(Boolean).join(' ')
  const numCls = `flow-num ${tone ? `tone-${tone === 'warn' ? 'warn' : tone === 'bad' ? 'bad' : tone === 'ok' ? 'ok' : 'accent'}` : ''}`
  return (
    <div className={cls} role={onClick ? 'button' : undefined}
         tabIndex={onClick ? 0 : undefined}
         onClick={onClick}
         onKeyDown={onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } } : undefined}>
      <div className="flow-head">
        <span className={`pulse-dot ${dot || ''}`} />
        <span className="flow-label">{label}</span>
      </div>
      <div className={numCls}>{num}</div>
      <div className="flow-sub">{sub}</div>
    </div>
  )
}

function FlowLink({ live, warn, bad }) {
  const color = bad ? 'rgba(248,113,113,.5)' : warn ? 'rgba(251,191,36,.5)' : undefined
  return (
    <span
      className={`flow-link ${live ? 'live' : ''}`}
      style={color ? { background: `repeating-linear-gradient(90deg, ${color} 0 6px, transparent 6px 12px)`, backgroundSize: '12px 2px' } : undefined}
    />
  )
}

function Kpi({ label, value, hint, tone }) {
  const numTone = tone ? `tone-${tone === 'warn' ? 'warning' : tone === 'bad' ? 'danger' : tone === 'ok' ? 'success' : tone}` : ''
  return (
    <div className="card">
      <div className="meta">{label}</div>
      <div className={`stat-num my-4 ${numTone}`}>{value}</div>
      <div className="meta">{hint}</div>
    </div>
  )
}

function ExRow({ num, tone, title, hint, onClick }) {
  const color = tone === 'bad' ? 'tone-danger' : tone === 'warn' ? 'tone-warning' : 'tone-success'
  return (
    <button className="exception-row" onClick={onClick}>
      <span className={`ex-num ${color}`}>{fmtNum(num)}</span>
      <span>{title}</span>
      <div className="spacer" />
      <span className="ex-hint">{hint}</span>
      <span className="text-accent">→</span>
    </button>
  )
}

function Kv({ k, v, tone }) {
  const dotCls = tone === 'ok' ? '' : tone === 'warn' ? 'warn' : tone === 'bad' ? 'bad' : 'off'
  const color = tone === 'bad' ? 'text-danger' : tone === 'warn' ? 'text-warning' : undefined
  return (
    <div className="kv">
      <span className={`pulse-dot ${dotCls}`} />
      <b>{k}</b>
      <span className={color}>{v}</span>
    </div>
  )
}

function highConfSummary(override) {
  const rows = Object.entries(override.by_kind || {})
  if (rows.length === 0) return '无判定记录'
  const danger = rows.filter(([, b]) => (b.high_conf_override_rate || 0) > 0)
  if (danger.length === 0) return '0 条高置信改判'
  return danger.map(([k]) => k).join('/') + ' 有高置信改判'
}

function hasHighConfOverride(override) {
  return Object.values(override.by_kind || {}).some((b) => (b.high_conf_override_rate || 0) > 0)
}

/* 近 24h token 迷你柱图（手写 SVG，与指标页 HourChart 同思路但更紧凑）。 */
function TokenSpark({ series }) {
  const W = 620, H = 46, gap = 2
  const max = Math.max(...series.map((s) => s.tokens), 1)
  const bw = (W - gap * (series.length - 1)) / series.length
  return (
    <div className="mt-12">
      <div className="meta mb-6">近 24h 逐小时 token（悬停看明细）</div>
      <svg viewBox={`0 0 ${W} ${H + 14}`} style={{ width: '100%', maxWidth: W, display: 'block' }}>
        {series.map((s, i) => {
          const h = Math.max(Math.round((s.tokens / max) * (H - 8)), s.tokens > 0 ? 2 : 0)
          return (
            <rect key={s.hour} x={i * (bw + gap)} y={H - h} width={bw} height={h} rx="2"
                  fill="var(--accent)" opacity="0.7">
              <title>{`${s.hour} · ${fmtTok(s.tokens)} tok · ${s.calls} 次调用`}</title>
            </rect>
          )
        })}
        <text x="0" y={H + 12} fontSize="10" fill="var(--text-3)">{series[0].hour}</text>
        <text x={W} y={H + 12} fontSize="10" fill="var(--text-3)" textAnchor="end">{series[series.length - 1].hour}</text>
      </svg>
    </div>
  )
}
