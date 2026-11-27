// 后端薄客户端。★ 所有请求走同源 /v1（dev 由 Vite 代理转发），出错时把
// 后端返回的 {error:{code,message}} 抛成带 status 的 Error，交给 UI 显示。

async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  let payload = null
  try {
    payload = await res.json()
  } catch {
    // 204 / 空响应体
  }
  if (!res.ok) {
    const err = payload?.error || {}
    const e = new Error(err.message || `${method} ${path} → ${res.status}`)
    e.status = res.status
    e.code = err.code
    throw e
  }
  return payload
}

function qs(params) {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') sp.set(k, v)
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export const api = {
  health: () => request('GET', '/healthz'),

  listLivingDoc: (p) => request('GET', `/v1/living-doc${qs(p)}`),
  editLivingDoc: (id, content, note) =>
    request('PUT', `/v1/living-doc/${id}`, { content, note }),
  setAuthority: (id, authority) =>
    request('PATCH', `/v1/living-doc/${id}/authority`, { authority }),
  history: (id) => request('GET', `/v1/living-doc/${id}/history`),

  listProposals: (p) => request('GET', `/v1/proposals${qs(p)}`),
  acceptProposal: (id, content) =>
    request('POST', `/v1/proposals/${id}/accept`, content ? { content } : {}),
  rejectProposal: (id) => request('POST', `/v1/proposals/${id}/reject`),

  listObservations: (p) => request('GET', `/v1/observations${qs(p)}`),
  triggerSummarize: (p) => request('POST', '/v1/observations/summarize', p || {}),
  revokeNegative: (id) => request('DELETE', `/v1/observations/${id}/negative`),

  // 待办中心 / 待确认（pending_items）。list 返回 {total,by_status,items}，
  // by_status 是 "status/stage"→数量，待办中心的红点直接用它。
  listPending: (p) => request('GET', `/v1/pending${qs(p)}`),
  // resolution 只带人改过的字段；后端 exclude_none 会把 null 丢掉、缺的从模型 suggestion 补。
  approvePending: (id, resolution) =>
    request('POST', `/v1/pending/${id}/approve`, resolution || {}),
  rejectPending: (id, note) =>
    request('POST', `/v1/pending/${id}/reject`, note ? { note } : {}),

  // 归属管理：规则（org_match_rules）+ 判过的目录缓存（workspace_map）。
  getAttribution: (p) => request('GET', `/v1/attribution${qs(p)}`),
  putAttribution: (body) => request('PUT', '/v1/attribution', body),
  putRule: (body) => request('PUT', '/v1/attribution/rules', body),
  deleteRule: (id) => request('DELETE', `/v1/attribution/rules/${id}`),

  // 召回调试：debug 版多返回 query_text/keywords/每条两路名次，且不写 recall_log。
  recallDebug: (body) => request('POST', '/v1/recall/debug', body),
  recallLog: (p) => request('GET', `/v1/recall-log${qs(p)}`),
  // B6：对一次真实召回标记"有用/没用"（null 撤销）；useless 的行进 eval 复查池。
  recallLogFeedback: (id, feedback) =>
    request('POST', `/v1/recall-log/${id}/feedback`, { feedback }),

  // 记忆库：浏览（all_scopes=true 看全库、不裁剪）、编辑、软删/恢复、看事件流水。
  listMemories: (p) => request('GET', `/v1/memories${qs(p)}`),
  patchMemory: (id, body) => request('PATCH', `/v1/memories/${id}`, body),
  deleteMemory: (id, note, restore) =>
    request('DELETE', `/v1/memories/${id}${qs({ note, restore })}`),
  memoryEvents: (id) => request('GET', `/v1/memories/${id}/events`),

  // 指标（§10）：纯 SQL 只读聚合，一次返回全部指标。org_key 给了就按分区裁剪。
  metrics: (p) => request('GET', `/v1/metrics${qs(p)}`),

  // 任务队列（jobs 表）：回填/写路径进度可视化。只回摘要字段，不带整包 payload。
  listJobs: (p) => request('GET', `/v1/jobs${qs(p)}`),
  // 单个 job 的执行过程：元信息 + 逐窗口进度 + 归属它的 LLM 调用时间线。
  getJob: (id) => request('GET', `/v1/jobs/${id}`),

  // 后台 worker 启停（★ 独立进程 python -m app.worker，由后端代管）。
  // list 回 {workers:[{slot,pid,alive,log}], max}；start 超上限后端回 400。
  listWorkers: () => request('GET', '/v1/workers'),
  startWorkers: (count) => request('POST', '/v1/workers/start', { count }),
  stopWorkers: () => request('POST', '/v1/workers/stop'),

  // MCP 接入探视（integrations/mcp-server/mem_mcp.py）。★ stdio 由 AI 客户端拉起，
  // 服务端不做"启动"；stop 默认只清父进程已退出的孤儿；selftest 走真连接（秒级）。
  getMcpStatus: () => request('GET', '/v1/mcp/status'),
  stopMcp: (orphansOnly) =>
    request('POST', '/v1/mcp/stop', { orphans_only: orphansOnly !== false }),
  mcpSelftest: () => request('POST', '/v1/mcp/selftest'),

  // 会话存档（conversation_log，ingest 落的原始对话）：列表 + 分页消息流。
  listConversations: (p) => request('GET', `/v1/conversations${qs(p)}`),
  conversationMessages: (id, p) =>
    request('GET', `/v1/conversations/${encodeURIComponent(id)}/messages${qs(p)}`),

  // 组织管理：orgs（检索分区，非权限边界）+ llm_endpoints（★ 只有非敏感字段，无 token）。
  getOrgs: () => request('GET', '/v1/orgs'),
  createOrg: (body) => request('POST', '/v1/orgs', body),
  patchOrg: (key, body) => request('PATCH', `/v1/orgs/${encodeURIComponent(key)}`, body),
  deleteOrg: (key) => request('DELETE', `/v1/orgs/${encodeURIComponent(key)}`),
  putEndpoint: (body) => request('PUT', '/v1/endpoints', body),
  deleteEndpoint: (key) => request('DELETE', `/v1/endpoints/${encodeURIComponent(key)}`),

  // 设置（只读）：生效配置 + 来源 + token 是否已配置（★ 从不返回 token 值，dsn 已脱敏）。
  getSettings: () => request('GET', '/v1/settings'),

  // 词表（doc_sections）：受控栏位 + aliases 维护（§14 item1 的工具）。
  // ★ 改 label/aliases 后端会重算 match_embedding（要模型，没热回 503）；改 sort/enabled 不碰模型。
  getSections: () => request('GET', '/v1/sections'),
  createSection: (body) => request('POST', '/v1/sections', body),
  patchSection: (key, body) => request('PATCH', `/v1/sections/${encodeURIComponent(key)}`, body),
  deleteSection: (key) => request('DELETE', `/v1/sections/${encodeURIComponent(key)}`),
  recomputeSections: () => request('POST', '/v1/sections/recompute'),

  // 重跑（replay_runs）：审阅 shadow 重跑 diff + 逐条/批量 apply（§8 中断点 6，applied 可续）。
  // ★ 生产这些表的 shadow 重跑是 P7，这一页只审阅 + 落地。apply 要算 embedding（没热回 503）。
  getReplayRuns: () => request('GET', '/v1/replay/runs'),
  getReplayRun: (id) => request('GET', `/v1/replay/runs/${id}`),
  applyReplayRun: (id, actionIds) =>
    request('POST', `/v1/replay/runs/${id}/apply`, actionIds ? { action_ids: actionIds } : {}),
  discardReplayRun: (id) => request('POST', `/v1/replay/runs/${id}/discard`),
}
