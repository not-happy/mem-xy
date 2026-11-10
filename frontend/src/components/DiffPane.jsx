import React from 'react'

/**
 * 旧/新两栏对照。收编 Proposals/LivingDocs/Memories 的 3 份 diffPane 内联对象
 * 与 Replay 的 diffBox。语义标签（当前/提议/（新建，无原文）…）由调用方经 title 传入。
 */
export default function DiffPane({ oldTitle = '旧', newTitle = '新', oldText, newText }) {
  return (
    <div className="diff-row">
      <div className="diff-pane">
        <div className="diff-title">{oldTitle}</div>
        <div className="diff-old">{oldText == null || oldText === '' ? '（无原文）' : oldText}</div>
      </div>
      <div className="diff-pane">
        <div className="diff-title">{newTitle}</div>
        <div className="diff-new">{newText == null || newText === '' ? '（空）' : newText}</div>
      </div>
    </div>
  )
}
