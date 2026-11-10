import React, { useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import EmptyState from '../components/EmptyState.jsx'
import Spinner from '../components/Spinner.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { runBatch } from '../utils/batch.js'
import { enumOptions } from '../i18n/dict.js'

// 归属管理：两张表。org_match_rules = 怎么判（规则），workspace_map = 判过的结果（缓存）。
// ★ 重点场景（plan §12）：resolved_by='default' 的目录是没命中任何规则、被兜底判成 personal 的，
//   人工确认 org 后一次批量回溯改 org_key（retroactive 会顺着 conversation_log.cwd 把老记忆一起搬）。
export default function Attribution() {
  const [source, setSource] = useState('default')
  const [orgFilter, setOrgFilter] = useState('')
  const [sel, setSel] = useState({})
  const { data, error, loading, reload } = useFetch(
    () => api.getAttribution({ resolved_by: source, org_key: orgFilter, limit: 500 }),
    [source, orgFilter],
  )
  const action = useAction(reload)
  const err = error || action.error
  const workspaces = data?.workspaces || []

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}

      <RulesCard data={data} action={action} />

      <div className="card row mt-12">
        <span className="section-label">目录归属（workspace_map）</span>
        <div className="field">
          <label><Term f="resolved_by" /></label>
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">全部</option>
            {enumOptions('resolved_by').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
          </select>
        </div>
        <div className="field">
          <label><Term f="org_key" /> 过滤</label>
          <input className="input" value={orgFilter} onChange={(e) => setOrgFilter(e.target.value)} placeholder="personal" />
        </div>
        <div className="spacer" />
        <span className="muted fs-sm">
          默认归属 {data?.default_count ?? 0} 个 · 共 {workspaces.length} 行
        </span>
        <button className="btn" onClick={reload} disabled={loading}>{loading ? '加载中…' : '刷新'}</button>
      </div>

      <BatchBar workspaces={workspaces} sel={sel} setSel={setSel} action={action} />

      {workspaces.length === 0 && !loading && (
        <EmptyState title="没有匹配的目录"
                    hint={source === 'default' ? '没有待确认的默认归属，好事。' : undefined} />
      )}

      {workspaces.map((w) => (
        <WorkspaceRow key={w.cwd} w={w} action={action} sel={sel} setSel={setSel} />
      ))}
    </div>
  )
}

function BatchBar({ workspaces, sel, setSel, action }) {
  const [org, setOrg] = useState('')
  const [project, setProject] = useState('')
  const [progress, setProgress] = useState(null)
  const { confirm, dialog } = useConfirm()
  const toast = useToast()
  const chosen = workspaces.filter((w) => sel[w.cwd])

  // 回溯会把老记忆一起搬 org——批量更要把话说清楚：搬几个、搬到哪。
  const apply = async () => {
    if (!org.trim()) { action.setError('要先填 org_key'); return }
    const sure = await confirm({
      title: `批量回溯 ${chosen.length} 个目录？`,
      body: `改归到「${org.trim()}${project.trim() ? ` / ${project.trim()}` : ''}」，`
        + '并顺着 conversation_log.cwd 把这些目录的老记忆一起搬过去。',
      okText: '回溯',
    })
    if (!sure) return
    setProgress({ done: 0, total: chosen.length })
    const r = await runBatch(
      chosen,
      (w) => api.putAttribution({
        cwd: w.cwd, org_key: org.trim(), project_key: project.trim() || null, retroactive: true,
      }),
      (done, total) => setProgress({ done, total }),
    )
    setProgress(null)
    setSel({})
    if (r.failed) toast.error(`搬到第 ${r.ok + 1} 个失败：${r.firstError}`)
    else toast.success(`已回溯 ${r.ok} 个目录`)
  }

  return (
    <div className={`card row ${chosen.length ? 'is-active' : ''}`}>
      {dialog}
      <button className="btn" onClick={() => {
        const all = {}; if (chosen.length !== workspaces.length) for (const w of workspaces) all[w.cwd] = true
        setSel(all)
      }}>{chosen.length === workspaces.length && workspaces.length ? '清空' : '全选'}</button>
      {chosen.length === 0
        ? <span className="meta">勾选下面的目录后可批量回溯改归属</span>
        : (
          <>
            <Badge tone="info">已选 {chosen.length} 个</Badge>
            <input className="input sm" value={org} onChange={(e) => setOrg(e.target.value)} placeholder="org_key（如 dida）" />
            <input className="input sm" value={project} onChange={(e) => setProject(e.target.value)} placeholder="project_key（可选）" />
            <div className="spacer" />
            <button className="btn primary" onClick={apply} disabled={action.busy || !!progress}>批量回溯改归属</button>
          </>
        )}
      {progress && <span className="meta"><Spinner /> 进度 {progress.done}/{progress.total}</span>}
    </div>
  )
}

function WorkspaceRow({ w, action, sel, setSel }) {
  const [editing, setEditing] = useState(false)
  const [org, setOrg] = useState(w.org_key || '')
  const [project, setProject] = useState(w.project_key || '')
  const toast = useToast()
  const human = w.resolved_by === 'human'

  const save = async () => {
    const out = await action.run(() => api.putAttribution({
      cwd: w.cwd, org_key: org.trim(), project_key: project.trim() || null, retroactive: true,
    }))
    if (out) { setEditing(false); toast.success('已改归属并回溯') }
  }

  return (
    <div className="card">
      <div className="row">
        <input type="checkbox" checked={!!sel[w.cwd]}
               onChange={(e) => setSel((s) => ({ ...s, [w.cwd]: e.target.checked }))} />
        <span className="section-label mono fs-sm">{w.cwd}</span>
        <Badge enum="resolved_by" value={w.resolved_by} />
        <div className="spacer" />
        {!editing && <span className="meta">{w.org_key}{w.project_key ? ` / ${w.project_key}` : ''}</span>}
      </div>
      {editing ? (
        <div className="row mt-8">
          <input className="input sm" value={org} onChange={(e) => setOrg(e.target.value)} placeholder="org_key" />
          <input className="input sm" value={project} onChange={(e) => setProject(e.target.value)} placeholder="project_key" />
          <button className="btn green" onClick={save} disabled={action.busy}>保存并回溯</button>
          <button className="btn" onClick={() => setEditing(false)} disabled={action.busy}>取消</button>
        </div>
      ) : (
        <div className="row mt-8">
          <button className="btn" onClick={() => setEditing(true)} disabled={action.busy}>改归属</button>
          {human && <span className="meta">人工定过，规则不会再覆盖它</span>}
        </div>
      )}
    </div>
  )
}

function RulesCard({ data, action }) {
  const [adding, setAdding] = useState(false)
  // confirm 放卡片层：RuleRow 是 <tr>，dialog 没地方挂（塞 td 会被表格布局坑）。
  const { confirm, dialog } = useConfirm()
  const rules = data?.rules || []
  return (
    <div className="card">
      {dialog}
      <div className="row">
        <span className="section-label">归属规则（org_match_rules）</span>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setAdding((v) => !v)}>
          {adding ? '收起' : '+ 新规则'}
        </button>
      </div>
      {adding && <RuleEditor action={action} onDone={() => setAdding(false)} />}
      {rules.length > 0 && (
        <div className="tbl-wrap mt-10">
          <table className="tbl">
            <thead>
              <tr>
                <th><Term f="priority" /></th><th><Term f="pattern_type" /></th><th><Term f="pattern" /></th>
                <th><Term f="org_key" /></th><th><Term f="project_key" /></th><th>启用</th><th />
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <RuleRow key={r.id} r={r} action={action} confirm={confirm} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rules.length === 0 && (
        <EmptyState title="还没有规则" hint="没规则时所有目录都兜底判成 personal。" />
      )}
    </div>
  )
}

function RuleRow({ r, action, confirm }) {
  const [editing, setEditing] = useState(false)
  const toast = useToast()

  // 删规则会让它管着的目录掉回兜底归属——补确认。
  const del = async () => {
    const sure = await confirm({
      title: `删除规则 #${r.id}？`,
      body: `「${r.pattern}」不再参与归属判定，它管着的目录会掉回兜底（personal）。`,
      danger: true,
      okText: '删除',
    })
    if (!sure) return
    const ok = await action.run(() => api.deleteRule(r.id))
    if (ok) toast.success(`已删规则 #${r.id}`)
  }

  return editing
    ? <RuleEditor action={action} rule={r} onDone={() => setEditing(false)} />
    : (
      <tr>
        <td className="num">{r.priority}</td>
        <td><Badge enum="pattern_type" value={r.pattern_type} /></td>
        <td className="mono">{r.pattern}</td>
        <td>{r.org_key}</td>
        <td>{r.project_key || '—'}</td>
        <td><span className={r.enabled ? 'bool yes' : 'bool no'}>{r.enabled ? '✓' : '✗'}</span></td>
        <td>
          <button className="btn sm" onClick={() => setEditing(true)}>改</button>{' '}
          <button className="btn sm danger" onClick={del}>删</button>
        </td>
      </tr>
    )
}

function RuleEditor({ action, rule, onDone }) {
  const [f, setF] = useState({
    pattern_type: rule?.pattern_type || 'cwd_prefix',
    pattern: rule?.pattern || '',
    org_key: rule?.org_key || '',
    project_key: rule?.project_key || '',
    priority: rule?.priority ?? 50,
    enabled: rule?.enabled ?? true,
  })
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }))
  const save = () => action.run(() => api.putRule({
    ...(rule?.id ? { id: rule.id } : {}),
    pattern_type: f.pattern_type, pattern: f.pattern, org_key: f.org_key,
    project_key: f.project_key || null, priority: Number(f.priority), enabled: f.enabled,
  })).then((out) => out && onDone())

  return (
    <tr>
      <td><input className="input sm w-60" value={f.priority} onChange={set('priority')} /></td>
      <td>
        <select className="input sm" value={f.pattern_type} onChange={set('pattern_type')}>
          {enumOptions('pattern_type').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
        </select>
      </td>
      <td><input className="input sm" value={f.pattern} onChange={set('pattern')} placeholder="C:/Users/.../ 或 62bc.../%" /></td>
      <td><input className="input sm" value={f.org_key} onChange={set('org_key')} placeholder="dida" /></td>
      <td><input className="input sm" value={f.project_key} onChange={set('project_key')} placeholder="（可选）" /></td>
      <td><input type="checkbox" checked={f.enabled} onChange={set('enabled')} /></td>
      <td>
        <button className="btn sm green" onClick={save} disabled={action.busy}>存</button>{' '}
        <button className="btn sm" onClick={onDone}>取消</button>
      </td>
    </tr>
  )
}
