"""读路径 query 长度上限：找出让 encode p95 留在预算内的截断点。"""
import os, time

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from sentence_transformers import SentenceTransformer

m = SentenceTransformer("BAAI/bge-m3", device="cpu")

def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]

# 真实用户输入的样子：中文问句 + 代码标识符混排
BASE = ("在 gds-agg-processor-tiket 里改签流程 executeChange 返回码怎么判成功，"
        "verifyChangeDoneByRetrievePnr 要不要校验行程内容，cabinGrade 取什么值")

print("字符数 → encode 单条（每档 25 次，先预热 3 次）")
print(f"{'chars':>6} {'tokens':>7} {'p50':>8} {'p95':>8} {'max':>8}")
rows = []
for n in (16, 32, 48, 64, 96, 128, 192, 256, 384, 512):
    txt = (BASE * 6)[:n]
    toks = len(m.tokenizer(txt, truncation=False)["input_ids"]) - 2
    for _ in range(3):
        m.encode([txt], normalize_embeddings=True, show_progress_bar=False)
    xs = []
    for _ in range(25):
        t = time.time()
        m.encode([txt], normalize_embeddings=True, show_progress_bar=False)
        xs.append((time.time() - t) * 1000)
    rows.append((n, toks, pct(xs, .50), pct(xs, .95), max(xs)))
    print(f"{n:>6} {toks:>7} {pct(xs,.50):>7.1f}  {pct(xs,.95):>7.1f}  {max(xs):>7.1f}")

# ── 截断到 N 字符后 token 数与实际延迟，验证"按字符截断"是否够用 ──
print("\n按字符截断的效果（同一长文本，截到不同长度）")
long_txt = BASE * 8
print(f"  原文 {len(long_txt)} 字符 / "
      f"{len(m.tokenizer(long_txt, truncation=False)['input_ids'])-2} tokens")
for cap in (48, 64, 96, 128):
    t_txt = long_txt[:cap]
    toks = len(m.tokenizer(t_txt, truncation=False)["input_ids"]) - 2
    for _ in range(3):
        m.encode([t_txt], normalize_embeddings=True, show_progress_bar=False)
    xs = []
    for _ in range(25):
        t = time.time()
        m.encode([t_txt], normalize_embeddings=True, show_progress_bar=False)
        xs.append((time.time() - t) * 1000)
    print(f"  cap {cap:4d} 字符 = {toks:4d} tokens  p50 {pct(xs,.50):6.1f} ms  "
          f"p95 {pct(xs,.95):6.1f} ms")

# ── 模型自带截断 vs 手动截断：max_seq_length 设小能否省时间 ──
print("\n把 max_seq_length 设小（模型内部截断）是否等价于手动截断")
m.max_seq_length = 64
xs = []
for _ in range(25):
    t = time.time()
    m.encode([long_txt], normalize_embeddings=True, show_progress_bar=False)
    xs.append((time.time() - t) * 1000)
print(f"  max_seq_length=64，输入 {len(long_txt)} 字符："
      f"p50 {pct(xs,.50):.1f} ms  p95 {pct(xs,.95):.1f} ms")
m.max_seq_length = 128
xs = []
for _ in range(25):
    t = time.time()
    m.encode([long_txt], normalize_embeddings=True, show_progress_bar=False)
    xs.append((time.time() - t) * 1000)
print(f"  max_seq_length=128，同一输入："
      f"p50 {pct(xs,.50):.1f} ms  p95 {pct(xs,.95):.1f} ms")
