// 时间格式统一出口：yyyy-MM-dd HH:mm:ss（本地时区）。
// ★ 后端回的是带时区的 ISO 串，各视图曾各自格式（Jobs 只显时分秒、其余裸 ISO），
//   现统一收口到这里。解析不了的原样返回，UI 不坏。

const p2 = (n) => String(n).padStart(2, '0')

export function fmtDateTime(s) {
  if (!s) return '—'
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return String(s)
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} `
    + `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
}
