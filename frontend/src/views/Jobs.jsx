import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useFetch } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'
import { useToast } from '../components/Toast.jsx'
import { fmtDateTime } from '../utils/format.js'

// 任务队列页（jobs 表）：后台任务进度可视化——回填跑到哪、谁在跑、谁失败了。
// ★ 全局视角不按 org 分区（队列是系统级的）。有活任务（pending/running>0）时
//   每 10s 自动刷新，全部安静后停止轮询，不空转。

const TONE = { running: 'success', pending: 'warn', failed: 'danger', done: undefined, cancelled: undefined }
const FILTERS = [
  { value: '', label: '全部' },
  { value: 'running', label: 'running' },
  { value: 'pending', label: 'pending' },
  { value: 'done', label: 'done' },
  { value: 'failed', label: 'failed' },
]

const shortId = (s) => (s ? `${s.slice(0, 8)}…` : '—')

export default function Jobs() {
  const [status, setStatus] = useState('')
  const [expanded, setExpanded] = useState(null)
  const { data, error, loading, reload } = useFetch(
    () => api.listJobs({ status: status || undefined, limit: 200 }),
    [status],
  )
  const live = data ? (data.summary.pending || 0) + (data.summary.running || 0) : 0

  useEffect(() => {
    if (!live) return undefined
    const t = setInterval(reload, 10000)
    return () => clearInterval(t)
  }, [live, reload])

  return (
    <div>
      {error && <div className="alert alert-error">{error}</div>}
      <div className="card row">
        <span className="section-label">后台任务队列 · jobs</span>
        <span className="meta">route / write_facts / summarize · worker 后台消费</span>
        <div className="spacer" />
        {live > 0 && <Badge tone="warn">每 10s 自动刷新</Badge>}
        <button className="btn" onClick={reload} disabled={loading}>
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      {data && <QueueBar summary={data.summary} />}

      <WorkerControl />

      <McpControl />

      <div className="card row">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            className={status === f.value ? 'btn primary' : 'btn'}
            onClick={() => setStatus(f.value)}
          >
            {f.label}
            {f.value && data?.summary?.[f.value] ? ` (${data.summary[f.value]})` : ''}
          </button>
        ))}
      </div>

      {data && data.items.length === 0 && <EmptyState title="没有符合条件的任务" />}
      {data && data.items.length > 0 && (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>#</th><th>任务</th><th>状态</th><th>会话</th><th>项目</th>
                <th className="num">条数</th><th className="num">重试</th>
                <th>创建</th><th>开始</th><th>结束</th><th>错误</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((j) => (
                <React.Fragment key={j.id}>
                  <tr className={expanded === j.id ? 'row-selected' : undefined}
                      style={{ cursor: 'pointer' }}
                      onClick={() => setExpanded(expanded === j.id ? null : j.id)}
                      title="点击展开/收起执行过程">
                    <td className="mono">{j.id}</td>
                    <td><code>{j.kind}</code></td>
                    <td>
                      <Badge tone={TONE[j.status]}>{j.status}</Badge>
                      {j.status === 'running' && j.progress?.windows_total ? (
                        <Badge tone="info">窗口 {j.progress.window}/{j.progress.windows_total}</Badge>
                      ) : null}
                    </td>
                    <td className="mono" title={j.session_id || ''}>{shortId(j.session_id)}</td>
                    <td className="meta">{j.project_key || j.org_key || '—'}</td>
                    <td className="num">{j.n_items || ''}</td>
                    <td className="num">{j.attempts}/{j.max_attempts}</td>
                    <td className="meta">{fmtDateTime(j.created_at)}</td>
                    <td className="meta">{fmtDateTime(j.started_at)}</td>
                    <td className="meta">{fmtDateTime(j.finished_at)}</td>
                    <td className="meta" style={{ maxWidth: 260, wordBreak: 'break-all' }}>
                      {j.last_error || ''}
                    </td>
                  </tr>
                  {expanded === j.id && (
                    <tr>
                      <td colSpan={11} style={{ background: 'var(--bg-2)' }}>
                        <JobDetail jobId={j.id} running={j.status === 'running'} />
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// 后台 worker 启停（★ 独立进程，后端代管 pid）。列表 10s 轮询；启动带上限校验，
// 停止要确认（杀进程）。历史回填已按用户决定停掉，这里只服务增量写路径。
function WorkerControl() {
  const [w, setW] = useState(null)
  const [err, setErr] = useState(null)
  const [count, setCount] = useState(2)
  const [busy, setBusy] = useState(false)
  const { confirm, dialog } = useConfirm()
  const toast = useToast()

  const load = useCallback(() => {
    api.listWorkers()
      .then((d) => { setW(d); setErr(null) })
      .catch((e) => setErr(e.message || String(e)))
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [load])

  const workers = w?.workers || []
  const alive = workers.filter((x) => x.alive)
  const max = w?.max || 4

  const start = async () => {
    setBusy(true)
    try {
      const d = await api.startWorkers(count)
      setW(d); setErr(null)
      toast.success(`已启动 ${count} 个 worker`)
    } catch (e) {
      setErr(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const stop = async () => {
    const sure = await confirm({
      title: '停止全部后台 worker？',
      body: '会终止正在消费队列的进程。未完成的 job 租约到期后可被重新领取，不会丢。',
      danger: true,
      okText: '停止全部',
    })
    if (!sure) return
    setBusy(true)
    try {
      const d = await api.stopWorkers()
      setW({ workers: d.workers, max }); setErr(null)
      toast.success(`已停止 ${d.killed.length} 个 worker`)
    } catch (e) {
      setErr(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card mb-12">
      {dialog}
      <div className="row">
        <span className="section-label">后台 worker</span>
        <span className="meta">独立进程 · 消费 route/write_facts/summarize 队列</span>
        <div className="spacer" />
        <Badge tone={alive.length ? 'success' : undefined}>
          {alive.length} / {max} 在跑
        </Badge>
      </div>

      {err && <div className="alert alert-error mt-8">{err}</div>}

      <div className="row mt-10">
        <div className="field">
          <label>启动数量</label>
          <select value={count} onChange={(e) => setCount(Number(e.target.value))} disabled={busy}>
            {Array.from({ length: Math.max(max - alive.length, 1) }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>
        <button className="btn primary" disabled={busy || alive.length >= max} onClick={start}>
          启动
        </button>
        <button className="btn red" disabled={busy || alive.length === 0} onClick={stop}>
          停止全部
        </button>
        <div className="spacer" />
        <span className="meta">上限 {max}（网关并发探测：2 稳）</span>
      </div>

      {workers.length > 0 && (
        <table className="tbl mt-10">
          <thead>
            <tr><th>槽位</th><th>PID</th><th>状态</th><th>日志</th></tr>
          </thead>
          <tbody>
            {workers.map((x) => (
              <tr key={x.slot}>
                <td className="mono">#{x.slot}</td>
                <td className="mono">{x.pid}</td>
                <td>{x.alive ? <Badge tone="success">alive</Badge> : <Badge>dead</Badge>}</td>
                <td className="meta mono" style={{ maxWidth: 420, wordBreak: 'break-all' }} title={x.log}>
                  {x.log}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// MCP 接入探视（integrations/mcp-server/mem_mcp.py）。★ 故意没有"启动"按钮：
// stdio MCP 是 AI 客户端（claude.exe 等）在会话里按需拉起的子进程，服务端代启
// 只会造出无主进程（cwd 归属也是错的）。这里回答三件事：谁在跑、能不能跑
// （脚本/包/注册）、跑得怎么样（recall_log source='mcp' 的近期活动）。
const MCP_TONE = { running: 'success', orphaned: 'warn', ready: 'info', unregistered: 'warn', broken: 'danger' }
const MCP_LABEL = {
  running: '运行中', orphaned: '孤儿残留', ready: '就绪·未连接',
  unregistered: '未注册', broken: '异常',
}

function McpControl() {
  const [st, setSt] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const [selftest, setSelftest] = useState(null)
  const { confirm, dialog } = useConfirm()
  const toast = useToast()

  const load = useCallback(() => {
    api.getMcpStatus()
      .then((d) => { setSt(d); setErr(null) })
      .catch((e) => setErr(e.message || String(e)))
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)   // 进程扫描较重（后端有 10s 缓存），轮询放宽到 15s
    return () => clearInterval(t)
  }, [load])

  const cleanOrphans = async () => {
    const sure = await confirm({
      title: '清理 MCP 孤儿进程？',
      body: '只结束父进程已退出的 mem_mcp.py 残留（客户端关了、adapter 还挂着）。'
          + '活跃会话的 MCP 不受影响。',
      danger: true,
      okText: '清理',
    })
    if (!sure) return
    setBusy(true)
    try {
      const d = await api.stopMcp(true)
      toast.success(d.killed.length ? `已清理 ${d.killed.join('、')}` : '没有孤儿可清')
      load()
    } catch (e) {
      setErr(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const runSelftest = async () => {
    setBusy(true); setSelftest(null)
    try {
      const d = await api.mcpSelftest()
      setSelftest(d)
      toast[d.ok ? 'success' : 'error'](d.ok
        ? `自检通过：5 个工具齐，${(d.latency_ms / 1000).toFixed(1)}s`
        : `自检失败：${d.error || '未知错误'}`)
      load()
    } catch (e) {
      setSelftest({ ok: false, error: e.message || String(e) })
    } finally {
      setBusy(false)
    }
  }

  const copyConfig = async () => {
    try {
      await navigator.clipboard.writeText(st?.config_snippet || '')
      toast.success('已复制，粘贴到 mcpServers 配置对象里')
    } catch {
      toast.error('复制失败（浏览器剪贴板权限）')
    }
  }

  const procs = st?.processes || []
  return (
    <div className="card mb-12">
      {dialog}
      <div className="row">
        <span className="section-label">MCP 接入 · mem_mcp</span>
        <span className="meta">stdio · 由 AI 客户端按会话拉起，服务端不代启停</span>
        <div className="spacer" />
        {st && <Badge tone={MCP_TONE[st.verdict]}>{MCP_LABEL[st.verdict] || st.verdict}</Badge>}
        <button className="btn" onClick={load} disabled={busy}>刷新</button>
      </div>

      {err && <div className="alert alert-error mt-8">{err}</div>}

      {st && (
        <>
          <div className="row mt-10 meta">
            <span>脚本 <span className="mono" title={st.script.path}>{st.script.exists ? '✓' : '✗ 缺失'}</span></span>
            <span>mcp 包 {st.package.installed ? `✓ v${st.package.version}` : '✗ 未安装'}</span>
            {st.activity.recalls_7d !== null ? (
              <span>近 7 天 MCP 召回 <b>{st.activity.recalls_7d}</b> 次
                {st.activity.last_recall_at ? ` · 最近 ${fmtDateTime(st.activity.last_recall_at)}` : ''}
              </span>
            ) : (
              <span>近 7 天 MCP 召回 —（查不到）</span>
            )}
          </div>

          <div className="row mt-8 meta">
            {st.registration.map((r) => (
              <span key={r.path} title={r.path}>
                {r.path.split(/[\\/]/).pop()}：{r.registered
                  ? (r.points_here ? '已注册 ✓' : '已注册（指向别处）')
                  : '未注册'}
              </span>
            ))}
            <div className="spacer" />
            <button className="btn" onClick={copyConfig}>复制接入配置</button>
            <button className="btn" onClick={runSelftest} disabled={busy}>连接自检</button>
            <button className="btn red" onClick={cleanOrphans}
                    disabled={busy || !st.orphan_count}>
              清理孤儿{st.orphan_count ? ` (${st.orphan_count})` : ''}
            </button>
          </div>

          {procs.length > 0 && (
            <table className="tbl mt-10">
              <thead>
                <tr><th>PID</th><th>父进程</th><th>启动时间</th><th>状态</th></tr>
              </thead>
              <tbody>
                {procs.map((p) => (
                  <tr key={p.pid}>
                    <td className="mono">{p.pid}</td>
                    <td className="mono">{p.parent_name || '—'}{p.parent_pid ? ` (${p.parent_pid})` : ''}</td>
                    <td className="meta">{fmtDateTime(p.started_at)}</td>
                    <td>
                      {p.orphan
                        ? <Badge tone="warn">孤儿·父进程已退出</Badge>
                        : <Badge tone="success">活跃</Badge>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {selftest && (
            <div className={`alert ${selftest.ok ? 'alert-success' : 'alert-error'} mt-10`}>
              自检{selftest.ok ? '通过' : '失败'}
              {selftest.latency_ms != null && ` · ${(selftest.latency_ms / 1000).toFixed(1)}s`}
              {selftest.tools ? ` · ${selftest.tools.join(', ')}` : ''}
              {selftest.error ? ` · ${selftest.error}` : ''}
            </div>
          )}

          <div className="meta mt-8">{st.note}</div>
        </>
      )}
    </div>
  )
}

// 队列堆叠进度条：一眼看清回填整体进度（done 占比 = 完成率）。
function QueueBar({ summary }) {  const parts = ['done', 'running', 'pending', 'failed', 'cancelled']
    .map((k) => ({ k, n: summary[k] || 0 }))
    .filter((p) => p.n > 0)
  const total = parts.reduce((a, p) => a + p.n, 0)
  if (!total) return null
  const COLOR = {
    done: 'var(--q-done)', running: 'var(--q-running)', pending: 'var(--q-pending)',
    failed: 'var(--q-failed)', cancelled: 'var(--q-cancelled)',
  }
  return (
    <div className="card mb-12">
      <div className="row meta">
        <span>队列进度：<b>{summary.done || 0}</b> / {total} 完成</span>
        <div className="spacer" />
        {parts.map((p) => <span key={p.k}>{p.k} {p.n}　</span>)}
      </div>
      <div style={{ display: 'flex', height: 14, borderRadius: 7, overflow: 'hidden', marginTop: 8 }}>
        {parts.map((p) => (
          <div key={p.k} title={`${p.k}: ${p.n}`}
               style={{ width: `${(p.n / total) * 100}%`, background: COLOR[p.k] }} />
        ))}
      </div>
    </div>
  )
}

// 执行过程下钻（点列表行展开）：逐窗口进度条 + 归属本 job 的 LLM 调用时间线。
// ★ running 时跟列表同频 10s 刷新；升级（migration 0004）之前领的任务没有
//   job_id 归属，时间线为空——明确说明，别让用户以为坏了。
function JobDetail({ jobId, running }) {
  const [detail, setDetail] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () => api.getJob(jobId)
      .then((d) => { if (alive) { setDetail(d); setErr(null) } })
      .catch((e) => { if (alive) setErr(e.message || String(e)) })
    load()
    const t = running ? setInterval(load, 10000) : null
    return () => { alive = false; if (t) clearInterval(t) }
  }, [jobId, running])

  if (err) return <div className="alert alert-error">{err}</div>
  if (!detail) return <div className="meta my-8">加载中…</div>
  const j = detail.job
  if (!j) return <div className="meta my-8">任务不存在</div>

  const prog = j.progress
  const doneWin = prog ? Math.max(prog.window - 1, 0) : 0
  return (
    <div style={{ padding: '10px 12px' }}>
      <div className="row meta">
        <span>会话 <span className="mono">{j.session_id || '—'}</span></span>
        <span>项目 {j.project_key || j.org_key || '—'}</span>
        <span>重试 {j.attempts}/{j.max_attempts}</span>
        <span>租约至 {fmtDateTime(j.lease_until)}</span>
        {running && <Badge tone="warn">每 10s 刷新</Badge>}
      </div>
      {prog?.windows_total ? (
        <div className="my-8">
          <div className="meta">
            窗口进度：<b>{doneWin}</b> / {prog.windows_total} 完成（正在跑第 {prog.window} 个）
          </div>
          <div style={{ height: 8, borderRadius: 4, background: 'var(--bg-3)', marginTop: 4 }}>
            <div style={{
              width: `${(doneWin / prog.windows_total) * 100}%`,
              height: '100%', borderRadius: 4, background: 'var(--q-done)',
            }} />
          </div>
        </div>
      ) : (
        <div className="meta my-8">暂无逐窗口进度（write_facts 不分窗口；或任务在本功能上线前领取）</div>
      )}
      {detail.calls.length === 0 ? (
        <div className="meta">本任务没有归属的 LLM 调用记录（升级前领取的任务不记 job_id）</div>
      ) : (
        <table className="tbl my-8">
          <thead>
            <tr>
              <th>时间</th><th>调用</th><th>结果</th><th className="num">in tok</th>
              <th className="num">out tok</th><th className="num">耗时</th><th>错误</th>
            </tr>
          </thead>
          <tbody>
            {detail.calls.map((c) => (
              <tr key={c.id}>
                <td className="meta">{fmtDateTime(c.created_at)}</td>
                <td><code>{c.task}</code></td>
                <td>{c.ok ? <Badge tone="success">ok</Badge> : <Badge tone="danger">fail</Badge>}</td>
                <td className="num">{Number(c.input_tokens).toLocaleString()}</td>
                <td className="num">{Number(c.output_tokens).toLocaleString()}</td>
                <td className="num">{(c.latency_ms / 1000).toFixed(1)}s</td>
                <td className="meta" style={{ maxWidth: 300, wordBreak: 'break-all' }}>{c.error || ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {j.last_error && (
        <div className="alert alert-error" style={{ whiteSpace: 'pre-wrap' }}>{j.last_error}</div>
      )}
    </div>
  )
}
