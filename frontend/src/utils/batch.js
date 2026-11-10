/**
 * 串行批量执行器：进度回调 + 汇总结果。
 *
 * ★ 语义与现行代码一致：**出错即停，不跳过继续**（LivingDocs/Attribution
 * 两处注释明确「别改一半」——批量回溯归属改一半比停下更危险）。
 *
 * @param {Array} items 待处理项
 * @param {(item, index) => Promise<any>} fn 每项的执行函数
 * @param {(done: number, total: number) => void} [onProgress] 进度回调（0-based 完成数）
 * @returns {Promise<{ok: number, failed: number, total: number, firstError: string|null}>}
 */
export async function runBatch(items, fn, onProgress) {
  let ok = 0
  let firstError = null

  for (let i = 0; i < items.length; i++) {
    try {
      await fn(items[i], i)
      ok++
    } catch (e) {
      firstError = e?.message || String(e)
      onProgress?.(ok, items.length)
      break
    }
    onProgress?.(ok, items.length)
  }

  return { ok, failed: items.length - ok, total: items.length, firstError }
}
