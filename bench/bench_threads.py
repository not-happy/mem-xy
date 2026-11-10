"""torch 线程数对读路径 encode 延迟的影响（尾延迟是 400ms 预算的真正风险）。"""
import os, time, subprocess, sys, json

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def run(nthreads):
    os.environ["OMP_NUM_THREADS"] = str(nthreads)
    import torch
    torch.set_num_threads(nthreads)
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer("BAAI/bge-m3", device="cpu")
    m.max_seq_length = 128          # ★ 实测定的读路径截断上限

    BASE = ("在 gds-agg-processor-tiket 里改签流程 executeChange 返回码怎么判成功，"
            "verifyChangeDoneByRetrievePnr 要不要校验行程内容")
    qs = [BASE[:128], BASE[:64], BASE[:32], "cabinGrade 取什么值", "Maven 打包要加 -o 吗"]

    def pct(xs, p):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(len(xs) * p))]

    for q in qs:                                     # 预热
        m.encode([q], normalize_embeddings=True, show_progress_bar=False)

    xs = []
    for i in range(60):
        q = qs[i % len(qs)]
        t = time.time()
        m.encode([q], normalize_embeddings=True, show_progress_bar=False)
        xs.append((time.time() - t) * 1000)

    # 批量（写路径）
    batch = [qs[i % len(qs)] for i in range(32)]
    t = time.time()
    m.encode(batch, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
    b32 = (time.time() - t) * 1000

    return dict(threads=nthreads, p50=pct(xs, .50), p95=pct(xs, .95),
                p99=pct(xs, .99), mx=max(xs), batch32=b32)


if __name__ == "__main__":
    n = int(sys.argv[1])
    print(json.dumps(run(n)))
