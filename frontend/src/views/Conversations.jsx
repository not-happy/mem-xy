import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useFetch } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import EmptyState from '../components/EmptyState.jsx'

// 会话存档：浏览 ingest 落库的原始对话（conversation_log，§3）。
// 上表 = 一次聊天一行（项目/消息数/时间跨度），点开下面按落库序回放消息流。
// ★ thinking 默认折叠（占大头且是过程稿）；分页 100 条一拉，「加载更多」续接。

const ROLE_TONE = { user: 'info', assistant: 'success', thinking: 'neutral', summary: 'warning' }
const PAGE = 100

import { fmtDateTime as fmtTs } from '../utils/format.js'
const shortId = (s) => `${s.slice(0, 8)}…`

export default function Conversations({ partition }) {
  const { data, error, loading, reload } = useFetch(
    () => api.listConversations({ org_key: partition.org_key, limit: 200 }),
    [partition.org_key],
  )
  const [selected, setSelected] = useState(null)

  // 分区切换后选中项可能不属于新分区，清掉避免「下面和上面不对应」的困惑。
  useEffect(() => { setSelected(null) }, [partition.org_key])

  return (
    <div>
      {error && <div className="alert alert-error">{error}</div>}
      <div className="card row">
        <span className="section-label">会话存档 · conversation_log</span>
        <span className="meta">ingest 扫 transcript 落的原始对话，只读</span>
        <div className="spacer" />
        <button className="btn" onClick={reload} disabled={loading}>
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      {data && data.length === 0 && <EmptyState title="还没有摄取到任何会话" />}
      {data && data.length > 0 && (
        <div className="card tbl-wrap mb-12">
          <table className="tbl">
            <thead>
              <tr>
                <th>会话</th><th>项目</th><th className="num">消息</th>
                <th className="num">其中 user</th><th className="num">估 tok</th>
                <th>首条</th><th>末条</th>
              </tr>
            </thead>
            <tbody>
              {data.map((s) => (
                <tr key={s.session_id}
                    className={selected === s.session_id ? 'row-selected' : undefined}
                    style={{ cursor: 'pointer' }}
                    onClick={() => setSelected(s.session_id)}>
                  <td className="mono" title={s.session_id}>{shortId(s.session_id)}</td>
                  <td className="meta">{s.project_key || s.cwd || s.org_key || '—'}</td>
                  <td className="num">{s.msgs}</td>
                  <td className="num">{s.user_msgs}</td>
                  <td className="num">{Number(s.tokens_est).toLocaleString()}</td>
                  <td className="meta">{fmtTs(s.first_ts)}</td>
                  <td className="meta">{fmtTs(s.last_ts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && <MessageStream sessionId={selected} />}
    </div>
  )
}

// 选中会话的消息流：分页拉取 + 加载更多；thinking 折叠。
function MessageStream({ sessionId }) {
  const [state, setState] = useState({ items: [], total: 0, offset: 0, loading: true, error: null })

  const load = useCallback(async (append) => {
    setState((s) => ({ ...s, loading: true, error: null }))
    try {
      const offset = append ? state.offset : 0
      const d = await api.conversationMessages(sessionId, { offset, limit: PAGE })
      setState((s) => ({
        items: append ? [...s.items, ...d.items] : d.items,
        total: d.total,
        offset: offset + d.items.length,
        loading: false, error: null,
      }))
    } catch (e) {
      setState((s) => ({ ...s, loading: false, error: e.message || String(e) }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, state.offset])

  useEffect(() => { load(false) }, [sessionId])  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="card">
      <div className="row">
        <span className="section-label">消息流 · <span className="mono">{shortId(sessionId)}</span></span>
        <span className="meta">{state.items.length} / {state.total} 条</span>
        <div className="spacer" />
        {state.offset < state.total && (
          <button className="btn" disabled={state.loading} onClick={() => load(true)}>
            {state.loading ? '加载中…' : `加载更多（还有 ${state.total - state.offset}）`}
          </button>
        )}
      </div>
      {state.error && <div className="alert alert-error">{state.error}</div>}
      {state.items.map((m, i) => (
        <MessageRow key={m.id} m={m} newTurn={i > 0 && m.turn_no !== state.items[i - 1].turn_no} />
      ))}
    </div>
  )
}

function MessageRow({ m, newTurn }) {
  const body = (
    <div style={{ fontSize: 13, marginTop: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
      {m.content}
    </div>
  )
  return (
    <div className="list-row" style={{ display: 'block' }}>
      {newTurn && <div className="meta my-8" style={{ borderTop: '1px solid var(--border-2)', paddingTop: 8 }}>
        ── 轮次 {m.turn_no ?? '—'} ──
      </div>}
      <div className="row">
        <Badge tone={ROLE_TONE[m.role] || 'neutral'}>{m.role}</Badge>
        <span className="meta">{fmtTs(m.msg_ts)}</span>
        {m.tokens_est ? <span className="meta">~{m.tokens_est} tok</span> : null}
      </div>
      {m.role === 'thinking'
        ? <details><summary className="meta">思考过程（点击展开）</summary>{body}</details>
        : body}
    </div>
  )
}
