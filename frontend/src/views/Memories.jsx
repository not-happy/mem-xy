import React, { useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import { useDebounced } from '../hooks/useDebounced.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import DiffPane from '../components/DiffPane.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { enumOptions, enumItem } from '../i18n/dict.js'
import { fmtDateTime } from '../utils/format.js'

// 记忆库（plan §13）：按 org/scope/subject/role 浏览、编辑、软删/恢复、看 why 与 memory_events。
// ★ 默认按 §6.1 可见性裁剪；勾「浏览全库」走 all_scopes=true（只读浏览，不注入，所以不裁剪）。
export default function Memories({ partition }) {
  const [qInput, setQInput] = useState('')
  const q = useDebounced(qInput, 300)  // 搜索防抖：每字符 ILIKE 全表扫太浪费
  const [f, setF] = useState({ scope: '', subject: '', role: '', status: 'active' })
  const [allScopes, setAllScopes] = useState(false)
  const { data, error, loading, reload } = useFetch(
    () => api.listMemories({ ...partition, ...f, q, all_scopes: allScopes || undefined, limit: 200 }),
    [partition.org_key, partition.project_key, f.scope, f.subject, f.role, f.status, q, allScopes],
  )
  const action = useAction(reload)
  const items = data?.items || []
  const err = error || action.error
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.value }))

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      <div className="card row">
        <div className="field"><label><Term f="scope" /></label>
          <select value={f.scope} onChange={set('scope')}><option value="">全部</option>
            {enumOptions('scope').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}</select></div>
        <div className="field"><label><Term f="subject" /></label>
          <select value={f.subject} onChange={set('subject')}><option value="">全部</option>
            {enumOptions('subject').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}</select></div>
        <div className="field"><label><Term f="role" /></label>
          <select value={f.role} onChange={set('role')}><option value="">全部</option>
            {enumOptions('role').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}</select></div>
        {/* ★ status 没有「全部」选项：后端缺省=active、单值过滤，没有「全部」语义
            （空串会被当 status='' 精确匹配→0 条）。看已删的切「已删除」。 */}
        <div className="field"><label><Term f="status" /></label>
          <select value={f.status} onChange={set('status')}>
            {enumOptions('memory_status').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}</select></div>
        <div className="field flex-1"><label>搜索（正文/关键词 ILIKE）</label>
          <input className="input w-full" value={qInput} onChange={(e) => setQInput(e.target.value)}
                 placeholder="不是语义检索，那是召回调试页" /></div>
        <div className="field"><label>&nbsp;</label>
          <label className="muted fs-sm">
            <input type="checkbox" checked={allScopes} onChange={(e) => setAllScopes(e.target.checked)} /> 浏览全库
          </label></div>
        <div className="spacer" />
        <span className="muted fs-sm">共 {data?.total ?? 0} 条</span>
        <button className="btn" onClick={reload} disabled={loading}>{loading ? '加载中…' : '刷新'}</button>
      </div>

      {items.length === 0 && !loading && (
        <EmptyState
          title={allScopes ? '全库还没有记忆' : '这个分区（按可见性裁剪后）还没有记忆'}
          hint={f.status !== 'active' ? undefined
            : `当前只看「生效中」，被软删/替代的切上方状态筛选查看${allScopes ? '' : '；或勾「浏览全库」看所有'}。`}
        />
      )}

      {items.map((m) => <MemoryCard key={m.id} m={m} action={action} />)}
    </div>
  )
}

function MemoryCard({ m, action }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({ content: m.content, role: m.role, scope: m.scope,
    subject: m.subject || '', keywords: (m.keywords || []).join(', '), why: m.why || '' })
  const [note, setNote] = useState('')
  const [showEvents, setShowEvents] = useState(false)
  const [events, setEvents] = useState(null)
  const [eventsErr, setEventsErr] = useState(null)
  const { confirm, dialog } = useConfirm()
  const toast = useToast()
  const set = (k) => (e) => setDraft((s) => ({ ...s, [k]: e.target.value }))
  const deleted = m.status !== 'active'

  const save = async () => {
    const out = await action.run(() => api.patchMemory(m.id, {
      content: draft.content, role: draft.role, scope: draft.scope,
      subject: draft.subject || null, why: draft.why || null,
      keywords: draft.keywords.split(',').map((x) => x.trim()).filter(Boolean),
    }))
    if (out) { setEditing(false); toast.success(`已保存记忆 #${m.id}`) }
  }

  // 软删/恢复都写 memory_events 留档，但删错一条会让它从注入里消失——补确认。
  const del = async () => {
    const sure = await confirm({
      title: `软删记忆 #${m.id}？`,
      body: '软删后不再参与注入与汇总，可随时恢复。理由会写进事件流水。',
      danger: true,
      okText: '删除',
    })
    if (!sure) return
    const ok = await action.run(() => api.deleteMemory(m.id, note.trim() || undefined, false))
    if (ok) toast.success(`已软删 #${m.id}`)
  }
  const restore = async () => {
    const ok = await action.run(() => api.deleteMemory(m.id, note.trim() || undefined, true))
    if (ok) toast.success(`已恢复 #${m.id}`)
  }
  // 原版没 try/catch：后端挂了这里会 unhandled rejection。
  const loadEvents = async () => {
    const next = !showEvents
    setShowEvents(next)
    if (next && !events) {
      setEventsErr(null)
      try {
        setEvents(await api.memoryEvents(m.id))
      } catch (e) {
        setEventsErr(e.message || String(e))
      }
    }
  }

  return (
    <div className={deleted ? 'card is-deleted' : 'card'}>
      {dialog}
      <div className="row">
        <span className="meta">#{m.id}</span>
        <Badge enum="scope" value={m.scope} />
        <Badge enum="subject" value={m.subject || '—'} />
        <Badge enum="role" value={m.role} />
        <Badge enum="memory_status" value={m.status} />
        {m.project_key && <span className="meta">@ {m.project_key}</span>}
        <div className="spacer" />
        <span className="meta">提及 {m.mention_count} · 改 {m.revision_count}</span>
      </div>

      {editing ? (
        <>
          <textarea value={draft.content} onChange={set('content')} />
          <div className="row">
            <div className="field"><label><Term f="scope" /></label>
              <select value={draft.scope} onChange={set('scope')}>
                {enumOptions('scope').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}</select></div>
            <div className="field"><label><Term f="subject" /></label>
              <select value={draft.subject} onChange={set('subject')}>
                <option value="">—</option>
                {enumOptions('subject').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}</select></div>
            <div className="field"><label><Term f="role" /></label>
              <select value={draft.role} onChange={set('role')}>
                {enumOptions('role').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}</select></div>
          </div>
          <input className="input w-full" value={draft.keywords} onChange={set('keywords')}
                 placeholder="关键词（逗号分隔）" />
          <input className="input w-full mt-6" value={draft.why} onChange={set('why')}
                 placeholder="依据（为什么记这条）" />
          <div className="row mt-8">
            <button className="btn primary" onClick={save} disabled={action.busy}>保存</button>
            <button className="btn" onClick={() => setEditing(false)} disabled={action.busy}>取消</button>
          </div>
        </>
      ) : (
        <>
          <div className="diff-new my-8">{m.content}</div>
          {m.why && <div className="meta mb-6">依据：{m.why}</div>}
          {(m.keywords || []).length > 0 && (
            <div className="row mb-6">
              {m.keywords.map((k) => <Badge key={k} tone="neutral">{k}</Badge>)}
            </div>
          )}
          <div className="row">
            <button className="btn" onClick={() => setEditing(true)} disabled={action.busy}>编辑</button>
            {deleted
              ? <button className="btn green" onClick={restore} disabled={action.busy}>恢复</button>
              : <button className="btn red" onClick={del} disabled={action.busy}>删除</button>}
            <input className="input flex-1 min-w-100" value={note} onChange={(e) => setNote(e.target.value)}
                   placeholder={deleted ? '恢复说明（可选）' : '删除理由（可选）'} />
            <button className="btn" onClick={loadEvents}>{showEvents ? '收起事件' : '事件流水'}</button>
          </div>
          <div className="meta mt-6">
            建 {fmtDateTime(m.created_at)} · 改 {fmtDateTime(m.updated_at)}{m.last_seen_at ? ` · 最近命中 ${fmtDateTime(m.last_seen_at)}` : ''}
          </div>
        </>
      )}

      {showEvents && eventsErr && <div className="alert alert-error mt-8">事件加载失败：{eventsErr}</div>}
      {showEvents && events && (
        <div className="mt-10">
          {events.length === 0 && <span className="meta">暂无事件</span>}
          {events.map((e) => <EventRow key={e.id} e={e} />)}
        </div>
      )}
    </div>
  )
}

function EventRow({ e }) {
  const [open, setOpen] = useState(false)
  const changed = e.old_content !== e.new_content && e.old_content != null
  return (
    <div className="list-row">
      <Badge enum="memory_action" value={e.action} />
      <span className="meta">{e.actor}</span>
      {e.source_msg && <span className="meta">源消息 #{e.source_msg}</span>}
      <div className="spacer" />
      <span className="muted fs-sm">{fmtDateTime(e.created_at)}</span>
      {changed && <button className="btn sm" onClick={() => setOpen((v) => !v)}>{open ? '收起' : '看 diff'}</button>}
      {changed && open && (
        <div className="w-full">
          <DiffPane oldText={e.old_content} newText={e.new_content} />
        </div>
      )}
    </div>
  )
}
