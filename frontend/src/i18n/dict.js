// i18n 词典：字段名 + 枚举值的中文映射（单一事实源）。
//
// 四条设计原则（重构计划 §1.2）：
// 1. 两层结构：FIELDS（字段 label）与 ENUMS（枚举组 → 值 → {zh, tone}）。
// 2. tone 只有 5 个语义值：success / warning / danger / info / neutral。
//    Badge 类名 = tone-*，与枚举值彻底解耦（修「类名=枚举值」陷阱）。
// 3. 兜底优先：查不到 → 原样显示 + title 挂原值。后端新增枚举不会坏 UI。
// 4. 下拉 option 的 value 永远是英文原值（后端 CHECK 约束），只译显示 label。

// ── 字段词典（~30 个）：zh = 中文 label，hint = 悬停补充说明 ──────────────
export const FIELDS = {
  org_key: { zh: '组织', hint: 'org = 检索分区，不是权限边界' },
  project_key: { zh: '项目' },
  scope: { zh: '作用域', hint: 'user 用户级 / org 组织级 / project 项目级' },
  subject: { zh: '主体', hint: '这条记忆说的是用户还是项目' },
  role: { zh: '角色' },
  kind: { zh: '类型', hint: '路由判定的结果：观察线索进成文规范人审流水线，零碎事实只靠召回，丢弃即出局' },
  stage: { zh: '阶段', hint: '卡在等人裁决的哪一步：路由判定 / 查重 / 选栏位' },
  status: { zh: '状态' },
  section_key: { zh: '栏位', hint: 'doc_sections 受控栏位；下拉里显示中文名' },
  aliases: { zh: '同义词', hint: '这一栏的同义词表，参与栏位匹配向量' },
  endpoint_key: { zh: '端点' },
  base_url: { zh: '地址' },
  model: { zh: '模型' },
  max_input_tokens: { zh: '输入上限' },
  max_input: { zh: '输入上限' },
  sort: { zh: '排序' },
  label: { zh: '显示名' },
  token: { zh: '令牌', hint: 'token 不经前端，只走 ~/.memxy/secrets.toml 或环境变量' },
  consumed: { zh: '已消费', hint: '是否已被汇总进成文规范提案' },
  polarity: { zh: '极性', hint: 'positive 正面素材 / negative 人工否决记录' },
  final: { zh: '综合分', hint: 'final = rrf 归一 × scope 权重 × 时间衰减 × mention 加成（§6.4）' },
  rrf: { zh: '融合分', hint: 'Reciprocal Rank Fusion，两路名次的倒数融合' },
  authority: { zh: '权威度', hint: 'draft 草稿 / confirmed 已确认 / pinned 已钉住' },
  origin: { zh: '来源' },
  resolved_by: { zh: '判定来源' },
  pattern: { zh: '匹配模式' },
  pattern_type: { zh: '模式类型' },
  applies_to: { zh: '适用范围' },
  priority: { zh: '优先级' },
  cwd: { zh: '工作目录' },
  why: { zh: '依据', hint: '为什么记这条' },
  keywords: {
    zh: '关键词',
    hint: '标识符/专名/术语（类名、字段名、供应商名、命令）。召回的精确匹配通道：与查询关键词重叠即命中，与向量通道融合排序',
  },
  retention_days: { zh: '保留天数' },
  embedding: { zh: '向量', hint: 'bge-m3 嵌入向量' },
  job: { zh: '任务', hint: '异步 job 队列' },
  diff: { zh: '改动对比' },
  prompt_ver: { zh: '提示词版本' },
  k: { zh: '条数 k' },
  dense_rank: { zh: '向量名次', hint: 'dense 通道的名次，空 = 该通道没召回它' },
  keyword_rank: { zh: '关键词名次', hint: 'keyword 通道的名次，空 = 该通道没召回它' },
}

// ── 枚举映射（20 组，取值已全部对照后端源码/schema CHECK 约束核实）──────────
export const ENUMS = {
  kind: {
    observation: {
      zh: '观察线索', tone: 'info',
      hint: '该变成规矩/提醒的规范性素材：进成文规范人审流水线，汇总后每次相关对话主动注入',
    },
    atomic_fact: {
      zh: '零碎事实', tone: 'neutral',
      hint: '一次性信息点：只靠相似度 + 关键词召回，永不主动注入',
    },
    discard: { zh: '丢弃', tone: 'neutral', hint: '没有记忆价值，只留路由记录' },
  },
  scope: {
    user: { zh: '用户级', tone: 'info' },
    org: { zh: '组织级', tone: 'success' },
    project: { zh: '项目级', tone: 'neutral' },
  },
  subject: {
    user: { zh: '用户', tone: 'info' },
    project: { zh: '项目', tone: 'neutral' },
  },
  role: {
    profile: { zh: '画像', tone: 'neutral' },
    preference: { zh: '偏好', tone: 'neutral' },
    pointer: { zh: '指针', tone: 'neutral' },
    fact: { zh: '事实', tone: 'neutral' },
  },
  stage: {
    route: {
      zh: '路由判定', tone: 'info',
      hint: '模型给每条对话素材分类（观察线索/零碎事实/丢弃）；置信度不够或输出不合 schema 就排到这里等人裁决',
    },
    dedup: {
      zh: '查重', tone: 'neutral',
      hint: '定时扫描发现两条已有记忆高度相似，等你裁决是否合并成一条（近重复合并卡）',
    },
    section: {
      zh: '选栏位', tone: 'warning',
      hint: '分类已定但没匹配上受控栏位；通常是该栏 aliases 缺词',
    },
  },
  pending_status: {
    pending: { zh: '待确认', tone: 'warning' },
    failed: { zh: '落库失败', tone: 'danger' },
    approved: { zh: '已批准', tone: 'info' },
    rejected: { zh: '已否决', tone: 'danger' },
    done: { zh: '已完成', tone: 'success' },
  },
  proposal_status: {
    pending: { zh: '待审', tone: 'warning' },
    accepted: { zh: '已采纳', tone: 'success' },
    rejected: { zh: '已否决', tone: 'danger' },
    expired: { zh: '已过期', tone: 'neutral' },
  },
  // app/queue/jobs.py 状态机：pending → running → done / failed
  job_status: {
    pending: { zh: '排队', tone: 'warning' },
    running: { zh: '执行中', tone: 'info' },
    done: { zh: '完成', tone: 'success' },
    failed: { zh: '失败', tone: 'danger' },
  },
  authority: {
    draft: { zh: '草稿', tone: 'neutral' },
    confirmed: { zh: '已确认', tone: 'success' },
    pinned: { zh: '已钉住', tone: 'info' },
  },
  origin: {
    summarized: { zh: '汇总合成', tone: 'info' },
    human: { zh: '人工修改', tone: 'success' },
  },
  polarity: {
    positive: { zh: '正面素材', tone: 'success' },
    negative: { zh: '否决', tone: 'danger' },
  },
  memory_status: {
    active: { zh: '生效中', tone: 'success' },
    deleted: { zh: '已删除', tone: 'danger' },
    superseded: { zh: '已被替代', tone: 'neutral' },
  },
  // schema CHECK：memory_events.action IN (ADD,UPDATE,DELETE,NOOP,RESTORE)；replay_actions 复用
  memory_action: {
    ADD: { zh: '新增', tone: 'success' },
    UPDATE: { zh: '修改', tone: 'info' },
    DELETE: { zh: '删除', tone: 'danger' },
    NOOP: { zh: '无变化', tone: 'neutral' },
    RESTORE: { zh: '恢复', tone: 'success' },
  },
  replay_run_status: {
    running: { zh: '进行中', tone: 'info' },
    awaiting_review: { zh: '待审阅', tone: 'warning' },
    applied: { zh: '已落地', tone: 'success' },
    discarded: { zh: '已丢弃', tone: 'neutral' },
  },
  resolved_by: {
    default: { zh: '兜底', tone: 'warning' },
    rule: { zh: '规则命中', tone: 'info' },
    human: { zh: '人工确认', tone: 'success' },
  },
  pattern_type: {
    cwd_prefix: { zh: '目录前缀', tone: 'neutral' },
    git_remote_path: { zh: '仓库路径', tone: 'neutral' },
    git_remote_host: { zh: '仓库主机', tone: 'neutral' },
  },
  applies_to: {
    org: { zh: '组织级', tone: 'neutral' },
    project: { zh: '项目级', tone: 'neutral' },
    both: { zh: '两者', tone: 'neutral' },
  },
  settings_source: {
    default: { zh: '默认', tone: 'neutral' },
    config: { zh: 'config.toml', tone: 'success' },
    secrets: { zh: 'secrets.toml', tone: 'info' },
    env: { zh: '环境变量', tone: 'warning' },
  },
  // app/core/config.py：{"default","route","write_facts","summarize"}；未知键原样显示
  thinking_task: {
    default: { zh: '默认', tone: 'neutral' },
    route: { zh: '路由', tone: 'neutral' },
    write_facts: { zh: '写入', tone: 'neutral' },
    summarize: { zh: '汇总', tone: 'neutral' },
    recall: { zh: '召回', tone: 'neutral' },
  },
  // schema CHECK：memory_feedback.kind IN (corrected,useless,useful)
  feedback_kind: {
    corrected: { zh: '已纠错', tone: 'warning' },
    useless: { zh: '无用', tone: 'danger' },
    useful: { zh: '有用', tone: 'success' },
  },
  // 品牌名不译，只给 tone
  api_protocol: {
    anthropic: { zh: 'anthropic', tone: 'neutral' },
    openai: { zh: 'openai', tone: 'neutral' },
    ollama: { zh: 'ollama', tone: 'neutral' },
  },
}

// ── 后端自由文案的兜底词典：连真后端实测后回填；查不到原样显示 ──────────────
// Pending it.reason 是「基键[:参数]」用 '+' 拼接的复合串，
// 实测取值：below_threshold:0.85 / below_threshold:0.85+section_unmatched
export const REASONS = {
  below_threshold: (param) => (param ? `置信度低于门槛 ${param}` : '置信度低于门槛'),
  section_unmatched: () => '未匹配到栏位',
  near_duplicate: () => '两条记忆高度相似，建议合并',
}
export const SETTINGS_LABELS = {}  // Settings it.label
export const SETTINGS_GROUPS = {}  // Settings it.group

// ── 统一查询出口（所有组件只经这三个函数取词）─────────────────────────────

/** 字段名 → 中文；查不到返回原键。 */
export function fieldZh(key) {
  return FIELDS[key]?.zh || key
}

/** 字段 hint（无则 undefined）。 */
export function fieldHint(key) {
  return FIELDS[key]?.hint
}

/**
 * 枚举值 → {zh, tone, en, unknown?}。
 * 查不到：zh = 原值、tone = neutral、unknown = true —— UI 不坏，只是没翻译。
 */
export function enumItem(group, value) {
  const it = ENUMS[group]?.[value]
  if (it) return { ...it, en: value }
  return { zh: String(value ?? '—'), tone: 'neutral', en: value, unknown: true }
}

/** 枚举组 → 下拉选项 [{value(英文), zh}]，供 <option> 渲染。 */
export function enumOptions(group) {
  return Object.entries(ENUMS[group] || {}).map(([value, it]) => ({ value, zh: it.zh }))
}

/** 后端自由文案兜底翻译：查不到原样显示。 */
export function softZh(dict, value) {
  return dict[value] || value
}

/**
 * Pending reason 复合串 → 中文：按 '+' 拆段，段内 '基键:参数' 查 REASONS；
 * 任一段查不到就原样保留（后端新增 reason 不坏 UI）。
 */
export function reasonZh(reason) {
  if (!reason) return reason
  return String(reason).split('+').map((seg) => {
    const i = seg.indexOf(':')
    const key = i < 0 ? seg : seg.slice(0, i)
    const fn = REASONS[key]
    return fn ? fn(i < 0 ? undefined : seg.slice(i + 1)) : seg
  }).join(' + ')
}
