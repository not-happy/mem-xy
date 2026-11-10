import React, { useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import DiffPane from '../components/DiffPane.jsx'
import EmptyState from '../components/EmptyState.jsx'
import KvList from '../components/KvList.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { enumItem } from '../i18n/dict.js'

// 重跑页（§11）：审阅 shadow 重跑产出的 diff（replay_runs / replay_actions）+ 逐条/批量 apply。
// ★ 生产这些表的 shadow 重跑（拿新 prompt_ver 重抽 conversation_log 再和现有记忆 diff）是 **P7**，
//   还没做——所以这一页平时是空的。它只负责「审阅 + 落地」那半（§8 中断点 6：applied 逐条可续）。
// ★ apply 会真改记忆库（ADD 建、UPDATE 改正文、DELETE 软删、NOOP 跳过），ADD/UPDATE 要算 embedding，
//   模型没热后端回 503。actor='replay'，memory_events 留轨迹。

export default function Replay() {
  const { data, error, loading, reload } = useFetch(() => api.getReplayRuns(), [])
  const [selected, setSelected] = useState(null)

  return (
    <div>
      {error && <div className="alert alert-error">{error}</div>}
      <div className="card row">
        <span className="section-label">重跑（replay_runs）</span>
        <span className="meta">shadow 重跑 diff 审阅 + 批量落地</span>
        <div className="spacer" />
        <button className="btn" onClick={reload} disabled={loading}>{loading ? '加载中…' : '刷新'}</button>
      </div>

      <div className="alert alert-info">
        ★ <b>生产方还没做</b>：往这两张表里写 diff 的 shadow 重跑是 <b>P7</b>，所以这里平时是空的。
        这一页只负责<b>审阅 + 落地</b>——apply 会真改记忆库（建/改正文/软删），逐条可续（§8）。
      </div>

      {(data?.runs || []).length === 0 && !loading && (
        <EmptyState title="还没有重跑 run" hint="等 P7 的 shadow 重跑产出，或手动 seed 一个 run 来验这一页。" />
      )}

      {(data?.runs || []).map((r) => (
        <RunCard key={r.id} run={r} selected={selected === r.id}
                 onToggle={() => setSelected((s) => (s === r.id ? null : r.id))} />
      ))}

      {selected !== null && <RunDetail runId={selected} onChanged={reload} onCollapse={() => setSelected(null)} />}
    </div>
  )
}

function RunCard({ run, selected, onToggle }) {
  const c = run.action_counts || {}
  const by = c.by_action || {}
  return (
    <div className={`card ${selected ? 'is-active' : ''}`}>
      <div className="row pointer" onClick={onToggle}>
        <span className="section-label">run #{run.id}</span>
        <Badge enum="replay_run_status" value={run.status} />
        <Badge tone="neutral" title={run.prompt_ver}>提示词 {run.prompt_ver}</Badge>
        <div className="spacer" />
        <span className="meta">
          动作 {c.total ?? 0}（已落地 {c.applied ?? 0} / 待 {c.pending ?? 0}）
          {' · '}新增 {by.ADD ?? 0} / 修改 {by.UPDATE ?? 0} / 删除 {by.DELETE ?? 0} / 无变化 {by.NOOP ?? 0}
        </span>
        <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>{selected ? '▲' : '▼'}</span>
      </div>
      <div className="meta mt-6">
        创建 {(run.created_at || '').replace('T', ' ')}
        {run.finished_at ? ` · 完成 ${run.finished_at.replace('T', ' ')}` : ''}
        {run.scope_filter ? <> · 范围过滤 <KvList value={run.scope_filter} /></> : ''}
      </div>
    </div>
  )
}

function RunDetail({ runId, onChanged, onCollapse }) {
  const { data, error, loading, reload } = useFetch(() => api.getReplayRun(runId), [runId])
  const action = useAction(() => { reload(); onChanged() })
  const { confirm, dialog } = useConfirm()
  const toast = useToast()
  const err = error || action.error
  const actions = data?.actions || []
  const pending = actions.filter((a) => !a.applied)
  const discardable = data && !['applied', 'discarded'].includes(data.status)

  // 批量落地是真改记忆库——确认框把动作构成说清楚。
  const applyAll = async () => {
    const by = {}
    for (const a of pending) by[a.action] = (by[a.action] || 0) + 1
    const summary = Object.entries(by).map(([k, v]) => `${enumItem('memory_action', k).zh} ${v}`).join('、')
    const sure = await confirm({
      title: `落地 run #${runId} 的全部 ${pending.length} 个待处理动作？`,
      body: `构成：${summary}。会真改记忆库（建/改正文/软删），逐条可续、失败停在那条。`,
      okText: '全部落地',
    })
    if (!sure) return
    const ok = await action.run(() => api.applyReplayRun(runId))
    if (ok) toast.success(`run #${runId} 待处理动作已落地`)
  }

  const discard = async () => {
    const sure = await confirm({
      title: `丢弃 run #${runId}？`,
      body: '丢弃后不能再 apply，这个 run 的 diff 只留档不生效。',
      danger: true,
      okText: '丢弃',
    })
    if (!sure) return
    const ok = await action.run(() => api.discardReplayRun(runId))
    if (ok) { toast.success(`已丢弃 run #${runId}`); onCollapse(); onChanged() }
  }

  return (
    <div className="card">
      {dialog}
      <div className="row">
        <span className="section-label">run #{runId} 的动作</span>
        <div className="spacer" />
        {err && <span className="alert alert-error" style={{ marginRight: 8, padding: '2px 8px' }}>{err}</span>}
        <button className="btn primary" disabled={action.busy || pending.length === 0} onClick={applyAll}>
          落地全部待处理（{pending.length}）
        </button>
        {discardable && (
          <button className="btn danger" disabled={action.busy} onClick={discard}>丢弃 run</button>
        )}
        <button className="btn" onClick={() => { reload(); onChanged() }} disabled={loading}>{loading ? '…' : '刷新'}</button>
      </div>

      {actions.length === 0 && !loading && <EmptyState title="这个 run 没有动作" />}

      {actions.map((a) => (
        <ActionRow key={a.id} a={a} runId={runId} action={action} confirm={confirm} toast={toast} />
      ))}
    </div>
  )
}

function ActionRow({ a, runId, action, confirm, toast }) {
  // 单条落地也真改记忆库——同样要确认。
  const applyOne = async () => {
    const sure = await confirm({
      title: `落地这条 ${enumItem('memory_action', a.action).zh}（#${a.id}）？`,
      body: a.target_memory_id != null
        ? `目标记忆 #${a.target_memory_id}，会按右侧提议改动。`
        : '会按提议新建一条记忆。',
      okText: '落地',
    })
    if (!sure) return
    const ok = await action.run(() => api.applyReplayRun(runId, [a.id]))
    if (ok) toast.success(`#${a.id} 已落地`)
  }

  return (
    <div className="list-row">
      <div className="row">
        <Badge enum="memory_action" value={a.action} />
        <span className="muted fs-sm">#{a.id}</span>
        {a.target_memory_id != null && (
          <span className="meta">目标记忆 #{a.target_memory_id}
            {a.target_exists ? '' : <Badge tone="danger">已不存在</Badge>}
            {a.target_status && a.target_status !== 'active' && (
              <Badge enum="memory_status" value={a.target_status} />)}
          </span>
        )}
        <div className="spacer" />
        {a.applied
          ? <Badge tone="success">已落地</Badge>
          : <button className="btn sm green" disabled={action.busy} onClick={applyOne}>落地这条</button>}
      </div>
      <Diff a={a} />
      {a.proposed_meta && Object.keys(a.proposed_meta).length > 0 && (
        <div className="meta mt-4">meta：<KvList value={a.proposed_meta} /></div>
      )}
    </div>
  )
}

function Diff({ a }) {
  // old → new 的语义按动作类型摊开；NOOP 不显示 diff
  if (a.action === 'NOOP') return <div className="meta mt-4">无变化（NOOP）</div>
  const oldText = a.action === 'ADD' ? null : a.target_content
  const newText = a.action === 'DELETE' ? null : a.proposed_content
  return (
    <div style={{ width: '100%', marginTop: 6 }}>
      <DiffPane
        oldTitle={a.action === 'ADD' ? '（新建，无原文）' : '当前'}
        newTitle={a.action === 'DELETE' ? '（软删，正文保留在 memory_events）' : '提议'}
        oldText={oldText}
        newText={newText}
      />
    </div>
  )
}
