"""分步剖析 pair_diverse 暖态耗时（每步计时，定位剩余热点）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import bootstrap  # noqa: E402

bootstrap(r"D:\SophosStress\data_query", r"data\models")
from app.config import settings  # noqa: E402
from app.db import session as dsm  # noqa: E402

dsm.init_engine(settings)
SessionLocal = dsm.SessionLocal

import numpy as np  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.services import pairs  # noqa: E402


def main() -> None:
    with SessionLocal() as db:
        for attempt in range(3):
            t = {}
            t0 = time.perf_counter()
            rows = pairs._light_rows(db, max(50, settings.pair_sample_size))
            t["light_rows"] = time.perf_counter() - t0
            ids = list(rows)

            t0 = time.perf_counter()
            embs = pairs._load_embeddings(db, ids)
            t["load_emb"] = time.perf_counter() - t0

            t0 = time.perf_counter()
            co = pairs._cooccur_pairs(db, settings.merge_cooccur_tolerance_sec, ids)
            t["cooccur"] = time.perf_counter() - t0

            t0 = time.perf_counter()
            cmpd = pairs._compared_pairs_within(db, ids)
            t["compared"] = time.perf_counter() - t0

            t0 = time.perf_counter()
            n = len(ids)
            dim = len(next(iter(embs.values()))) if embs else 0
            E = np.zeros((n, dim), dtype=np.float32)
            for iid, emb in embs.items():
                E[pairs._pair_triu_index and ids.index(iid)] = emb
            t["build_E"] = time.perf_counter() - t0

            t0 = time.perf_counter()
            norms = np.maximum(np.linalg.norm(E, axis=1, keepdims=True), 1e-9)
            En = E / norms
            S = En @ En.T
            t["matmul"] = time.perf_counter() - t0

            t0 = time.perf_counter()
            iu0, iu1 = np.triu_indices(n, 1)
            _ = np.einsum("ij,ij->i", En[iu0], En[iu1])
            t["einsum_fancy"] = time.perf_counter() - t0

            t0 = time.perf_counter()
            p = pairs._pick_diverse(db, 300)
            t["full_pick"] = time.perf_counter() - t0
            print(f"attempt {attempt}: pick={p}")
            for k, v in t.items():
                print(f"  {k}: {v * 1000:.1f} ms")


if __name__ == "__main__":
    main()
