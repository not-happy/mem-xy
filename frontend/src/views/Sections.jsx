import React, { useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { enumOptions } from '../i18n/dict.js'

// 词表（doc_sections）：受控栏位（§5.3，栏位不自由生成，22 个 seed）。
// ★ 这一页是 §14 item1「复核 aliases」的工具：每栏位靠 label + aliases 的向量去匹配 content，
//   改 alias 就是改「哪些说法该归到这一栏」。缺词 = 匹配不出 = 变成用户的待办（§5.3）。
// ★ 改 label/aliases 后端会**重算 match_embedding**（要模型就绪，没热回 503）；只改 sort/enabled 不碰模型。
// ★ 停用（enabled=false）是安全下线：不丢数据、只是不再作为匹配候选；删除被观察/成文规范/引用会被拒（409）。

// aliases 在输入框里用顿号/逗号分隔，存回后端是 TEXT[]
const splitAliases = (s) => s.split(/[、,，\n]/).map((x) => x.trim()).filter(Boolean)

export default function Sections() {
  const { data, error, loading, reload } = useFetch(() => api.getSections(), [])
  const action = useAction(reload)
  const toast = useToast()
  const err = error || action.error

  const recompute = async () => {
    const ok = await action.run(() => api.recomputeSections())
    if (ok) toast.success('已触发重算（需模型就绪，完成后刷新看「向量」列）')
  }

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      <div className="card row">
        <span className="section-label">词表（doc_sections）</span>
        <span className="meta">受控栏位 · 改 aliases 会重算匹配向量</span>
        <div className="spacer" />
        {data && (
          <span className="row" style={{ marginRight: 8, gap: 6 }}>
            <span className="muted fs-sm">共 {data.total} · 启用 {data.enabled_count}</span>
            {data.missing_embedding > 0 && <Badge tone="danger">缺向量 {data.missing_embedding}</Badge>}
            {data.stale_count > 0 && <Badge tone="warning">待重算 {data.stale_count}</Badge>}
          </span>
        )}
        <button className="btn" onClick={recompute} disabled={action.busy}>重算 embedding</button>
        <button className="btn" onClick={reload} disabled={loading}>{loading ? '加载中…' : '刷新'}</button>
      </div>

      <SectionsCard data={data} action={action} />
    </div>
  )
}

function SectionsCard({ data, action }) {
  const [adding, setAdding] = useState(false)
  // confirm/dialog 挂卡片层：SectionRow 是 <tr>，dialog 没地方挂。
  const { confirm, dialog } = useConfirm()
  const sections = data?.sections || []
  return (
    <div className="card">
      {dialog}
      <div className="row">
        <span className="section-label">栏位</span>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setAdding((v) => !v)}>{adding ? '收起' : '+ 新栏位'}</button>
      </div>
      <div className="alert alert-info my-8">
        ★ <b>aliases 是这一栏的「同义词表」</b>：content 的向量跟「label。aliases」的向量比，谁最近归谁。
        某栏位老匹配不上（待办里阶段=选栏位堆积）通常就是 aliases 缺词，在这里补。
        改完显示名/同义词会自动重算向量（需 embedding 就绪）；<b>停用</b>不丢数据、只是不再参与匹配。
      </div>
      {adding && <SectionEditor action={action} appliesOptions={data?.applies_options} onDone={() => setAdding(false)} />}
      {sections.length > 0 && (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th className="num"><Term f="sort" /></th><th><Term f="section_key" /></th><th><Term f="label" /></th>
                <th><Term f="aliases" /></th><th><Term f="applies_to" /></th><th><Term f="org_key" /></th>
                <th><Term f="embedding" /></th><th>启用</th><th />
              </tr>
            </thead>
            <tbody>
              {sections.map((s) => (
                <SectionRow key={s.section_key} s={s} action={action} confirm={confirm}
                            appliesOptions={data?.applies_options} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {sections.length === 0 && !adding && <EmptyState title="词表为空" hint="没 seed？跑 bootstrap。" />}
    </div>
  )
}

function SectionRow({ s, action, confirm, appliesOptions }) {
  const [editing, setEditing] = useState(false)
  const toast = useToast()
  if (editing) return <SectionEditor action={action} section={s} appliesOptions={appliesOptions} onDone={() => setEditing(false)} />

  const toggle = async () => {
    const ok = await action.run(() => api.patchSection(s.section_key, { enabled: !s.enabled }))
    if (ok) toast.success(s.enabled ? `已停用 ${s.section_key}（数据保留，不再参与匹配）` : `已启用 ${s.section_key}`)
  }
  // 删除有后端守卫（被引用会拒 409），但仍是危险操作——确认框把退路说清楚。
  const del = async () => {
    const sure = await confirm({
      title: `删除栏位 ${s.section_key}？`,
      body: '被观察/成文规范/提案引用会被后端拒绝，届时请改用「停用」（不丢数据、只是不再参与匹配）。',
      danger: true,
      okText: '删除',
    })
    if (!sure) return
    const ok = await action.run(() => api.deleteSection(s.section_key))
    if (ok) toast.success(`已删除栏位 ${s.section_key}`)
  }

  return (
    <tr>
      <td className="num">{s.sort}</td>
      <td className="mono">{s.section_key}</td>
      <td>{s.label}</td>
      <td className="max-w-360">
        <span className="muted">{(s.aliases || []).join('、') || <i>（无）</i>}</span>
      </td>
      <td><Badge enum="applies_to" value={s.applies_to} /></td>
      <td>{s.org_key || <span className="muted">通用</span>}</td>
      <td>
        {!s.has_embedding
          ? <Badge tone="danger">缺失</Badge>
          : s.stale
            ? <Badge tone="warning">待重算</Badge>
            : <Badge tone="success">已算</Badge>}
      </td>
      <td><span className={s.enabled ? 'bool yes' : 'bool no'}>{s.enabled ? '✓' : '✗'}</span></td>
      <td>
        <button className="btn sm" onClick={() => setEditing(true)}>改</button>{' '}
        <button className="btn sm" onClick={toggle} disabled={action.busy}>{s.enabled ? '停用' : '启用'}</button>{' '}
        <button className="btn sm danger" onClick={del} disabled={action.busy}>删</button>
      </td>
    </tr>
  )
}

function SectionEditor({ action, section, appliesOptions, onDone }) {
  const [f, setF] = useState({
    section_key: section?.section_key || '',
    label: section?.label || '',
    aliases: (section?.aliases || []).join('、'),
    applies_to: section?.applies_to || 'org',
    org_key: section?.org_key || '',
    sort: section?.sort ?? 100,
    enabled: section?.enabled ?? true,
  })
  // 后端没给 applies_options 时用词典兜底（value 永远英文，CHECK 约束）
  const options = appliesOptions && appliesOptions.length > 0
    ? appliesOptions.map((v) => ({ value: v, zh: v }))
    : enumOptions('applies_to')
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }))
  const save = () => {
    const body = {
      label: f.label.trim(),
      aliases: splitAliases(f.aliases),
      applies_to: f.applies_to,
      org_key: f.org_key.trim() || null,
      sort: Number(f.sort),
      enabled: f.enabled,
    }
    const p = section
      ? action.run(() => api.patchSection(section.section_key, body))
      : action.run(() => api.createSection({ section_key: f.section_key.trim(), ...body }))
    p.then((out) => out && onDone())
  }
  return (
    <>
      <tr>
        <td><input className="input sm w-56" value={f.sort} onChange={set('sort')} /></td>
        <td>
          {section
            ? <span className="mono">{section.section_key}</span>
            : <input className="input sm" value={f.section_key} onChange={set('section_key')} placeholder="my_section" />}
        </td>
        <td><input className="input sm" value={f.label} onChange={set('label')} placeholder="栏位名" /></td>
        <td className="max-w-360">
          <input className="input sm w-full" value={f.aliases} onChange={set('aliases')}
                 placeholder="同义词，用、或逗号分隔" />
        </td>
        <td>
          <select className="input sm" value={f.applies_to} onChange={set('applies_to')}>
            {options.map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
          </select>
        </td>
        <td><input className="input sm w-70" value={f.org_key} onChange={set('org_key')} placeholder="通用" /></td>
        <td />
        <td><input type="checkbox" checked={f.enabled} onChange={set('enabled')} /></td>
        <td>
          <button className="btn sm green" onClick={save} disabled={action.busy}>存</button>{' '}
          <button className="btn sm" onClick={onDone}>取消</button>
        </td>
      </tr>
      <tr>
        <td colSpan={9} className="muted">
          ★ 改显示名/同义词会触发 match_embedding 重算（需 embedding 就绪，否则 503）。aliases 直接进匹配文本，别塞无关词。
        </td>
      </tr>
    </>
  )
}
