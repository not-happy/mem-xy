import React from 'react'

/**
 * 空态占位：标题 + 提示 + 可选动作按钮。替换 13 处 .notice 裸文本空态。
 *   <EmptyState title="没有待确认的线索" hint="队列空了就是好消息" action={<button …/>} />
 */
export default function EmptyState({ title, hint, action }) {
  return (
    <div className="empty-state">
      <div className="empty-title">{title}</div>
      {hint ? <div className="empty-hint">{hint}</div> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  )
}
