import React from 'react'
import { api } from '../api.js'
import { useFetch } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import { enumItem } from '../i18n/dict.js'

// 待办中心：一屏看全"有多少事等着人裁决"。数字来自两处——
//   pending_items（by_status，"status/stage"→数量）和 doc_proposals（status=pending 的 total）。
// ★ 两处待办统一在这里亮红点（plan §12），点卡片跳到对应页去处理。
export default function Inbox({ partition, onNavigate }) {
  const { data: pend, error: e1, loading: l1, reload: r1 } = useFetch(
    () => api.listPending({ ...partition, limit: 1 }),
    [partition.org_key, partition.project_key],
  )
  const { data: prop, error: e2, loading: l2, reload: r2 } = useFetch(
    () => api.listProposals({ ...partition, status: 'pending', limit: 1 }),
    [partition.org_key, partition.project_key],
  )
  const err = e1 || e2
  const loading = l1 || l2
  const reload = () => { r1(); r2() }

  const byStatus = pend?.by_status || {}
  const sum = (pred) => Object.entries(byStatus)
    .filter(([k]) => pred(k))
    .reduce((n, [, v]) => n + v, 0)
  const pendingNow = sum((k) => k.startsWith('pending/'))
  const failedNow = sum((k) => k.startsWith('failed/'))
  const proposalsNow = prop?.total ?? 0

  const cards = [
    { label: '待确认线索', n: pendingNow, tab: 'pending',
      hint: '系统拿不准的判定，等你选 kind/scope/栏位后批准或否决' },
    { label: '落库失败', n: failedNow, tab: 'pending', danger: failedNow > 0,
      hint: '批过了但写库炸了，比待确认更该先看' },
    { label: '待审 diff', n: proposalsNow, tab: 'proposals',
      hint: '汇总器把观察合成的成文规范改动，逐段采纳/否决/手改' },
  ]

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      <div className="card row">
        <span className="section-label">待办中心</span>
        <div className="spacer" />
        <button className="btn" onClick={reload} disabled={loading}>
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      <div className="card-grid mt-12">
        {cards.map((c) => (
          // button 里不能嵌 div（无效 HTML）——用 div[role=button] + 键盘激活。
          <div
            key={c.label}
            role="button"
            tabIndex={0}
            className={`card card-interactive ${c.danger ? 'is-danger' : (c.n > 0 ? 'is-active' : '')}`}
            onClick={() => onNavigate(c.tab)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNavigate(c.tab) }
            }}
          >
            <div className="row">
              <span className={`stat-num ${c.danger ? 'tone-danger' : (c.n > 0 ? 'tone-accent' : 'tone-muted')}`}>
                {c.n}
              </span>
              {c.n > 0 && <Badge tone={c.danger ? 'danger' : 'warning'}>待处理</Badge>}
              <div className="spacer" />
              <span className="section-label">{c.label}</span>
            </div>
            <div className="meta mt-6">{c.hint}</div>
          </div>
        ))}
      </div>

      <div className="card mt-12">
        <div className="meta">按 status/stage 明细（pending_items）：</div>
        <div className="row mt-6">
          {Object.keys(byStatus).length === 0 && <span className="muted">队列为空</span>}
          {Object.entries(byStatus).map(([k, v]) => <StatusChip key={k} k={k} v={v} />)}
        </div>
      </div>
    </div>
  )
}

// by_status 的键是「status/stage」拼接串，拆开分别查词典；tone 取 status 的语义色。
function StatusChip({ k, v }) {
  const [st, sg] = k.split('/')
  const stItem = enumItem('pending_status', st)
  const sgItem = enumItem('stage', sg)
  return (
    <Badge tone={stItem.tone} title={k}>
      {stItem.zh}/{sgItem.zh}: {v}
    </Badge>
  )
}
