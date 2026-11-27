import React, { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { api } from '../api.js'
import { useFetch, useAction } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { fmtDateTime } from '../utils/format.js'
import { enumOptions, enumItem, reasonZh } from '../i18n/dict.js'

// 受控词表不在这里维护——ENUMS（i18n/dict.js）已与后端 CHECK 约束核对过，
// option 的 value 永远是英文原值，只译显示 label。
export default function Pending({ partition, onDecided }) {
  const [stage, setStage] = useState('')
  const [status, setStatus] = useState('pending')
  const { data, error, loading, reload } = useFetch(
    () => api.listPending({ ...partition, stage, status, limit: 100 }),
    [partition.org_key, partition.project_key, stage, status],
  )
  // 栏位中文名来自 doc_sections（词表页维护），待办卡里的英文键靠它翻译。
  const { data: secData } = useFetch(() => api.getSections(), [])
  const sections = secData?.sections || []
  // 裁决成功后除了刷本页列表，还要捅一下侧边栏红点（onDecided），不然数字要等 60s 轮询才变。
  const refresh = useCallback(() => { reload(); onDecided?.() }, [reload, onDecided])
  const action = useAction(refresh)

  const items = data?.items || []
  const err = error || action.error

  // ---- B5-① 聚簇视图 / B5-② 键盘流 ----
  const [view, setView] = useState('list')           // list | cluster
  const [selIdx, setSelIdx] = useState(-1)           // 键盘选中的卡，跟随 items 顺序
  const cardRefs = useRef({})                        // id → PendingCard imperative handle

  // 换页/换筛选/换视图都把选中清零，避免指向已经不在场的卡。
  useEffect(() => { setSelIdx(-1) }, [partition.org_key, partition.project_key, stage, status, view])

  useEffect(() => {
    if (!items.length) return
    const onKey = (e) => {
      const t = e.target
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (document.querySelector('.modal-overlay')) return   // 确认框打开时让位给对话框
      if (e.key === 'j' || e.key === 'k') {
        e.preventDefault()
        setSelIdx((i) => {
          const next = e.key === 'j' ? Math.min(i + 1, items.length - 1) : Math.max(i - 1, 0)
          cardRefs.current[items[next]?.id]?.scrollIntoView?.({ block: 'nearest' })
          return next
        })
      } else if (e.key === '1' || e.key === '2' || e.key === '3') {
        const it = items[selIdx]
        if (!it) return
        e.preventDefault()
        if (e.key === '1') cardRefs.current[it.id]?.approve?.()
        else if (e.key === '2') cardRefs.current[it.id]?.reject?.()
        else cardRefs.current[it.id]?.adopt?.()
      } else if (e.key === 'Escape') {
        setSelIdx(-1)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [items, selIdx])
  // ---- B5-① / B5-② end ----

  const renderCard = (it) => (
    <PendingCard key={it.id} ref={(h) => { if (h) cardRefs.current[it.id] = h; else delete cardRefs.current[it.id] }}
                 it={it} action={action} canDecide={it.status === 'pending'}
                 sections={sections} selected={items[selIdx]?.id === it.id} />
  )

  return (
    <div>
      {err && <div className="alert alert-error">{err}</div>}
      <div className="alert alert-info">
        这里排队的都是<b>系统自己拿不准的裁决</b>：<Term en="route">路由判定</Term> = 模型给素材分类
        （<Term en="observation">观察线索</Term> / <Term en="atomic_fact">零碎事实</Term> / 丢弃）没把握；
        选栏位 = 分类定了但没匹配上受控栏位；查重 = 扫描发现两条已有记忆高度相似。
        批准 = 按卡片下方字段写入记忆库，否决 = 这条线索永久出局。术语悬停都有解释。
      </div>
      <div className="card row">
        <div className="field">
          <label><Term f="stage" /></label>
          <select value={stage} onChange={(e) => setStage(e.target.value)}>
            <option value="">全部</option>
            {enumOptions('stage').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
          </select>
        </div>
        <div className="field">
          <label><Term f="status" /></label>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">全部</option>
            {enumOptions('pending_status').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
          </select>
        </div>
        <div className="spacer" />
        <span className="muted fs-sm">共 {data?.total ?? 0} 条</span>
        {/* B5-① 聚簇：相近内容归一堆，一批批裁。 */}
        <button className="btn" onClick={() => setView((v) => (v === 'list' ? 'cluster' : 'list'))}>
          {view === 'list' ? '按栏位聚簇' : '切回列表'}
        </button>
        <button className="btn" onClick={reload} disabled={loading}>
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>
      {items.length > 0 && (
        <div className="muted fs-sm" style={{ marginBottom: 8 }}>
          键盘流：<code>j</code>/<code>k</code> 选卡 · <code>1</code> 批准 · <code>2</code> 否决 · <code>3</code> 采用原判 · <code>Esc</code> 取消选中
        </div>
      )}

      {items.length === 0 && !loading && (
        <EmptyState
          title={status === 'pending' ? '没有待确认的线索' : `没有${enumItem('pending_status', status).zh}的待办`}
          hint={status === 'pending' ? '系统拿不准的判定会在这里排队等人裁决。' : '换个状态筛选看看。'}
        />
      )}

      {view === 'cluster'
        ? <ClusteredCards items={items} renderCard={renderCard} />
        : items.map(renderCard)}
    </div>
  )
}

// 栏位键 → 「中文名（key）」；词表里没有的键原样显示（后端新增栏位不坏 UI）。
const secLabel = (sections, key) => {
  const s = sections.find((x) => x.section_key === key)
  return s ? `${s.label}（${key}）` : key
}

const PendingCard = forwardRef(function PendingCard({ it, action, canDecide, sections, selected }, ref) {
  const s = it.suggestion || {}
  // 近重复合并卡（stage=dedup）：裁决只有两个选项——合并（默认保留 A 的文案，可改）或"不是重复"。
  const isMerge = s.kind === 'merge_pair'
  // 裁决草稿：预选模型给的 kind（后端已把 kind 预选成 observation），人一眼可改回。
  const [draft, setDraft] = useState({
    kind: s.kind || 'observation',
    content: s.content || it.raw_content || '',
    scope: s.scope || '',
    subject: s.subject || '',
    role: s.role || '',
    section_key: s.section_key || '',
    keywords: (s.keywords || []).join(', '),
    why: s.why || '',
  })
  const [note, setNote] = useState('')
  const { confirm, dialog } = useConfirm()
  const toast = useToast()
  const set = (k) => (e) => setDraft((d) => ({ ...d, [k]: e.target.value }))

  const approve = async () => {
    if (isMerge) {
      // 合并卡只带 kind + 文案；其余字段对合并没有意义，带上反而误导后端。
      const content = (draft.content ?? '').toString().trim()
      const resolution = content ? { kind: 'merge_pair', content } : { kind: 'merge_pair' }
      const ok = await action.run(() => api.approvePending(it.id, resolution))
      if (ok) toast.success(`已合并 #${it.id} 对应的两条记忆`)
      return
    }
    // 只带人填了的字段；空的交给后端从模型 suggestion 补（exclude_none）。
    const resolution = {}
    for (const [k, v] of Object.entries(draft)) {
      const val = (v ?? '').toString().trim()
      if (!val) continue
      resolution[k] = k === 'keywords'
        ? val.split(',').map((x) => x.trim()).filter(Boolean)
        : val
    }
    const ok = await action.run(() => api.approvePending(it.id, resolution))
    if (ok) toast.success(`已批准 #${it.id}，写入记忆库`)
  }
  // 否决不可逆（线索直接出局），补确认；批准高频低风险不补。
  const reject = async () => {
    const sure = await confirm({
      title: isMerge ? `否决配对 #${it.id}？` : `否决线索 #${it.id}？`,
      body: isMerge
        ? '标记这两条记忆不是重复，之后查重扫描不会再拿它们来问。'
        : '否决后这条线索不会再进记忆库。理由会留档。',
      danger: true,
      okText: '否决',
    })
    if (!sure) return
    const ok = await action.run(() => api.rejectPending(it.id, note.trim() || undefined))
    if (ok) toast.success(`已否决 #${it.id}`)
  }

  const atomic = draft.kind === 'atomic_fact'

  // B5-② 键盘流：主组件持 ref 调这三个动作；adopt = 把类型改回模型原判（同"采用原判"按钮）。
  useImperativeHandle(ref, () => ({
    approve,
    reject,
    adopt: () => { if (canDecide && s.model_kind) setDraft((d) => ({ ...d, kind: s.model_kind })) },
  }))

  return (
    <div className={selected ? 'card is-active' : 'card'}>
      {dialog}
      <div className="row">
        <Badge enum="stage" value={it.stage} />
        <Badge enum="pending_status" value={it.status} />
        {it.project_key && <span className="meta">@ {it.project_key}</span>}
        <div className="spacer" />
        <span className="meta">#{it.id} · {fmtDateTime(it.created_at)}</span>
      </div>

      {it.status === 'failed' && it.last_error && (
        <div className="alert alert-error mt-8">落库失败：{it.last_error}</div>
      )}

      <div className="my-8">
        <div className="meta">原文：</div>
        <div className="diff-new">{it.raw_content}</div>
      </div>

      {isMerge && s.memory_ids?.length === 2 && (
        <div className="my-8">
          <div className="meta">
            疑似重复的两条记忆{typeof s.similarity === 'number' && `（重合度 ${(s.similarity * 100).toFixed(1)}%）`}：
            批准后保留 A（可改文案），B 会被软删除。
          </div>
          <div className="diff-new">A · #{s.memory_ids[0]}：{s.content_a}</div>
          <div className="diff-old">B · #{s.memory_ids[1]}：{s.content_b}</div>
        </div>
      )}

      {it.reason && (
        <div className="my-8">
          <div className="meta">为什么拿不准：</div>
          <Badge tone="warning" title={it.reason}>{reasonZh(it.reason)}</Badge>
          {typeof s.confidence === 'number' && (
            <span className="meta"> · 模型置信度 {s.confidence.toFixed(2)}</span>
          )}
          {s.model_kind && (
            <span className="meta">
              {' · '}模型原判 {enumItem('kind', s.model_kind).zh}
              {/* §5.4 说好的"一眼改回"落成真按钮：预选 kind 与模型原判不一致时可直接采用 */}
              {canDecide && draft.kind !== s.model_kind && (
                <button className="btn sm" style={{ marginLeft: 6 }}
                        title={`把类型改回模型原判（${enumItem('kind', s.model_kind).zh}）`}
                        onClick={() => setDraft((d) => ({ ...d, kind: s.model_kind }))}>
                  采用原判
                </button>
              )}
            </span>
          )}
        </div>
      )}

      {s.why && (
        <div className="my-8">
          <div className="meta">模型的理由：</div>
          <div className="muted">{s.why}</div>
        </div>
      )}

      {s.section_runner_up && (
        <div className="meta my-4">
          栏位次选：{secLabel(sections, s.section_runner_up)}
          {typeof s.section_score === 'number' && ` · 命中分 ${s.section_score.toFixed(2)}`}
        </div>
      )}

      {canDecide && (
        <>
          {!isMerge && (
            <div className="row mt-10">
              <div className="field">
                <label><Term f="kind" /></label>
                <select value={draft.kind} onChange={set('kind')}>
                  {enumOptions('kind').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
                </select>
              </div>
              {atomic && (
                <>
                  <div className="field">
                    <label><Term f="scope" /></label>
                    <select value={draft.scope} onChange={set('scope')}>
                      <option value="">—</option>
                      {enumOptions('scope').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
                    </select>
                  </div>
                  <div className="field">
                    <label><Term f="subject" /></label>
                    <select value={draft.subject} onChange={set('subject')}>
                      <option value="">—</option>
                      {enumOptions('subject').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
                    </select>
                  </div>
                  <div className="field">
                    <label><Term f="role" /></label>
                    <select value={draft.role} onChange={set('role')}>
                      <option value="">—</option>
                      {enumOptions('role').map((o) => <option key={o.value} value={o.value}>{o.zh}</option>)}
                    </select>
                  </div>
                </>
              )}
              <div className="field">
                <label><Term f="section_key" /></label>
                <select value={draft.section_key} onChange={set('section_key')}>
                  <option value="">—（未匹配到栏位）</option>
                  {sections.map((x) => (
                    <option key={x.section_key} value={x.section_key}>
                      {x.label}（{x.section_key}）
                    </option>
                  ))}
                  {draft.section_key && !sections.some((x) => x.section_key === draft.section_key) && (
                    <option value={draft.section_key}>{draft.section_key}（词表外）</option>
                  )}
                </select>
              </div>
            </div>
          )}

          {atomic && (
            <div className="alert alert-warning mt-8">
              ★ 存成零碎事实（atomic_fact）就只靠相似度召回，<b>永远不会被主动注入</b>。
              如果这其实是一条该每次都提醒你的规矩，请改回 observation（进成文规范流水线）。
            </div>
          )}

          {isMerge ? (
            <div className="field mt-10">
              <label>合并后的文案（默认保留 A，可改成任何合并写法）</label>
              <textarea value={draft.content} onChange={set('content')} />
            </div>
          ) : (
            <>
              <textarea value={draft.content} onChange={set('content')} />

              <div className="row">
                <div className="field flex-1">
                  <label><Term f="keywords" />（逗号分隔）</label>
                  <input className="input w-full" value={draft.keywords} onChange={set('keywords')} placeholder=" infant-ticket, 出票, GDS" />
                </div>
              </div>
            </>
          )}

          <div className="row mt-10">
            <button className="btn green" onClick={approve} disabled={action.busy}>批准</button>
            <input className="input" value={note} onChange={(e) => setNote(e.target.value)}
                   placeholder="否决理由（可选）" style={{ flex: 1, minWidth: 160 }} />
            <button className="btn red" onClick={reject} disabled={action.busy}>否决</button>
          </div>
        </>
      )}
    </div>
  )
})

// B5-① 聚簇：按（stage, 栏位）归堆，同堆内容相近，一眼一批。
const clusterKey = (it) => `${it.stage || '—'}｜${it.suggestion?.section_key || '—'}`

function ClusteredCards({ items, renderCard }) {
  const groups = []
  const index = new Map()
  for (const it of items) {
    const key = clusterKey(it)
    if (!index.has(key)) { index.set(key, groups.length); groups.push({ key, stage: it.stage, section: it.suggestion?.section_key, items: [] }) }
    groups[index.get(key)].items.push(it)
  }
  return groups.map((g) => (
    <div key={g.key}>
      <div className="row mt-14" style={{ marginBottom: 8 }}>
        <Badge enum="stage" value={g.stage} />
        <span className="meta">{g.section ? `栏位 ${g.section}` : '未匹配栏位'}</span>
        <span className="badge">{g.items.length} 条</span>
      </div>
      {g.items.map(renderCard)}
    </div>
  ))
}
