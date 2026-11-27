import React, { useState } from 'react'
import { api } from '../api.js'
import { useFetch } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { enumItem } from '../i18n/dict.js'
import { fmtDateTime } from '../utils/format.js'

// 召回调试（plan §12：不可省）。多通道 + RRF + 多个加权因子，不摊开看根本调不动。
// ★ 走 /recall/debug：不写 recall_log（调试不是真注入），但多返回 query_text/keywords/两路名次。
export default function RecallDebug({ partition }) {
  const [query, setQuery] = useState('')
  const [k, setK] = useState(5)
  const [cwd, setCwd] = useState('')
  const [res, setRes] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  const run = async () => {
    if (!query.trim()) return
    setBusy(true); setErr(null)
    try {
      setRes(await api.recallDebug({
        query: query.trim(), k: Number(k),
        org_key: partition.org_key || undefined,
        project_key: partition.project_key || undefined,
        cwd: cwd.trim() || undefined,
      }))
    } catch (e) {
      setErr(e.status === 503 ? `${e.message}（模型还在加载，稍等再试）` : (e.message || String(e)))
      setRes(null)
    } finally { setBusy(false) }
  }

  const items = res?.items || []

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      <div className="card">
        <div className="row">
          <div className="field flex-1">
            <label>查询</label>
            <input className="input w-full" value={query} onChange={(e) => setQuery(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && run()}
                   placeholder="问一句，看它召回什么、为什么" />
          </div>
          <div className="field">
            <label><Term f="k" /></label>
            <input className="input w-60" value={k} onChange={(e) => setK(e.target.value)} />
          </div>
          <div className="field">
            <label><Term f="cwd" />（可选，覆盖 org 判定）</label>
            <input className="input" value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="C:/Users/.../repo" />
          </div>
          <div className="field">
            <label>&nbsp;</label>
            <button className="btn primary" onClick={run} disabled={busy}>{busy ? '召回中…' : '召回'}</button>
          </div>
        </div>
      </div>

      {res && (
        <>
          <div className="card">
            <div className="row">
              <span className="section-label">这次召回发生了什么</span>
              <div className="spacer" />
              <Badge tone={res.over_budget ? 'danger' : 'success'}>
                {res.latency_ms} ms{res.budget_ms ? ` / 预算 ${res.budget_ms}` : ''}
                {res.over_budget ? ' · 超预算' : ''}
              </Badge>
            </div>
            <div className="meta mt-6">
              分区：{res.org_key || '—'}{res.project_key ? ` / ${res.project_key}` : ''}
              {' · '}增广后查询：<span className="diff-new">{res.query_text}</span>
            </div>
            <div className="meta mt-4">
              抽出的关键词：{(res.keywords || []).map((kw) => <Badge key={kw} tone="neutral">{kw}</Badge>)}
              {(res.keywords || []).length === 0 && <span className="muted">（无）</span>}
            </div>
          </div>

          {items.length === 0 && (
            <EmptyState title="没召回任何东西" hint="分区对吗？库里有货吗？" />
          )}

          {items.length > 0 && (
            <div className="tbl-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>#</th><th>id</th><th>正文</th>
                    <th><Term f="scope" />/<Term f="role" /></th>
                    <th className="num"><Term f="final" /></th>
                    <th className="num"><Term f="rrf" /></th>
                    <th className="num"><Term f="dense_rank" /></th>
                    <th className="num"><Term f="keyword_rank" /></th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((h, i) => (
                    <tr key={h.memory_id}>
                      <td>{i + 1}</td>
                      <td className="mono">{h.memory_id}</td>
                      <td className="max-w-360">{h.text}</td>
                      <td>{enumItem('scope', h.scope).zh}/{enumItem('role', h.role).zh}</td>
                      <td className="num text-accent" style={{ fontWeight: 600 }}>{h.score?.toFixed(4)}</td>
                      <td className="num">{h.rrf?.toFixed(4)}</td>
                      <td className="num">{h.dense_rank ?? '—'}{h.channels?.dense ? ` (${h.channels.dense})` : ''}</td>
                      <td className="num">{h.keyword_rank ?? '—'}{h.channels?.keywords ? ` (${h.channels.keywords})` : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="meta mt-6">
            final = rrf 归一 × scope 权重 × 时间衰减 × mention 加成（§6.4）。dense/keyword 名次为 null = 那条通道没召回它。
          </div>
        </>
      )}

      <RecallLog />
    </div>
  )
}

function RecallLog() {
  const { data, loading, reload } = useFetch(() => api.recallLog({ limit: 20 }), [])
  const rows = data || []
  const [open, setOpen] = useState(null)   // 同一时刻只展开一行
  const [marking, setMarking] = useState(null) // 正在标记的行 id（防连点）
  // B6：一键"有用/没用"。落 recall_log.feedback（整轮一个标记，不拆到每条记忆）；
  // useless 的行会被 eval --harvest 优先采进复查池。null = 撤销。
  const mark = async (id, feedback) => {
    setMarking(id)
    try {
      await api.recallLogFeedback(id, feedback)
      await reload()
    } catch (e) {
      alert(`标记失败：${e.message || e}`)
    } finally { setMarking(null) }
  }
  return (
    <div className="card mt-12">
      <div className="row">
        <span className="section-label">最近真实注入（recall_log）</span>
        <div className="spacer" />
        <span className="meta">点击行看命中与注入详情；👍/👎 标记这轮带得对不对（👎 进复查池）</span>
        <button className="btn" onClick={reload} disabled={loading}>{loading ? '加载中…' : '刷新'}</button>
      </div>
      {rows.length === 0 && (
        <EmptyState title="还没有真实召回记录" hint="调试召回不写这里。" />
      )}
      {rows.map((r) => (
        <div key={r.id}>
          {/* ★ hits 是对象数组 [{id,score,channels,text}]（后端透传 jsonb + 现查正文），
              直接渲染会让 React 抛"Objects are not valid as a child"整页白屏——只能取 length。 */}
          <div className="list-row" style={{ cursor: 'pointer' }}
               onClick={() => setOpen(open === r.id ? null : r.id)}>
            <span className="meta mono">{open === r.id ? '▾' : '▸'}</span>
            <span className="meta">#{r.id} · {fmtDateTime(r.created_at)} · {r.latency_ms}ms · {r.org_key}{r.project_key ? `/${r.project_key}` : ''}</span>
            <div className="spacer" />
            {r.feedback === 'useful' && <Badge tone="success">👍 有用</Badge>}
            {r.feedback === 'useless' && <Badge tone="danger">👎 没用 · 进复查池</Badge>}
            <Badge tone="neutral">命中 {r.hits?.length ?? 0} · 注入 {r.injected?.length ?? 0}</Badge>
            {/* 按钮嵌在行里：stopPropagation 防止触发展开；再次点同一个 = 撤销。 */}
            <span onClick={(e) => e.stopPropagation()} style={{ display: 'inline-flex', gap: 4 }}>
              <button className="btn" disabled={marking === r.id}
                      title={r.feedback === 'useful' ? '撤销标记' : '这轮带得对'}
                      onClick={() => mark(r.id, r.feedback === 'useful' ? null : 'useful')}>👍</button>
              <button className="btn" disabled={marking === r.id}
                      title={r.feedback === 'useless' ? '撤销标记' : '这轮带错了（会进复查池）'}
                      onClick={() => mark(r.id, r.feedback === 'useless' ? null : 'useless')}>👎</button>
            </span>
          </div>
          {open === r.id && <RecallLogDetail r={r} />}
        </div>
      ))}
    </div>
  )
}

// 展开详情：查询原文 + 命中明细（分数/通道/正文，标注是否被注入）+ 注入清单（按注入顺序）。
function RecallLogDetail({ r }) {
  const injectedIds = new Set((r.injected || []).map((x) => x.id))
  return (
    <div style={{ padding: '4px 12px 12px 28px' }}>
      <div className="meta mb-4">查询：<span className="diff-new">{r.query_text}</span></div>

      <div className="meta my-4">命中 {(r.hits || []).length} 条（★ = 最终注入）：</div>
      {(r.hits || []).length === 0 && <div className="meta muted">（无命中）</div>}
      {(r.hits || []).map((h, i) => (
        <div key={`${h.id}-${i}`} className="list-row">
          <span className="meta mono">{injectedIds.has(h.id) ? '★' : '　'}#{h.id}</span>
          <Badge tone={injectedIds.has(h.id) ? 'success' : 'neutral'}>
            {typeof h.score === 'number' ? h.score.toFixed(4) : '—'} · {channelText(h.channels)}
          </Badge>
          <div style={{ width: '100%', fontSize: 13, whiteSpace: 'pre-wrap' }}>{h.text}</div>
        </div>
      ))}

      <div className="meta my-4">注入 {(r.injected || []).length} 条（按进入 prompt 的顺序）：</div>
      {(r.injected || []).length === 0 && <div className="meta muted">（无注入）</div>}
      {(r.injected || []).map((x, i) => (
        <div key={`${x.id}-${i}`} className="list-row">
          <span className="meta mono">{i + 1}. #{x.id}</span>
          <div style={{ width: '100%', fontSize: 13, whiteSpace: 'pre-wrap' }}>{x.text}</div>
        </div>
      ))}
    </div>
  )
}

// channels 形如 {dense:1, keywords:3}（值 = 该通道里的名次），摊平成人话。
function channelText(channels) {
  const entries = Object.entries(channels || {})
  if (entries.length === 0) return '无通道'
  const NAME = { dense: '向量', keywords: '关键词' }
  return entries.map(([k, v]) => `${NAME[k] || k}#${v}`).join(' + ')
}
