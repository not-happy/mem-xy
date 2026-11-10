import { useCallback, useEffect, useRef, useState } from 'react'

// 三个视图都要「拉列表 + 记录 error + 手动刷新」，抽出来省得重复三份。
// fn 是 () => Promise<data>；deps 变化时自动重拉。
// ★ stale 守卫：快速切筛选时旧请求晚归也不许覆盖新结果（seq 单调递增，只认最后一次）。
export function useFetch(fn, deps) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const seqRef = useRef(0)

  const reload = useCallback(async () => {
    const seq = ++seqRef.current
    setLoading(true)
    setError(null)
    try {
      const d = await fn()
      if (seq === seqRef.current) setData(d)
    } catch (e) {
      if (seq === seqRef.current) setError(e.message || String(e))
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => { reload() }, [reload])

  return { data, error, loading, reload, setData }
}

// 写操作（编辑/采纳/否决）共用：跑一个 promise，成功回调后刷新列表，失败把消息冒出来。
export function useAction(reload) {
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const run = useCallback(async (promiseFn) => {
    setBusy(true)
    setError(null)
    try {
      const out = await promiseFn()
      await reload()
      return out
    } catch (e) {
      setError(e.message || String(e))
      return null
    } finally {
      setBusy(false)
    }
  }, [reload])

  return { run, error, busy, setError }
}
