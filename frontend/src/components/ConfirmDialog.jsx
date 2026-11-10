import React, { useCallback, useEffect, useRef, useState } from 'react'

/**
 * promise 化确认框，替代原生 confirm()（与深色主题割裂）。
 *
 *   const { confirm, dialog } = useConfirm()
 *   if (!(await confirm({ title: '丢弃 run #3？', body: '丢弃后不能再落地。', danger: true }))) return
 *   …执行危险操作…
 *   return <div>…{dialog}</div>   // dialog 节点由视图渲染
 *
 * ESC / 点遮罩 = 取消；danger 时确认键红色；打开即 focus 确认键。
 */
export function useConfirm() {
  const [opts, setOpts] = useState(null)
  const resolveRef = useRef(null)

  const confirm = useCallback((o) => {
    return new Promise((resolve) => {
      resolveRef.current = resolve
      setOpts(o || {})
    })
  }, [])

  const close = useCallback((result) => {
    setOpts(null)
    if (resolveRef.current) {
      resolveRef.current(result)
      resolveRef.current = null
    }
  }, [])

  const dialog = opts ? <ConfirmDialog opts={opts} onClose={close} /> : null
  return { confirm, dialog }
}

function ConfirmDialog({ opts, onClose }) {
  const okRef = useRef(null)

  useEffect(() => {
    okRef.current?.focus()
    const onKey = (e) => {
      if (e.key === 'Escape') onClose(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(false) }}>
      <div className="modal" role="dialog" aria-modal="true">
        <div className="modal-title">{opts.title || '确认操作'}</div>
        {opts.body ? <div className="modal-body">{opts.body}</div> : null}
        <div className="modal-actions">
          <button className="btn" onClick={() => onClose(false)}>取消</button>
          <button
            ref={okRef}
            className={opts.danger ? 'btn danger' : 'btn primary'}
            onClick={() => onClose(true)}
          >
            {opts.okText || '确认'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default ConfirmDialog
