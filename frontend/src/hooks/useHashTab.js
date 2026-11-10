import { useCallback, useEffect, useState } from 'react'

/**
 * tab ↔ location.hash 双向同步（手写 ~20 行，不引入 react-router）。
 * 刷新页面直达当前 tab（#/memories），也支持前进/后退。
 *
 * @param {string} defaultTab hash 为空/非法时的兜底 tab
 * @param {string[]} valid 合法 tab key 列表
 * @returns {[string, (t: string) => void]}
 */
export function useHashTab(defaultTab, valid) {
  const read = useCallback(() => {
    const h = window.location.hash.replace(/^#\/?/, '')
    return valid.includes(h) ? h : defaultTab
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultTab, valid.join('|')])

  const [tab, setTabState] = useState(read)

  useEffect(() => {
    const onHashChange = () => setTabState(read())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [read])

  const setTab = useCallback((t) => {
    if (window.location.hash !== `#/${t}`) window.location.hash = `#/${t}`
    setTabState(t)
  }, [])

  return [tab, setTab]
}
