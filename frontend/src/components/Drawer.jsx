import React, { useEffect } from 'react'

/**
 * 右侧滑出的全高抽屉：流水线画布上所有视图的新容器。
 * 头部 = 标题 + 页签（可选）+ 关闭按钮；点遮罩或 ✕ 关闭。
 * 内容由 children 提供，App 层用 key={partitionKey} 保证分区切换时重挂。
 *
 * @param {string}  title     抽屉标题（如「双判 · 例外裁决」）
 * @param {Array}   tabs      可选页签 [{ key, label }]
 * @param {string}  activeTab 当前页签 key
 * @param {(k: string) => void} onTab 切页签（App 会同步写 hash）
 * @param {number}  width     面板宽度 px，默认 880；分歧裁决等宽视图传 960
 * @param {() => void} onClose
 */
export default function Drawer({ title, tabs, activeTab, onTab, width = 880, onClose, children }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="drawer-overlay"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <aside className="drawer" style={{ width: `min(${width}px, 94vw)` }} role="dialog" aria-label={title}>
        <header className="drawer-head">
          <h2>{title}</h2>
          {tabs && tabs.length > 1 && (
            <nav className="drawer-tabs">
              {tabs.map((t) => (
                <button
                  key={t.key}
                  className={'drawer-tab' + (t.key === activeTab ? ' active' : '')}
                  onClick={() => onTab(t.key)}
                >
                  {t.label}
                </button>
              ))}
            </nav>
          )}
          <button className="btn ghost sm drawer-close" onClick={onClose} aria-label="关闭">✕</button>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  )
}
