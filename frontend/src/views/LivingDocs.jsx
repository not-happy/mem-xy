import React, { useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import DiffPane from '../components/DiffPane.jsx'
import EmptyState from '../components/EmptyState.jsx'
import Spinner from '../components/Spinner.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { runBatch } from '../utils/batch.js'
import { enumOptions, enumItem } from '../i18n/dict.js'
import { fmtDateTime } from '../utils/format.js'

export default function LivingDocs({ partition }) {
  const [authority, setAuthority] = useState('')
  const [sel, setSel] = useState({})
  const { data, error, loading, reload } = useFetch(
    () => api.listLivingDoc({ ...partition, authority, limit: 200 }),
    [partition.org_key, partition.project_key, authority],
  )
  const action = useAction(reload)

  const items = data?.items || []
  const err = error || action.error

  // tree() 返回的是扁平列表，已按 s.sort, section_key, project NULLS FIRST 排好。
  // 这里按 section_key 分组成"树"，顺序天然跟着后端走（§13 P6 的 section 树）。
  const groups = []
  for (const doc of items) {
    const last = groups[groups.length - 1]
    if (last && last.section_key === doc.section_key) last.docs.push(doc)
    else groups.push({ section_key: doc.section_key, label: doc.label, docs: [doc] })
  }

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      <div className="card row">
        <div className="field">
          <label>只看 <Term f="authority" /></label>
          <select value={authority} onChange={(e) => setAuthority(e.target.value)}>
            <option value="">全部</option>
            {enumOptions('authority').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
          </select>
        </div>
        <div className="spacer" />
        <span className="muted fs-sm">共 {data?.total ?? 0} 条 · {groups.length} 个栏位</span>
        <button className="btn" onClick={reload} disabled={loading}>{loading ? '加载中…' : '刷新'}</button>
      </div>

      <BatchAuthority items={items} sel={sel} setSel={setSel} action={action} />

      {items.length === 0 && !loading && (
        <EmptyState title="这个分区还没有成文规范"
                    hint="攒够观察后由汇总器生成提案，采纳后出现在这里。" />
      )}

      {groups.map((g) => (
        <div key={g.section_key} className="mb-16">
          <div className="row" style={{ alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
            <span className="section-label fs-15">{g.label || g.section_key}</span>
            <span className="meta mono">{g.section_key}</span>
            <div className="spacer" />
            <Badge tone="neutral">{g.docs.length} 条</Badge>
          </div>
          {g.docs.map((doc) => (
            <DocCard key={doc.id} doc={doc} action={action} sel={sel} setSel={setSel} />
          ))}
        </div>
      ))}
    </div>
  )
}

function BatchAuthority({ items, sel, setSel, action }) {
  const [target, setTarget] = useState('confirmed')
  const [progress, setProgress] = useState(null)  // {done, total}
  const { confirm, dialog } = useConfirm()
  const toast = useToast()
  const chosen = items.filter((d) => sel[d.id])

  // 批量改档逐条串行、出错即停（保留原语义），进度条显示 x/y。
  const apply = async () => {
    const sure = await confirm({
      title: `批量改档 ${chosen.length} 条？`,
      body: `将改成「${enumItem('authority', target).zh}」。authority 影响汇总器是否敢动这一段。`,
      okText: '改档',
    })
    if (!sure) return
    setProgress({ done: 0, total: chosen.length })
    const r = await runBatch(
      chosen,
      (d) => api.setAuthority(d.id, target),
      (done, total) => setProgress({ done, total }),
    )
    setProgress(null)
    setSel({})
    if (r.failed) toast.error(`改到第 ${r.ok + 1} 条失败：${r.firstError}`)
    else toast.success(`已改档 ${r.ok} 条`)
  }

  return (
    <div className={`card row ${chosen.length ? 'is-active' : ''}`}>
      {dialog}
      <button className="btn" onClick={() => {
        const all = {}
        if (chosen.length !== items.length) for (const d of items) all[d.id] = true
        setSel(all)
      }}>{items.length > 0 && chosen.length === items.length ? '清空' : '全选'}</button>
      {chosen.length === 0
        ? <span className="meta">勾选下面的成文规范后可批量改 authority</span>
        : (
          <>
            <Badge tone="info">已选 {chosen.length} 条</Badge>
            <span className="meta">改成</span>
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              {enumOptions('authority').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
            </select>
            <div className="spacer" />
            <button className="btn primary" onClick={apply} disabled={action.busy || !!progress}>
              批量改档
            </button>
          </>
        )}
      {progress && (
        <span className="meta"><Spinner /> 进度 {progress.done}/{progress.total}</span>
      )}
    </div>
  )
}

function DocCard({ doc, action, sel, setSel }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(doc.content)
  const [note, setNote] = useState('')
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState(null)
  const [historyErr, setHistoryErr] = useState(null)
  const toast = useToast()

  const save = async () => {
    const out = await action.run(() => api.editLivingDoc(doc.id, draft, note || undefined))
    if (out) {
      setEditing(false); setNote(''); setHistory(null)
      toast.success(`已保存 v${doc.version + 1}`)
    }
  }
  const changeAuthority = async (a) => {
    const out = await action.run(() => api.setAuthority(doc.id, a))
    if (out) { setHistory(null); toast.success(`已改成${enumItem('authority', a).zh}`) }
  }
  // 原版没 try/catch：后端挂了这里会 unhandled rejection。
  const loadHistory = async () => {
    const next = !showHistory
    setShowHistory(next)
    if (next && !history) {
      setHistoryErr(null)
      try {
        setHistory(await api.history(doc.id))
      } catch (e) {
        setHistoryErr(e.message || String(e))
      }
    }
  }

  const sourceIds = doc.source_ids || []

  return (
    <div className="card">
      <div className="row">
        <input type="checkbox" checked={!!sel[doc.id]}
               onChange={(e) => setSel((s) => ({ ...s, [doc.id]: e.target.checked }))} />
        <Badge enum="authority" value={doc.authority} />
        <span className="meta">v{doc.version}</span>
        {doc.project_key
          ? <span className="meta">@ {doc.project_key}</span>
          : <Badge tone="neutral">org 级</Badge>}
        <div className="spacer" />
        <span className="meta">{doc.updated_at}</span>
      </div>

      {/* 溯源（§13 P6）：origin 是 summarized（汇总器合成）还是 human（人工改过）；
          origin_ref 是改动说明/提案引用；source_ids 是喂进来的观察 id。 */}
      <div className="row" style={{ marginTop: 6, gap: 6, flexWrap: 'wrap' }}>
        <Badge enum="origin" value={doc.origin} />
        {doc.origin_ref && <span className="meta">备注：{doc.origin_ref}</span>}
        {sourceIds.length > 0 && (
          <span className="meta" title={sourceIds.join(', ')}>源自 {sourceIds.length} 条观察</span>
        )}
      </div>

      {editing ? (
        <>
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} />
          <input className="input w-full"
            placeholder="改动说明（可选，写进 history）" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="row mt-8">
            <button className="btn primary" onClick={save} disabled={action.busy}>保存</button>
            <button className="btn" onClick={() => { setEditing(false); setDraft(doc.content) }}
              disabled={action.busy}>取消</button>
          </div>
        </>
      ) : (
        <>
          <div className="diff-new my-8">{doc.content}</div>
          <div className="row">
            <button className="btn" onClick={() => setEditing(true)}>编辑</button>
            {enumOptions('authority').filter((o) => o.value !== doc.authority).map((o) => (
              <button key={o.value} className="btn" onClick={() => changeAuthority(o.value)}
                disabled={action.busy}>→ {o.zh}</button>
            ))}
            <button className="btn" onClick={loadHistory}>{showHistory ? '收起历史' : '版本历史'}</button>
          </div>
        </>
      )}

      {showHistory && historyErr && <div className="alert alert-error mt-8">历史加载失败：{historyErr}</div>}
      {showHistory && history && (
        <div className="mt-10">
          {history.length === 0 && <span className="meta">暂无历史</span>}
          {history.map((h) => <HistoryEntry key={h.id} h={h} />)}
        </div>
      )}
    </div>
  )
}

function HistoryEntry({ h }) {
  const [open, setOpen] = useState(false)
  const contentChanged = h.old_content !== h.new_content
  const authChanged = h.old_authority !== h.new_authority
  return (
    <div className="list-row">
      <span className="meta">v{h.version} · {h.changed_by}</span>
      {authChanged && (
        <Badge tone="neutral" title={`${h.old_authority} → ${h.new_authority}`}>
          {enumItem('authority', h.old_authority).zh} → {enumItem('authority', h.new_authority).zh}
        </Badge>
      )}
      {h.proposal_id && <Badge tone="info">提案#{h.proposal_id}</Badge>}
      <div className="spacer" />
      <span className="muted fs-sm">{fmtDateTime(h.created_at)}</span>
      {contentChanged && (
        <button className="btn sm" onClick={() => setOpen((v) => !v)}>
          {open ? '收起 diff' : '看 diff'}
        </button>
      )}
      {contentChanged && open && (
        <div className="w-full">
          <DiffPane oldTitle={`旧（v${h.version - 1}）`} newTitle={`新（v${h.version}）`}
                    oldText={h.old_content} newText={h.new_content} />
        </div>
      )}
    </div>
  )
}
