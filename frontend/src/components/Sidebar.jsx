import React from 'react'

/**
 * 左侧分组导航（数据驱动）。三组 = 用户拍板的 IA：
 *   待办审核（高频）/ 记忆内容（中频）/ 系统维护（低频）
 * counts: { [tabKey]: number }，>0 时显示红点计数（可缺省）。
 */
export default function Sidebar({ groups, tab, onNavigate, counts }) {
  return (
    <aside className="sidebar">
      <div className="brand">mem-xy <span className="muted">记忆控制台</span></div>
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
                <span className="count badge tone-danger">{counts[it.key]}</span>
              ) : null}
            </button>
          ))}
        </nav>
      ))}
    </aside>
  )
}
