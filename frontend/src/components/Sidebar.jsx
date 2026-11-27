import React from 'react'

/**
 * 左侧分组导航（数据驱动）+ 实时状态脚注。
 * IA（自动化优先的重构）：驾驶舱 / 例外处理（人只裁分歧）/ 记忆流水线 /
 * 质量与调优 / 系统。counts: { [tabKey]: number }>0 时显示琥珀计数。
 * status: { health, workers } → rail-foot 实时体征（后端 / embedding / worker 在岗）。
 */
export default function Sidebar({ groups, tab, onNavigate, counts, status }) {
  const workers = status?.workers
  const alive = workers ? (workers.workers || []).filter((w) => w.alive).length : null
  const max = workers?.max
  const emb = status?.health?.embedding || {}
  const embWarm = !!emb.loaded
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-dot" />
        mem·xy <span className="muted">记忆观测台</span>
      </div>
      {groups.map((g) => (
        <nav className="nav-group" key={g.title}>
          <div className="nav-group-title">{g.title}</div>
          {g.items.map((it) => (
            <button
              key={it.key}
              className={tab === it.key ? 'nav-item active' : 'nav-item'}
              onClick={() => onNavigate(it.key)}
            >
              <span>{it.label}</span>
              {counts?.[it.key] > 0 ? (
                <span className="count badge tone-warning">{counts[it.key]}</span>
              ) : null}
            </button>
          ))}
        </nav>
      ))}
      <div className="rail-foot">
        <div className="rf-row">
          <span className={`pulse-dot ${status?.health?.status === 'ok' ? '' : 'bad'}`} />
          后端 <b>{status?.health?.status === 'ok' ? '在线' : '离线'}</b>
        </div>
        <div className="rf-row">
          <span className={`pulse-dot ${embWarm ? '' : 'warn'}`} />
          embedding <b>{embWarm ? '热' : '冷'}</b>
        </div>
        <div className="rf-row">
          <span className={`pulse-dot ${alive > 0 ? '' : 'off'}`} />
          worker <b>{alive == null ? '—' : `${alive}/${max ?? '?'}`}</b>
        </div>
        <div className="rf-row"><span className="rf-tag">AUTOPILOT ON</span></div>
      </div>
    </aside>
  )
}
