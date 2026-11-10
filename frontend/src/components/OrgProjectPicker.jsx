import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useDebounced } from '../hooks/useDebounced.js'
import Term from './Term.jsx'

/**
 * 顶栏分区选择器：org 下拉（选项来自 GET /v1/orgs，**失败降级回文本输入**）
 * + project 输入（300ms 防抖后才向上报，修「每敲一字符全页重挂载」）。
 *
 * 当前 org 值不在列表里时（如默认的 'dida' 尚未加载/已删除），作为额外选项保留，
 * 不丢用户当前分区。disabled = 不消费分区的页面（归属/组织/词表/重跑/设置）置灰。
 */
export default function OrgProjectPicker({ org, project, onOrg, onProject, disabled }) {
  const [orgs, setOrgs] = useState(null)   // null = 加载中或已失败
  const [failed, setFailed] = useState(false)
  const [projectText, setProjectText] = useState(project || '')

  useEffect(() => {
    let alive = true
    api.getOrgs()
      .then((d) => { if (alive) setOrgs(d?.orgs || []) })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [])

  // 外部 project 变化（如重置）时同步本地输入
  useEffect(() => { setProjectText(project || '') }, [project])

  const debouncedProject = useDebounced(projectText, 300)
  useEffect(() => {
    if (debouncedProject !== (project || '')) onProject(debouncedProject)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedProject])

  const knownKeys = (orgs || []).map((o) => o.org_key)
  const extra = org && !knownKeys.includes(org) ? [org] : []

  return (
    <div className={disabled ? 'picker disabled' : 'picker'} title={disabled ? '本页不按分区过滤' : undefined}>
      <div className="field">
        <label><Term f="org_key" /></label>
        {failed || !orgs ? (
          <input
            className="input sm"
            value={org}
            disabled={disabled}
            onChange={(e) => onOrg(e.target.value)}
            placeholder="dida / personal"
          />
        ) : (
          <select className="input sm" value={org} disabled={disabled} onChange={(e) => onOrg(e.target.value)}>
            {[...extra, ...knownKeys].map((k) => {
              const o = (orgs || []).find((x) => x.org_key === k)
              return <option key={k} value={k}>{o ? `${o.name}（${k}）` : k}</option>
            })}
          </select>
        )}
      </div>
      <div className="field">
        <label><Term f="project_key" /><span className="muted">（可选）</span></label>
        <input
          className="input sm"
          value={projectText}
          disabled={disabled}
          onChange={(e) => setProjectText(e.target.value)}
          placeholder="如 dida/gds-agg-processor-tiket"
        />
      </div>
    </div>
  )
}
