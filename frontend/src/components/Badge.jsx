import React from 'react'
import { enumItem } from '../i18n/dict.js'

/**
 * 语义徽章。两种用法：
 *   <Badge enum="pending_status" value={it.status} />   枚举驱动：自动中文 + tone + title=英文原值
 *   <Badge tone="info">已选 {n} 条</Badge>               手动模式：装饰性计数等
 *
 * ★ 类名 = tone-*，枚举值只进 title——不再「类名=枚举值」（approved/expired 丢配色的根因）。
 */
export default function Badge({ enum: group, value, tone, children, title }) {
  if (group !== undefined) {
    const it = enumItem(group, value)
    // title = 英文原值 · 词典 hint（悬停即见术语解释，如「路由判定」是啥）
    const tip = [it.en != null ? String(it.en) : '', it.hint].filter(Boolean).join(' · ')
    return (
      <span className={`badge tone-${it.tone}`} title={title ?? tip}>
        {children ?? it.zh}
      </span>
    )
  }
  return (
    <span className={`badge tone-${tone || 'neutral'}`} title={title}>
      {children}
    </span>
  )
}
