import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'

/**
 * 轻提示（补 useAction 成功后无反馈的缺口）。
 * App 层包一个 <ToastProvider>，视图里：
 *   const toast = useToast()
 *   toast.success('已批准，写入记忆库')  / toast.error(msg) / toast.info(msg)
 * 3.5s 自动消失、最多同时堆 4 条、tone 左边条。
 * 未被 Provider 包裹时降级为 no-op（批次独立落地不炸）。
 */
const ToastContext = createContext(null)

const NOOP_TOAST = { success() {}, error() {}, info() {} }

export function useToast() {
  return useContext(ToastContext) || NOOP_TOAST
}

let nextId = 1

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const timersRef = useRef({})

  useEffect(() => {
    const timers = timersRef.current
    return () => { Object.values(timers).forEach(clearTimeout) }
  }, [])

  const dismiss = useCallback((id) => {
    setToasts((ts) => ts.filter((t) => t.id !== id))
    clearTimeout(timersRef.current[id])
    delete timersRef.current[id]
  }, [])

  const push = useCallback((tone, msg) => {
    const id = nextId++
    setToasts((ts) => [...ts, { id, tone, msg }].slice(-4))
    timersRef.current[id] = setTimeout(() => dismiss(id), 3500)
  }, [dismiss])

  const toast = useRef({
    success: (m) => push('success', m),
    error: (m) => push('danger', m),
    info: (m) => push('info', m),
  }).current

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div className="toast-host">
        {toasts.map((t) => (
          <div key={t.id} className={`toast tone-${t.tone}`} onClick={() => dismiss(t.id)} role="status">
            {t.msg}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
