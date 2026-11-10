import { useEffect, useState } from 'react'

/**
 * 值防抖：value 停止变化 ms 毫秒后才返回新值。
 * 修「分区 project 每敲一字符全页重挂载」「记忆库搜索每字符重拉」。
 */
export function useDebounced(value, ms = 300) {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])

  return debounced
}
