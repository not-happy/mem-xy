import React from 'react'

/**
 * JSON 值的人话渲染器（替代 JSON.stringify 直出）：
 *   对象 → .kv-list 的 k: v 行；数组 → 顿号连接；其他 → 原样字符串。
 *   原始 JSON 永远进 title，悬停可看。
 */
export default function KvList({ value, className }) {
  if (value === null || value === undefined) return null
  const raw = typeof value === 'string' ? value : JSON.stringify(value)

  if (Array.isArray(value)) {
    return <span className={className} title={raw}>{value.length ? value.join('、') : '（空）'}</span>
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value)
    if (entries.length === 0) return <span className={className} title={raw}>（空）</span>
    return (
      <span className={`kv-list ${className || ''}`} title={raw}>
        {entries.map(([k, v]) => (
          <span className="kv" key={k}>
            <b>{k}:</b>
            <span>{v === null || v === undefined ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
          </span>
        ))}
      </span>
    )
  }
  return <span className={className} title={raw}>{String(value)}</span>
}
