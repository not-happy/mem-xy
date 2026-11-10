import React from 'react'

/**
 * 分页条：‹ 上一页 · 第 x–y 条 / 共 n 条 · 下一页 ›。
 * total ≤ pageSize 时不渲染（单页不需要分页）。page 从 0 起。
 * 已验证：全部 list 端点支持 limit/offset 且返回 total。
 */
export default function Paginator({ total, page, pageSize, onChange }) {
  if (total == null || total <= pageSize) return null

  const pages = Math.ceil(total / pageSize)
  const from = page * pageSize + 1
  const to = Math.min(total, (page + 1) * pageSize)

  return (
    <div className="paginator">
      <button className="btn sm" disabled={page <= 0} onClick={() => onChange(page - 1)}>
        ‹ 上一页
      </button>
      <span className="muted">
        第 {from}–{to} 条 / 共 {total} 条（{page + 1}/{pages} 页）
      </span>
      <button className="btn sm" disabled={page + 1 >= pages} onClick={() => onChange(page + 1)}>
        下一页 ›
      </button>
    </div>
  )
}
