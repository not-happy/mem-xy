import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import OrgProjectPicker from './components/OrgProjectPicker.jsx'
import Drawer from './components/Drawer.jsx'
import { ToastProvider, useToast } from './components/Toast.jsx'
import { useHashTab } from './hooks/useHashTab.js'
import Pipeline from './views/Pipeline.jsx'
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

/* IA v2（单屏流水线画布）：没有侧栏、没有 15 个平级页面。主界面只有一张流水线
 * 画布（Pipeline），其余视图全部收进右侧抽屉，按流水线阶段分组：
 *   采集→会话存档；路由→观察池；双判→分歧裁决/待审 diff；写入→记忆库/成文规范/
 *   任务队列；召回→召回调试。顶栏的「质量」「系统」按钮打开低频工具抽屉。
 * key 仍是 hash 路由段（#/pending），旧链接（#/inbox、#/cockpit）落到画布。 */
const DRAWER_GROUPS = {
  conversations: {
    title: '采集 · 会话存档', tabs: [{ key: 'conversations', label: '会话存档' }],
  },
  observations: {
    title: '路由 · 观察池', tabs: [{ key: 'observations', label: '观察池' }],
  },
  pending: {
    title: '双判 · 例外裁决',
    tabs: [{ key: 'pending', label: '分歧裁决' }, { key: 'proposals', label: '待审 diff' }],
  },
  proposals: {
    title: '双判 · 例外裁决',
    tabs: [{ key: 'pending', label: '分歧裁决' }, { key: 'proposals', label: '待审 diff' }],
  },
  memories: {
    title: '写入 · 落库结果',
    tabs: [{ key: 'memories', label: '记忆库' }, { key: 'docs', label: '成文规范' }, { key: 'jobs', label: '任务队列' }],
  },
  docs: {
    title: '写入 · 落库结果',
    tabs: [{ key: 'memories', label: '记忆库' }, { key: 'docs', label: '成文规范' }, { key: 'jobs', label: '任务队列' }],
  },
  jobs: {
    title: '写入 · 落库结果',
    tabs: [{ key: 'memories', label: '记忆库' }, { key: 'docs', label: '成文规范' }, { key: 'jobs', label: '任务队列' }],
  },
  recall: {
    title: '召回 · 注入', tabs: [{ key: 'recall', label: '召回调试' }],
  },
  metrics: {
    title: '质量 · 调优', tabs: [{ key: 'metrics', label: '指标' }, { key: 'replay', label: '重跑' }],
  },
  replay: {
    title: '质量 · 调优', tabs: [{ key: 'metrics', label: '指标' }, { key: 'replay', label: '重跑' }],
  },
  attribution: {
    title: '系统',
    tabs: [
      { key: 'attribution', label: '归属管理' },
      { key: 'sections', label: '词表' },
      { key: 'orgs', label: '组织管理' },
      { key: 'settings', label: '设置' },
    ],
  },
  sections: {
    title: '系统',
    tabs: [
      { key: 'attribution', label: '归属管理' },
      { key: 'sections', label: '词表' },
      { key: 'orgs', label: '组织管理' },
      { key: 'settings', label: '设置' },
    ],
  },
  orgs: {
    title: '系统',
    tabs: [
      { key: 'attribution', label: '归属管理' },
      { key: 'sections', label: '词表' },
      { key: 'orgs', label: '组织管理' },
      { key: 'settings', label: '设置' },
    ],
  },
  settings: {
    title: '系统',
    tabs: [
      { key: 'attribution', label: '归属管理' },
      { key: 'sections', label: '词表' },
      { key: 'orgs', label: '组织管理' },
      { key: 'settings', label: '设置' },
    ],
  },
}

const DRAWER_KEYS = Object.keys(DRAWER_GROUPS)
/* 这些视图是全库视角，不消费 org/project 分区——选择器置灰，避免「改了没反应」的困惑。 */
const NO_PARTITION = new Set(['attribution', 'orgs', 'sections', 'replay', 'settings', 'jobs'])
/* 分歧裁决/待审 diff 内容宽，给更宽的抽屉。 */
const WIDE_DRAWER = new Set(['pending', 'proposals'])

export default function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  )
}

function Shell() {
  const [tab, setTab] = useHashTab('cockpit', ['cockpit', ...DRAWER_KEYS])
  // ★ 分区（org/project）在顶层管：多个视图都要按它过滤，放各自组件里会重复多份状态。
  const [org, setOrg] = useState('dida')
  const [project, setProject] = useState('')
  const health = useHealth()
  const [theme, toggleTheme] = useTheme()

  // 分区变了要给子视图一个信号去重新拉数据。用 key 强制重挂，比传 useEffect 依赖简单。
  // project 由 OrgProjectPicker 内部防抖 300ms 后才上报，连敲不会每字符重挂载。
  // partition 引用要稳（useMemo）：它是抽屉内容的记忆化依赖，引用每渲染都变就白记了。
  const partition = useMemo(
    () => ({ org_key: org || undefined, project_key: project || undefined }),
    [org, project],
  )
  const partitionKey = `${org}|${project}`

  const drawerKey = DRAWER_GROUPS[tab] ? tab : null
  const group = drawerKey ? DRAWER_GROUPS[drawerKey] : null
  const open = setTab
  const close = useCallback(() => setTab('cockpit'), [setTab])

  // 抽屉内容按 (drawerKey, 分区) 记忆化：health 心跳、主题状态这类 App 层变化
  // 不再把抽屉里的重列表（如 100 张待裁决卡）整棵拉着重渲一遍。
  const drawerContent = useMemo(
    () => (drawerKey ? renderView(drawerKey, partitionKey, partition) : null),
    [drawerKey, partitionKey, partition],
  )

  return (
    <div className="layout">
      <header className="topbar">
        <span className="brand">
          <span className="brand-dot" />
          mem-xy <span className="muted">记忆观测台</span>
        </span>
        <div className="spacer" />
        <OrgProjectPicker
          org={org}
          project={project}
          onOrg={setOrg}
          onProject={setProject}
          disabled={drawerKey != null && NO_PARTITION.has(drawerKey)}
        />
        <button
          className="btn ghost sm theme-btn"
          onClick={toggleTheme}
          title={theme === 'light' ? '切到暗色' : '切到亮色'}
        >
          {theme === 'light' ? '🌙' : '☀️'}
        </button>
        <HealthBadge health={health} />
      </header>

      <main className="content">
        <Pipeline onOpen={open} />
      </main>

      {drawerKey && (
        <Drawer
          title={group.title}
          tabs={group.tabs}
          activeTab={drawerKey}
          onTab={setTab}
          width={WIDE_DRAWER.has(drawerKey) ? 960 : 880}
          onClose={close}
        >
          {drawerContent}
        </Drawer>
      )}
    </div>
  )
}

/* 抽屉内容：视图 key → 组件。与旧版逐 tab 渲染一一对应，仅换了容器。 */
function renderView(key, partitionKey, partition) {
  switch (key) {
    case 'conversations': return <Conversations key={partitionKey} partition={partition} />
    case 'observations': return <Observations key={partitionKey} partition={partition} />
    case 'pending': return <Pending key={partitionKey} partition={partition} />
    case 'proposals': return <Proposals key={partitionKey} partition={partition} />
    case 'memories': return <Memories key={partitionKey} partition={partition} />
    case 'docs': return <LivingDocs key={partitionKey} partition={partition} />
    case 'jobs': return <Jobs />
    case 'recall': return <RecallDebug key={partitionKey} partition={partition} />
    case 'metrics': return <Metrics key={partitionKey} partition={partition} />
    case 'replay': return <Replay />
    case 'attribution': return <Attribution />
    case 'sections': return <Sections />
    case 'orgs': return <Orgs />
    case 'settings': return <Settings />
    default: return null
  }
}

/* 主题：亮色默认，切换写 localStorage('memxy-theme') 并同步 <html data-theme>。
 * 初始值由 index.html 的内联脚本在首帧前设好（防闪烁），这里只负责切与读。 */
function useTheme() {
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || 'light')
  const toggle = useCallback(() => {
    setTheme((t) => {
      const next = t === 'light' ? 'dark' : 'light'
      document.documentElement.dataset.theme = next
      try { localStorage.setItem('memxy-theme', next) } catch { /* 隐私模式忽略 */ }
      return next
    })
  }, [])
  return [theme, toggle]
}

/* 健康 30s 轮询；状态翻转（好→坏 / 坏→好）时 toast 提醒一次，避免「页面静默变空」无人察觉。 */
function useHealth() {
  const [health, setHealth] = useState(null)
  const toast = useToast()
  const wasOkRef = useRef(null)

  useEffect(() => {
    let alive = true
    let timer = null
    const load = () => {
      api.health()
        .then((h) => {
          if (!alive) return
          const ok = h?.status === 'ok'
          if (wasOkRef.current === true && !ok) toast.error(`后端状态劣化：${h?.status || '未知'}`)
          if (wasOkRef.current === false && ok) toast.success('后端已恢复')
          wasOkRef.current = ok
          // 徽章只消费 status / loaded / load_seconds：三者没变就不触发整树重渲
          // （health 里的 avg_ms_per_row 等字段每次召回都会变，不能拿它们比）。
          setHealth((prev) => (
            prev && prev.status === h?.status
            && prev.embedding?.loaded === h?.embedding?.loaded
            && prev.embedding?.load_seconds === h?.embedding?.load_seconds
              ? prev : h
          ))
        })
        .catch(() => {
          if (!alive) return
          if (wasOkRef.current === true) toast.error('后端不可达了（uvicorn 停了吗？）')
          wasOkRef.current = false
          setHealth((prev) => (prev?.status === 'unreachable' ? prev : { status: 'unreachable' }))
        })
    }
    load()
    timer = setInterval(load, 30000)
    // 标签页隐藏时停表，回前台立即补测并恢复心跳。
    const onVis = () => {
      if (document.hidden) { clearInterval(timer); timer = null }
      else { load(); if (timer == null) timer = setInterval(load, 30000) }
    }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      alive = false
      if (timer != null) clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [toast])

  return health
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
