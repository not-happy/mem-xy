import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import Sidebar from './components/Sidebar.jsx'
import OrgProjectPicker from './components/OrgProjectPicker.jsx'
import { ToastProvider, useToast } from './components/Toast.jsx'
import { useHashTab } from './hooks/useHashTab.js'
import Inbox from './views/Inbox.jsx'
import Pending from './views/Pending.jsx'
import LivingDocs from './views/LivingDocs.jsx'
import Proposals from './views/Proposals.jsx'
import Observations from './views/Observations.jsx'
import Memories from './views/Memories.jsx'
import Conversations from './views/Conversations.jsx'
import Attribution from './views/Attribution.jsx'
import RecallDebug from './views/RecallDebug.jsx'
import Metrics from './views/Metrics.jsx'
import Jobs from './views/Jobs.jsx'
import Orgs from './views/Orgs.jsx'
import Replay from './views/Replay.jsx'
import Sections from './views/Sections.jsx'
import Settings from './views/Settings.jsx'

/* 三组导航 = 使用频率分层：待办审核（天天看）/ 记忆内容（查阅）/ 系统维护（偶尔）。
 * key 同时是 hash 路由段（#/pending）与视图开关。 */
const NAV_GROUPS = [
  {
    title: '待办审核',
    items: [
      { key: 'inbox', label: '待办中心' },
      { key: 'pending', label: '待确认' },
      { key: 'proposals', label: '待审 diff' },
    ],
  },
  {
    title: '记忆内容',
    items: [
      { key: 'docs', label: '成文规范' },
      { key: 'observations', label: '观察池' },
      { key: 'memories', label: '记忆库' },
      { key: 'conversations', label: '会话存档' },
      { key: 'recall', label: '召回调试' },
    ],
  },
  {
    title: '系统维护',
    items: [
      { key: 'attribution', label: '归属管理' },
      { key: 'metrics', label: '指标' },
      { key: 'jobs', label: '任务队列' },
      { key: 'orgs', label: '组织管理' },
      { key: 'sections', label: '词表' },
      { key: 'replay', label: '重跑' },
      { key: 'settings', label: '设置' },
    ],
  },
]

const TAB_KEYS = NAV_GROUPS.flatMap((g) => g.items.map((it) => it.key))
const TAB_LABELS = Object.fromEntries(NAV_GROUPS.flatMap((g) => g.items.map((it) => [it.key, it.label])))
/* 这五页是全库视角，不消费 org/project 分区——选择器置灰，避免「改了没反应」的困惑。 */
const NO_PARTITION = new Set(['attribution', 'orgs', 'sections', 'replay', 'settings', 'jobs'])

export default function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  )
}

function Shell() {
  const [tab, setTab] = useHashTab('inbox', TAB_KEYS)
  // ★ 分区（org/project）在顶层管：多个视图都要按它过滤，放各自组件里会重复多份状态。
  const [org, setOrg] = useState('dida')
  const [project, setProject] = useState('')
  const health = useHealth()
  const [counts, refreshCounts] = useTodoCounts(org, project)

  // 分区变了要给子视图一个信号去重新拉数据。用 key 强制重挂，比传 useEffect 依赖简单。
  // project 由 OrgProjectPicker 内部防抖 300ms 后才上报，连敲不会每字符重挂载。
  const partition = { org_key: org || undefined, project_key: project || undefined }
  const partitionKey = `${org}|${project}`
  const noPartition = NO_PARTITION.has(tab)

  return (
    <div className="layout">
      <Sidebar groups={NAV_GROUPS} tab={tab} onNavigate={setTab} counts={counts} />
      <div className="main">
        <header className="topbar">
          <h1>{TAB_LABELS[tab]}</h1>
          <div className="spacer" />
          <OrgProjectPicker
            org={org}
            project={project}
            onOrg={setOrg}
            onProject={setProject}
            disabled={noPartition}
          />
          <HealthBadge health={health} />
        </header>

        <main className="content">
          {tab === 'inbox' && <Inbox key={partitionKey} partition={partition} onNavigate={setTab} />}
          {tab === 'pending' && <Pending key={partitionKey} partition={partition} onDecided={refreshCounts} />}
          {tab === 'proposals' && <Proposals key={partitionKey} partition={partition} onDecided={refreshCounts} />}
          {tab === 'docs' && <LivingDocs key={partitionKey} partition={partition} />}
          {tab === 'observations' && <Observations key={partitionKey} partition={partition} />}
          {tab === 'memories' && <Memories key={partitionKey} partition={partition} />}
          {tab === 'conversations' && <Conversations key={partitionKey} partition={partition} />}
          {tab === 'attribution' && <Attribution />}
          {tab === 'recall' && <RecallDebug key={partitionKey} partition={partition} />}
          {tab === 'metrics' && <Metrics key={partitionKey} partition={partition} />}
          {tab === 'jobs' && <Jobs />}
          {tab === 'orgs' && <Orgs />}
          {tab === 'sections' && <Sections />}
          {tab === 'replay' && <Replay />}
          {tab === 'settings' && <Settings />}
        </main>
      </div>
    </div>
  )
}

/* 健康 30s 轮询；状态翻转（好→坏 / 坏→好）时 toast 提醒一次，避免「页面静默变空」无人察觉。 */
function useHealth() {
  const [health, setHealth] = useState(null)
  const toast = useToast()
  const wasOkRef = useRef(null)

  useEffect(() => {
    let alive = true
    const load = () => {
      api.health()
        .then((h) => {
          if (!alive) return
          const ok = h?.status === 'ok'
          if (wasOkRef.current === true && !ok) toast.error(`后端状态劣化：${h?.status || '未知'}`)
          if (wasOkRef.current === false && ok) toast.success('后端已恢复')
          wasOkRef.current = ok
          setHealth(h)
        })
        .catch(() => {
          if (!alive) return
          if (wasOkRef.current === true) toast.error('后端不可达了（uvicorn 停了吗？）')
          wasOkRef.current = false
          setHealth({ status: 'unreachable' })
        })
    }
    load()
    const t = setInterval(load, 30000)
    return () => { alive = false; clearInterval(t) }
  }, [toast])

  return health
}

/* 侧边栏待办红点：pending 总数 + 待审 proposal 总数，60s 轮询。
 * 与 Inbox 页同源（listPending / listProposals 的 total），失败静默归 0 不干扰。
 * ★ 返回 [counts, refresh]：批准/否决成功后子视图立刻调 refresh 重拉红点，
 *   不用干等 60s 轮询（后端 list_pending 默认只算 pending+failed，裁决后 total 即时变化）。 */
function useTodoCounts(org, project) {
  const [counts, setCounts] = useState({})
  const [tick, setTick] = useState(0)
  const refresh = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    let alive = true
    const base = { org_key: org || undefined, project_key: project || undefined, limit: 1 }
    const load = () => {
      Promise.allSettled([
        api.listPending(base),
        api.listProposals({ ...base, status: 'pending' }),
      ]).then(([p, q]) => {
        if (!alive) return
        setCounts({
          pending: p.status === 'fulfilled' ? (p.value?.total || 0) : 0,
          proposals: q.status === 'fulfilled' ? (q.value?.total || 0) : 0,
        })
      })
    }
    load()
    const t = setInterval(load, 60000)
    return () => { alive = false; clearInterval(t) }
  }, [org, project, tick])

  return [counts, refresh]
}

function HealthBadge({ health }) {
  if (!health) return <span className="health">检查后端中…</span>
  if (health.status === 'unreachable') {
    return <span className="health"><span className="dot bad" />后端不可达（起 uvicorn 了吗？）</span>
  }
  const ready = health.status === 'ok'
  const emb = health.embedding || {}
  return (
    <span className="health">
      <span className={`dot ${ready ? 'ok' : 'bad'}`} />
      {ready ? 'embedding 就绪' : '模型加载中'}
      {emb.load_seconds ? ` · 冷启动 ${emb.load_seconds.toFixed(1)}s` : ''}
    </span>
  )
}
