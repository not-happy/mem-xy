import React from 'react'
import { api } from '../api.js'
import { useFetch } from '../useFetch.js'
import Badge from '../components/Badge.jsx'
import Term from '../components/Term.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { enumItem } from '../i18n/dict.js'

// 设置页（§11）：**只读**的生效配置检视器。
// ★ 不做 UI 写 config：get_settings 有进程级缓存，且线程数/端口/dsn 是启动时读一次——
//   从网页写回 config.toml 不重启根本不生效，只会让人以为「改了没反应」。改仍走文件 + 重启。
// ★ 两条安全线：token 绝不返回（只报「已配置/未配置」）、dsn 里的 password 后端已打码。
// ★ it.label / it.group 后端已下发中文（实测 2026-10），前端直通显示，不再套词典。

export default function Settings() {
  const { data, error, loading, reload } = useFetch(() => api.getSettings(), [])

  if (error) return <div className="alert alert-error">{error}</div>
  if (!data) return <EmptyState title={loading ? '加载中…' : '无数据'} />

  // 按 group 把 items 归拢，保持后端给的顺序
  const groups = []
  for (const it of data.items) {
    let g = groups.find((x) => x.name === it.group)
    if (!g) { g = { name: it.group, items: [] }; groups.push(g) }
    g.items.push(it)
  }

  return (
    <div>
      <div className="card row">
        <span className="section-label">设置 · 生效配置</span>
        <span className="meta">只读检视器</span>
        <div className="spacer" />
        <button className="btn" onClick={reload} disabled={loading}>{loading ? '加载中…' : '刷新'}</button>
      </div>

      <div className="alert alert-info">
        ★ 这一页只<b>看</b>当前生效值和它的来源层，不能在这里改。改配置请编辑
        <code className="mx-4">{data.paths.config_file}</code>后<b>重启进程</b>。
        <div className="meta mt-4">{data.restart_note}</div>
      </div>

      <div className="card-grid align-start">
        {groups.map((g) => (
          <div className="card card-col" key={g.name}>
            <div className="section-label mb-6">{g.name}</div>
            <div className="kv-list">
              {g.items.map((it) => (
                <span className="kv" key={it.key}>
                  <b title={it.key}>{it.label}</b>
                  <span className="mono">{fmt(it.value)}</span>
                  <Badge enum="settings_source" value={it.source} />
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>

      <ThinkingCard thinking={data.thinking} />
      <EndpointCard endpoints={data.endpoints} taskEndpoints={data.task_endpoints} />

      <div className="card">
        <div className="section-label mb-6">数据库连接（已打码）</div>
        <div className="row">
          <code className="fs-sm break-all">{data.dsn}</code>
          <div className="spacer" />
          <Badge enum="settings_source" value={data.dsn_source} />
        </div>
        <div className="meta mt-6">
          ★ password 由后端 <code>redact()</code> 抹掉，前端拿不到明文。
        </div>
      </div>

      <div className="card">
        <div className="section-label mb-6">配置文件</div>
        <PathRow label="config 目录" path={data.paths.config_dir} />
        <PathRow label="config.toml" path={data.paths.config_file} exists={data.paths.config_exists} />
        <PathRow label="secrets.toml" path={data.paths.secrets_file} exists={data.paths.secrets_exists} />
        <PathRow label="缓存目录" path={data.paths.cache_dir} />
        <div className="meta mt-6">
          ★ <Term f="token" /> 只走 secrets.toml 或环境变量，绝不进 config.toml、不进 DB、不经这个页面（§2.1）。
        </div>
      </div>
    </div>
  )
}

function fmt(v) {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  return String(v)
}

function ThinkingCard({ thinking }) {
  return (
    <div className="card">
      <div className="row">
        <span className="section-label">thinking 开关（按任务）</span>
        <div className="spacer" />
        <span className="meta">§9.1：路由用 thinking ON、召回不用</span>
      </div>
      <div className="row" style={{ marginTop: 8, gap: 8, flexWrap: 'wrap' }}>
        {thinking.map((t) => (
          <Badge key={t.task}
                 tone={t.enabled ? 'success' : 'neutral'}
                 title={`task=${t.task} · 来源：${enumItem('settings_source', t.source).zh}`}>
            {enumItem('thinking_task', t.task).zh}: {t.enabled ? 'ON' : 'OFF'}
          </Badge>
        ))}
      </div>
    </div>
  )
}

function EndpointCard({ endpoints, taskEndpoints }) {
  return (
    <div className="card">
      <div className="row">
        <span className="section-label">端点 token 状态</span>
        <div className="spacer" />
        <span className="meta">★ 只报有无，绝不显示 token 值</span>
      </div>
      <div className="tbl-wrap mt-8">
        <table className="tbl">
          <thead>
            <tr>
              <th><Term f="endpoint_key" /></th><th><Term f="model" /></th>
              <th>启用</th><th><Term f="token" /></th>
            </tr>
          </thead>
          <tbody>
            {endpoints.map((e) => (
              <tr key={e.endpoint_key}>
                <td className="mono">{e.endpoint_key}</td>
                <td>{e.model}</td>
                <td><span className={e.enabled ? 'bool yes' : 'bool no'}>{e.enabled ? '✓' : '✗'}</span></td>
                <td>
                  {e.token_configured
                    ? <Badge tone="success">已配置</Badge>
                    : <Badge tone="danger">未配置</Badge>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {taskEndpoints.length > 0 && (
        <div className="meta mt-6">
          任务→端点覆盖：{taskEndpoints.map((t) => `${enumItem('thinking_task', t.task).zh}→${t.endpoint}`).join('，')}
        </div>
      )}
    </div>
  )
}

function PathRow({ label, path, exists }) {
  return (
    <div className="list-row">
      <span className="muted fs-sm min-w-100">{label}</span>
      <code className="fs-sm break-all">{path}</code>
      {exists !== undefined && (
        exists ? <Badge tone="success">存在</Badge> : <Badge tone="neutral">不存在</Badge>
      )}
    </div>
  )
}
