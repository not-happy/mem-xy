import React, { useCallback, useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import DiffPane from '../components/DiffPane.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { enumOptions, enumItem } from '../i18n/dict.js'
import { fmtDateTime } from '../utils/format.js'

export default function Proposals({ partition, onDecided }) {
  const [status, setStatus] = useState('pending')
  const { data, error, loading, reload } = useFetch(
    () => api.listProposals({ ...partition, status, limit: 100 }),
    [partition.org_key, partition.project_key, status],
  )
  // 采纳/否决成功后同步刷新侧边栏「待审 diff」红点，不等 60s 轮询。
  const refresh = useCallback(() => { reload(); onDecided?.() }, [reload, onDecided])
  const action = useAction(refresh)

  const items = data?.items || []
  const err = error || action.error

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      <div className="card row">
        <div className="field">
          <label>状态</label>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {enumOptions('proposal_status').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
          </select>
        </div>
        <div className="spacer" />
        <span className="muted fs-sm">共 {data?.total ?? 0} 条</span>
        <button className="btn" onClick={reload} disabled={loading}>
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      {items.length === 0 && !loading && (
        <EmptyState
          title={status === 'pending' ? '没有待审提案' : `没有${enumItem('proposal_status', status).zh}的提案`}
          hint={status === 'pending' ? '观察攒够阈值后汇总器会生成。' : '换个状态筛选看看。'}
        />
      )}

      {items.map((p) => (
        <ProposalCard key={p.id} p={p} action={action} canDecide={status === 'pending'} />
      ))}
    </div>
  )
}

function ProposalCard({ p, action, canDecide }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(p.new_content)
  const { confirm, dialog } = useConfirm()
  const toast = useToast()

  // 采纳会直接改写成文规范、否决会丢弃整段合成——都不可逆，统一补确认。
  const accept = async (content) => {
    const sure = await confirm({
      title: `采纳提案 #${p.id}？`,
      body: `「${p.label || p.section_key}」这一段成文规范将被改写。`,
      okText: '采纳',
    })
    if (!sure) return
    const ok = await action.run(() => api.acceptProposal(p.id, content))
    if (ok) toast.success(`已采纳 #${p.id}，成文规范已更新`)
  }
  const reject = async () => {
    const sure = await confirm({
      title: `否决提案 #${p.id}？`,
      body: '合成内容会被丢弃，观察线索保留（下次汇总还会用到）。',
      danger: true,
      okText: '否决',
    })
    if (!sure) return
    const ok = await action.run(() => api.rejectProposal(p.id))
    if (ok) toast.success(`已否决 #${p.id}`)
  }

  const sourceCount = p.source_ids?.length || 0

  return (
    <div className="card">
      {dialog}
      <div className="row">
        <span className="section-label">{p.label || p.section_key}</span>
        <Badge enum="proposal_status" value={p.status} />
        {p.job_status && <Badge enum="job_status" value={p.job_status} />}
        {p.project_key && <span className="meta">@ {p.project_key}</span>}
        <div className="spacer" />
        <span className="meta">#{p.id} · {fmtDateTime(p.created_at)}</span>
      </div>

      {p.job_status === 'failed' && p.last_error && (
        <div className="alert alert-error mt-8">落库失败：{p.last_error}</div>
      )}

      {editing ? (
        <div className="diff-row">
          <div className="diff-pane">
            <div className="diff-title">旧（现行成文规范）</div>
            <div className="diff-old">{p.old_content || '（这一段原来没有内容）'}</div>
          </div>
          <div className="diff-pane">
            <div className="diff-title">改后正文（将写入成文规范）</div>
            <textarea value={draft} onChange={(e) => setDraft(e.target.value)}
                      style={{ minHeight: 140, margin: 0 }} />
          </div>
        </div>
      ) : (
        <DiffPane
          oldTitle="旧（现行成文规范）"
          newTitle={`新（AI 合成，${sourceCount} 条线索）`}
          oldText={p.old_content}
          newText={p.new_content}
        />
      )}

      {canDecide && (
        <div className="row">
          {editing ? (
            <>
              <button className="btn green" onClick={() => accept(draft)} disabled={action.busy}>
                采纳（用改后的正文）
              </button>
              <button className="btn" onClick={() => { setEditing(false); setDraft(p.new_content) }}
                disabled={action.busy}>取消</button>
            </>
          ) : (
            <>
              <button className="btn green" onClick={() => accept(null)} disabled={action.busy}>
                采纳原文
              </button>
              <button className="btn" onClick={() => setEditing(true)} disabled={action.busy}>
                改后采纳
              </button>
              <button className="btn red" onClick={reject} disabled={action.busy}>否决</button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
