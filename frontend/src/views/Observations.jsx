import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { enumOptions } from '../i18n/dict.js'
import { fmtDateTime } from '../utils/format.js'

export default function Observations({ partition }) {
  const [polarity, setPolarity] = useState('')
  const [consumed, setConsumed] = useState('')
  const [section, setSection] = useState('')
  const { data, error, loading, reload } = useFetch(
    () => api.listObservations({
      ...partition, polarity, section_key: section,
      consumed: consumed === '' ? undefined : consumed === 'true', limit: 100,
    }),
    [partition.org_key, partition.project_key, polarity, consumed, section],
  )
  const action = useAction(reload)

  const items = data?.items || []
  const err = error || action.error
  const [summarizeMsg, setSummarizeMsg] = useState(null)

  const triggerSummarize = async (force) => {
    setSummarizeMsg(null)
    const out = await action.run(() => api.triggerSummarize({
      ...partition, section_key: section || undefined, force,
    }))
    if (out) setSummarizeMsg(`已排汇总 job #${out.job_id}，worker 跑完再刷新看提案`)
  }

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      {summarizeMsg && <div className="alert alert-success">{summarizeMsg}</div>}

      <div className="card row">
        <div className="field">
          <label><Term f="section_key" />（可选）</label>
          <SectionInput value={section} onChange={setSection} />
        </div>
        <div className="field">
          <label><Term f="polarity" /></label>
          <select value={polarity} onChange={(e) => setPolarity(e.target.value)}>
            <option value="">全部</option>
            {enumOptions('polarity').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
          </select>
        </div>
        <div className="field">
          <label><Term f="consumed" /></label>
          <select value={consumed} onChange={(e) => setConsumed(e.target.value)}>
            <option value="">全部</option>
            <option value="false">未消费</option>
            <option value="true">已消费</option>
          </select>
        </div>
        <div className="spacer" />
        <button className="btn" onClick={reload} disabled={loading}>
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      <div className="card row">
        <span className="muted">手动触发汇总（排 job，异步调 LLM）：</span>
        <button className="btn primary" onClick={() => triggerSummarize(false)} disabled={action.busy}>
          按阈值汇总
        </button>
        <button className="btn" onClick={() => triggerSummarize(true)} disabled={action.busy}>
          强制汇总（绕过阈值）
        </button>
      </div>

      {items.length === 0 && !loading && (
        <EmptyState title="这个分区没有匹配的观察" hint="放宽筛选，或等 worker 下一轮抽取。" />
      )}

      {items.map((o) => (
        <ObservationCard key={o.id} o={o} action={action} />
      ))}
    </div>
  )
}

// 栏位键手打容易错（错了就是空结果）——datalist 给候选，仍允许自由输入新栏位。
function SectionInput({ value, onChange }) {
  const [sections, setSections] = useState([])
  useEffect(() => {
    let alive = true
    api.getSections().then((d) => { if (alive) setSections(d?.sections || d || []) }).catch(() => {})
    return () => { alive = false }
  }, [])
  return (
    <>
      <input className="input" list="section-keys" value={value}
             onChange={(e) => onChange(e.target.value)} placeholder="dependency" />
      <datalist id="section-keys">
        {sections.map((s) => <option key={s.section_key || s} value={s.section_key || s} />)}
      </datalist>
    </>
  )
}

function ObservationCard({ o, action }) {
  const { confirm, dialog } = useConfirm()
  const toast = useToast()

  // 撤销否决会把这条观察放回素材池参与汇总——确认一下，防手滑。
  const revoke = async () => {
    const sure = await confirm({
      title: `撤销观察 #${o.id} 的否决？`,
      body: '撤销后它回到正面素材池，下次汇总会再用它。',
      okText: '撤销否决',
    })
    if (!sure) return
    const ok = await action.run(() => api.revokeNegative(o.id))
    if (ok) toast.success(`已撤销 #${o.id} 的否决`)
  }

  return (
    <div className="card">
      {dialog}
      <div className="row">
        <span className="section-label">{o.label || o.section_key}</span>
        <Badge enum="polarity" value={o.polarity} />
        {o.consumed && <Badge tone="success">已消费</Badge>}
        {o.project_key && <span className="meta">@ {o.project_key}</span>}
        <div className="spacer" />
        <span className="meta">#{o.id} · {fmtDateTime(o.created_at)}</span>
      </div>
      <div className="diff-new my-8">{o.content}</div>
      {o.polarity === 'negative' && (
        <div className="row">
          <button className="btn red" onClick={revoke} disabled={action.busy}>
            撤销这条否决（当初否决错了）
          </button>
        </div>
      )}
    </div>
  )
}
