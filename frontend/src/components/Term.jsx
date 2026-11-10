import React from 'react'
import { FIELDS } from '../i18n/dict.js'

/**
 * 术语：中文为主 + 悬停 tooltip 显英文原值（原生 title，零成本零 bug）。
 *   <Term f="org_key" />                     捷径：查 FIELDS，title = 英文键 · hint
 *   <Term en="rrf">融合分</Term>              通用：自定义中文，title = 英文
 *   <Term en="final" hint="公式…">综合分</Term> 通用 + 附加说明
 */
export default function Term({ f, en, hint, children }) {
  if (f) {
    const d = FIELDS[f]
    const zh = d?.zh || f
    const tip = [f, d?.hint || hint].filter(Boolean).join(' · ')
    return <span className="term" title={tip}>{zh}</span>
  }
  const tip = [en, hint].filter(Boolean).join(' · ')
  return <span className="term" title={tip}>{children ?? en}</span>
}
