import React, { useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'

// 组织管理：orgs（检索分区）+ llm_endpoints（端点的非敏感字段）。
// ★ org 只作检索分区、不是权限边界（§14 已定）。删 org 有守卫：被数据引用就拒，提示改用「停用」。
// ★ 端点这里**只有非敏感字段**，token 绝不经此进出（§2.1）——页面上明确写出来，免得有人以为能填 token。
export default function Orgs() {
  const { data, error, loading, reload } = useFetch(() => api.getOrgs(), [])
  const action = useAction(reload)
  const err = error || action.error

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      <div className="card row">
        <span className="section-label">组织管理</span>
        <span className="meta">org = 检索分区，不是权限边界</span>
        <div className="spacer" />
        <button className="btn" onClick={reload} disabled={loading}>{loading ? '加载中…' : '刷新'}</button>
      </div>

      <OrgsCard data={data} action={action} />
      <EndpointsCard data={data} action={action} />
    </div>
  )
}

// ─────────────────── orgs ───────────────────

function OrgsCard({ data, action }) {
  const [adding, setAdding] = useState(false)
  // confirm/dialog 挂卡片层：OrgRow 是 <tr>，dialog 没地方挂。
  const { confirm, dialog } = useConfirm()
  const orgs = data?.orgs || []
  return (
    <div className="card">
      {dialog}
      <div className="row">
        <span className="section-label">组织（orgs）</span>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setAdding((v) => !v)}>{adding ? '收起' : '+ 新组织'}</button>
      </div>
      {adding && <OrgEditor action={action} onDone={() => setAdding(false)} />}
      {orgs.length > 0 && (
        <div className="tbl-wrap mt-10">
          <table className="tbl">
            <thead>
              <tr>
                <th><Term f="org_key" /></th><th>名称</th><th>类型</th>
                <th className="num"><Term f="retention_days" /></th><th className="num">记忆</th>
                <th className="num">待办</th><th>启用</th><th />
              </tr>
            </thead>
            <tbody>
              {orgs.map((o) => <OrgRow key={o.org_key} o={o} action={action} confirm={confirm} />)}
            </tbody>
          </table>
        </div>
      )}
      {orgs.length === 0 && !adding && <EmptyState title="还没有组织" />}
    </div>
  )
}

function OrgRow({ o, action, confirm }) {
  const [editing, setEditing] = useState(false)
  const toast = useToast()

  const toggle = async () => {
    const ok = await action.run(() => api.patchOrg(o.org_key, { enabled: !o.enabled }))
    if (ok) toast.success(o.enabled ? `已停用 ${o.org_key}` : `已启用 ${o.org_key}`)
  }
  // 删 org 后端有守卫（被引用会拒），但仍是危险操作——确认框把退路说清楚。
  const del = async () => {
    const sure = await confirm({
      title: `删除组织 ${o.org_key}？`,
      body: '被数据引用会被后端拒绝，届时请改用「停用」。',
      danger: true,
      okText: '删除',
    })
    if (!sure) return
    const ok = await action.run(() => api.deleteOrg(o.org_key))
    if (ok) toast.success(`已删除组织 ${o.org_key}`)
  }

  return editing
    ? <OrgEditor action={action} org={o} onDone={() => setEditing(false)} />
    : (
      <tr>
        <td className="mono">{o.org_key}</td>
        <td>{o.name}</td>
        <td>{o.is_personal ? <Badge tone="info">个人默认桶</Badge> : <Badge tone="neutral">分区</Badge>}</td>
        <td className="num">{o.retention_days ?? <span className="muted">永久</span>}</td>
        <td className="num">{o.memory_count}</td>
        <td className="num">{o.pending_count > 0 ? <Badge tone="warning">{o.pending_count}</Badge> : 0}</td>
        <td><span className={o.enabled ? 'bool yes' : 'bool no'}>{o.enabled ? '✓' : '✗'}</span></td>
        <td>
          <button className="btn sm" onClick={() => setEditing(true)}>改</button>{' '}
          <button className="btn sm" onClick={toggle} disabled={action.busy}>{o.enabled ? '停用' : '启用'}</button>{' '}
          <button className="btn sm danger" onClick={del} disabled={action.busy}>删</button>
        </td>
      </tr>
    )
}

function OrgEditor({ action, org, onDone }) {
  const [f, setF] = useState({
    org_key: org?.org_key || '',
    name: org?.name || '',
    is_personal: org?.is_personal ?? false,
    retention_days: org?.retention_days ?? '',
    enabled: org?.enabled ?? true,
  })
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }))
  const save = () => {
    const retention = f.retention_days === '' ? null : Number(f.retention_days)
    const body = { name: f.name, is_personal: f.is_personal, retention_days: retention, enabled: f.enabled }
    const p = org
      ? action.run(() => api.patchOrg(org.org_key, body))
      : action.run(() => api.createOrg({ org_key: f.org_key.trim(), ...body }))
    p.then((out) => out && onDone())
  }
  return (
    <tr>
      <td>
        {org
          ? <span className="mono">{org.org_key}</span>
          : <input className="input sm" value={f.org_key} onChange={set('org_key')} placeholder="dida" />}
      </td>
      <td><input className="input sm" value={f.name} onChange={set('name')} placeholder="名称" /></td>
      <td><input type="checkbox" checked={f.is_personal} onChange={set('is_personal')} disabled={!!org} /></td>
      <td><input className="input sm w-60" value={f.retention_days} onChange={set('retention_days')}
                 placeholder="永久" /></td>
      <td colSpan={2} />
      <td><input type="checkbox" checked={f.enabled} onChange={set('enabled')} /></td>
      <td>
        <button className="btn sm green" onClick={save} disabled={action.busy}>存</button>{' '}
        <button className="btn sm" onClick={onDone}>取消</button>
      </td>
    </tr>
  )
}

// ─────────────────── llm_endpoints ───────────────────

function EndpointsCard({ data, action }) {
  const [adding, setAdding] = useState(false)
  const { confirm, dialog } = useConfirm()
  const endpoints = data?.endpoints || []
  const taskMap = data?.task_endpoints || {}
  return (
    <div className="card">
      {dialog}
      <div className="row">
        <span className="section-label">LLM 端点（llm_endpoints）</span>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setAdding((v) => !v)}>{adding ? '收起' : '+ 新端点'}</button>
      </div>
      <div className="alert alert-info my-8">
        ★ 这里只维护端点的<b>非敏感字段</b>（地址/模型/协议/上限）。<b>token 不在这张表、也不经这个页面</b>——
        它只走 <code>~/.memxy/secrets.toml</code> 或环境变量（§2.1）。默认端点：
        <Badge tone="info">{data?.default_endpoint || '—'}</Badge>
        {Object.keys(taskMap).length > 0 && (
          <span className="meta">
            任务覆盖：{Object.entries(taskMap).map(([t, k]) => `${t}→${k}`).join('，')}
          </span>
        )}
      </div>
      {adding && <EndpointEditor action={action} onDone={() => setAdding(false)} />}
      {endpoints.length > 0 && (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th><Term f="endpoint_key" /></th><th><Term f="base_url" /></th><th><Term f="model" /></th>
                <th>协议</th><th className="num"><Term f="max_input_tokens" /></th><th>启用</th><th />
              </tr>
            </thead>
            <tbody>
              {endpoints.map((e) => <EndpointRow key={e.endpoint_key} e={e} action={action} confirm={confirm} />)}
            </tbody>
          </table>
        </div>
      )}
      {endpoints.length === 0 && !adding && <EmptyState title="还没有端点" hint="没端点 LLM 调不动，先加一个。" />}
    </div>
  )
}

function EndpointRow({ e, action, confirm }) {
  const [editing, setEditing] = useState(false)
  const toast = useToast()

  const del = async () => {
    const sure = await confirm({
      title: `删除端点 ${e.endpoint_key}？`,
      body: '是默认端点或任务目标会被后端拒绝。',
      danger: true,
      okText: '删除',
    })
    if (!sure) return
    const ok = await action.run(() => api.deleteEndpoint(e.endpoint_key))
    if (ok) toast.success(`已删除端点 ${e.endpoint_key}`)
  }

  return editing
    ? <EndpointEditor action={action} endpoint={e} onDone={() => setEditing(false)} />
    : (
      <tr>
        <td className="mono">{e.endpoint_key}</td>
        <td className="mono fs-xs">{e.base_url}</td>
        <td>{e.model}</td>
        <td><Badge enum="api_protocol" value={e.api_protocol} /></td>
        <td className="num">{e.max_input_tokens ?? <span className="muted">—</span>}</td>
        <td><span className={e.enabled ? 'bool yes' : 'bool no'}>{e.enabled ? '✓' : '✗'}</span></td>
        <td>
          <button className="btn sm" onClick={() => setEditing(true)}>改</button>{' '}
          <button className="btn sm danger" onClick={del} disabled={action.busy}>删</button>
        </td>
      </tr>
    )
}

function EndpointEditor({ action, endpoint, onDone }) {
  const [f, setF] = useState({
    endpoint_key: endpoint?.endpoint_key || '',
    base_url: endpoint?.base_url || '',
    model: endpoint?.model || '',
    api_protocol: endpoint?.api_protocol || 'anthropic',
    max_input_tokens: endpoint?.max_input_tokens ?? '',
    enabled: endpoint?.enabled ?? true,
    note: endpoint?.note || '',
  })
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }))
  const save = () => action.run(() => api.putEndpoint({
    endpoint_key: f.endpoint_key.trim(), base_url: f.base_url.trim(), model: f.model.trim(),
    api_protocol: f.api_protocol,
    max_input_tokens: f.max_input_tokens === '' ? null : Number(f.max_input_tokens),
    enabled: f.enabled, note: f.note || null,
  })).then((out) => out && onDone())
  return (
    <>
      <tr>
        <td>
          {endpoint
            ? <span className="mono">{endpoint.endpoint_key}</span>
            : <input className="input sm" value={f.endpoint_key} onChange={set('endpoint_key')} placeholder="my-endpoint" />}
        </td>
        <td><input className="input sm w-200" value={f.base_url} onChange={set('base_url')} placeholder="http://..." /></td>
        <td><input className="input sm" value={f.model} onChange={set('model')} placeholder="model 名" /></td>
        <td>
          <select className="input sm" value={f.api_protocol} onChange={set('api_protocol')}>
            {['anthropic', 'openai', 'ollama'].map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </td>
        <td><input className="input sm w-70" value={f.max_input_tokens} onChange={set('max_input_tokens')}
                   placeholder="32000" /></td>
        <td><input type="checkbox" checked={f.enabled} onChange={set('enabled')} /></td>
        <td>
          <button className="btn sm green" onClick={save} disabled={action.busy}>存</button>{' '}
          <button className="btn sm" onClick={onDone}>取消</button>
        </td>
      </tr>
      <tr>
        <td className="muted">备注</td>
        <td colSpan={6}>
          <input className="input sm w-full" value={f.note} onChange={set('note')}
                 placeholder="（可选）这个端点是干嘛的" />
        </td>
      </tr>
    </>
  )
}
