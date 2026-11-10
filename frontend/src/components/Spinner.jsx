import React from 'react'

/** CSS 旋转加载环。按钮 busy 态与列表首屏加载统一用它。 */
export default function Spinner({ size = 14 }) {
  return <span className="spinner" style={{ width: size, height: size }} aria-label="加载中" />
}
