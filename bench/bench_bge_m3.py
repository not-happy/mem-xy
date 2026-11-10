"""bge-m3 CPU 实测：读路径延迟预算 400ms 是否成立。"""
import os, time, statistics, sys

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]

t0 = time.time()
from sentence_transformers import SentenceTransformer
t_import = time.time() - t0

t0 = time.time()
m = SentenceTransformer("BAAI/bge-m3", device="cpu")
t_load = time.time() - t0

print(f"import sentence_transformers : {t_import:6.2f}s")
print(f"load model (cold proc)       : {t_load:6.2f}s")
print(f"dims                         : {m.get_sentence_embedding_dimension()}")
print(f"max_seq_length               : {m.max_seq_length}")

# ── 读路径：单条 query encode，这是 hook 每轮对话都要付的成本 ──
QUERIES = [
    "改签流程里 executeChange 返回码怎么判成功",
    "这个项目为什么禁止使用 Lombok",
    "cabinGrade 和 flightClass 有什么区别",
    "Maven 打包要不要加 -o 离线参数",
    "供应商 contact 字段能不能填中文",
]

# 预热（第一次跑含图构建/内存分配）
m.encode([QUERIES[0]], normalize_embeddings=True, show_progress_bar=False)

single = []
for i in range(30):
    q = QUERIES[i % len(QUERIES)]
    t = time.time()
    m.encode([q], normalize_embeddings=True, show_progress_bar=False)
    single.append((time.time() - t) * 1000)

print("\n── 读路径：单条 query（30 次）──")
print(f"  p50 {pct(single,.50):7.1f} ms   p95 {pct(single,.95):7.1f} ms   "
      f"min {min(single):7.1f}   max {max(single):7.1f}")

# 长短敏感性
print("\n── 单条 query 按文本长度 ──")
for n in (8, 32, 128, 512):
    txt = ("改签流程里 executeChange 返回码怎么判断成功 " * 40)[:n]
    xs = []
    for _ in range(10):
        t = time.time()
        m.encode([txt], normalize_embeddings=True, show_progress_bar=False)
        xs.append((time.time() - t) * 1000)
    print(f"  {n:4d} 字符  p50 {pct(xs,.50):7.1f} ms  p95 {pct(xs,.95):7.1f} ms")

# ── 写路径：批量 encode ──
print("\n── 写路径：批量（异步，无硬预算）──")
for bs in (8, 32, 128):
    batch = [QUERIES[i % len(QUERIES)] for i in range(bs)]
    t = time.time()
    m.encode(batch, normalize_embeddings=True, batch_size=bs, show_progress_bar=False)
    el = (time.time() - t) * 1000
    print(f"  batch {bs:4d}  总 {el:8.1f} ms   单条 {el/bs:6.1f} ms")

# ── sparse 词法权重：三通道检索的第三条，ST 2.7.0 能不能取到 ──
print("\n── sparse 词法权重（第三通道）──")
import sentence_transformers
print(f"  sentence_transformers {sentence_transformers.__version__}")
print(f"  encode() 支持 return_sparse ? "
      f"{'return_sparse' in m.encode.__doc__ if m.encode.__doc__ else False}")
try:
    out = m.encode(["禁止使用 Lombok"], return_sparse=True, show_progress_bar=False)
    print(f"  return_sparse=True → {type(out)}")
except TypeError as e:
    print(f"  ✗ ST 不支持 return_sparse：{e}")

# 模型内部有没有 sparse/colbert 头
names = [n for n, _ in m.named_parameters()]
print(f"  参数张量数 {len(names)}")
for kw in ("sparse", "colbert", "linear"):
    hit = [n for n in names if kw in n.lower()]
    print(f"    含 '{kw}' 的参数：{hit if hit else '无'}")

mods = [type(x).__name__ for x in m.modules()] if hasattr(m, "modules") else []
try:
    mods = [type(x).__name__ for x in m._modules.values()]
except Exception:
    pass
print(f"  ST 模块链：{mods}")

# FlagEmbedding 是否可用
try:
    import FlagEmbedding
    print(f"  ✓ FlagEmbedding 已安装 {getattr(FlagEmbedding,'__version__','?')}")
except ImportError:
    print("  ✗ FlagEmbedding 未安装")

# ── 常驻内存：这决定能不能和 FastAPI 同进程常驻 ──
try:
    import psutil
    rss = psutil.Process().memory_info().rss / 1024**2
    print(f"\n本进程常驻内存 RSS: {rss:.0f} MB")
except ImportError:
    print("\n(psutil 未安装，跳过内存测量)")
