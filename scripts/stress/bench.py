"""压测工装 S1c：对测试库做端点/服务基准计时（S2/S4/S5 的测量器）。

测量项（每项 N 次取中位数/最值，结果打表 + 落 JSON）：
- 查询层：health / videos 列表（首页·深页·搜索）/ faces unrated（首页·深页）/
  ratings stats / settings
- pair 选对：diverse 首次（含共现冷计算）与热身后的成本
- 缩略图：随机 identity 端点延迟（分片路径）p50/p90 + 404 路径
- 训练/打分应用（有评分时）：personalizer.train / apply_version（24 万 identity
  分块 UPSERT）/ aggregator.recompute_all（3 万视频）

用法（任意 CWD；--data-dir 与 seed_db 一致）：
  python scripts/stress/bench.py --data-dir <stress-data>/data_query \
      --models-dir <project-root>/data/models
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path

from sqlalchemy import text

from _common import STRESS_ROOT, bootstrap


def _timed(fn, n=3):
    """返回 (中位数秒, 最小秒, 全部耗时)。"""
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return round(statistics.median(times), 4), round(min(times), 4), [round(t, 4) for t in times]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(STRESS_ROOT / "data_query"))
    ap.add_argument("--models-dir", default="<project-root>/data/models")
    ap.add_argument("--thumbs-sample", type=int, default=30)
    ap.add_argument("--with-train", dest="with_train", action="store_true", default=True)
    ap.add_argument("--no-train", dest="with_train", action="store_false")
    args = ap.parse_args()

    bootstrap(args.data_dir, args.models_dir)
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.db import session as db_session_mod
    from app.db.models import FaceIdentity
    from app.main import app

    db_session_mod.init_engine(settings)
    SessionLocal = db_session_mod.SessionLocal
    report: dict = {"data_dir": str(settings.final_db_path()), "bench": {}}

    with SessionLocal() as db:
        counts = {
            "videos": db.execute(text("SELECT COUNT(*) FROM video")).scalar_one(),
            "identities": db.execute(text("SELECT COUNT(*) FROM face_identity")).scalar_one(),
            "faces": db.execute(text("SELECT COUNT(*) FROM face")).scalar_one(),
            "ratings": db.execute(text("SELECT COUNT(*) FROM user_rating")).scalar_one(),
            "pairs": db.execute(text("SELECT COUNT(*) FROM pair_comparison")).scalar_one(),
        }
    report["scale"] = counts
    print("scale:", counts)

    with TestClient(app) as client:
        b = report["bench"]

        b["health"] = _timed(lambda: client.get("/api/health").raise_for_status(), 5)

        b["videos_page1"] = _timed(lambda: client.get(
            "/api/videos", params={"page_size": 50, "sort": "final_score"}).raise_for_status(), 5)
        b["videos_deep"] = _timed(lambda: client.get(
            "/api/videos", params={"page": 500, "page_size": 50}).raise_for_status(), 3)
        b["videos_search"] = _timed(lambda: client.get(
            "/api/videos", params={"q": "video_0123", "page_size": 50}).raise_for_status(), 3)

        b["faces_unrated_cold"] = _timed(lambda: client.get(
            "/api/faces", params={"unrated": True, "page_size": 100}).raise_for_status(), 1)
        b["faces_unrated_warm"] = _timed(lambda: client.get(
            "/api/faces", params={"unrated": True, "page_size": 100}).raise_for_status(), 5)
        b["faces_deep"] = _timed(lambda: client.get(
            "/api/faces", params={"page": 1000, "page_size": 50}).raise_for_status(), 3)

        b["ratings_stats"] = _timed(lambda: client.get("/api/ratings/stats").raise_for_status(), 3)
        b["settings_get"] = _timed(lambda: client.get("/api/settings").raise_for_status(), 5)

        # pair diverse：首次含共现冷计算，之后为热路径
        b["pair_diverse_cold"] = _timed(lambda: client.get(
            "/api/faces/pair", params={"strategy": "diverse"}), 1)
        b["pair_diverse_warm"] = _timed(lambda: client.get(
            "/api/faces/pair", params={"strategy": "diverse"}), 5)

        # 缩略图：随机抽 identity（有缩略图文件的前段）
        with SessionLocal() as db:
            ids = [r[0] for r in db.query(FaceIdentity.id)
                   .order_by(FaceIdentity.id).limit(5000).all()]
        if ids:
            sample = random.Random(7).sample(ids, min(args.thumbs_sample, len(ids)))
            lat = []
            for iid in sample:
                t0 = time.perf_counter()
                r = client.get(f"/api/thumbs/{iid}.jpg")
                lat.append((time.perf_counter() - t0, r.status_code))
            ok = [t for t, c in lat if c == 200]
            b["thumbs_get"] = {
                "n": len(sample), "ok": len(ok),
                "p50_ms": round(sorted(ok)[len(ok) // 2] * 1000, 1) if ok else None,
                "p90_ms": round(sorted(ok)[int(len(ok) * 0.9)] * 1000, 1) if ok else None,
            }

    # ---- S5：训练 / 打分应用 / 全量重算 ----
    if args.with_train and counts["ratings"] >= 20:
        from app.services import aggregator, personalizer

        with SessionLocal() as db:
            t0 = time.perf_counter()
            meta = personalizer.train(db, settings.final_models_dir())
            b["train"] = {"sec": round(time.perf_counter() - t0, 2), **meta}

            version = meta["version"]
            t0 = time.perf_counter()
            n_apply = personalizer.apply_version(db, settings.final_models_dir(), version)
            b["apply"] = {"sec": round(time.perf_counter() - t0, 2), "updated": n_apply}

            from app.services.kv import set_kv
            set_kv(db, personalizer.ACTIVE_KEY, version)
            t0 = time.perf_counter()
            n_rec = aggregator.recompute_all(db)
            b["recompute_all"] = {"sec": round(time.perf_counter() - t0, 2),
                                  "videos": n_rec}

    out = Path(args.data_dir) / "stress_bench.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["bench"], ensure_ascii=False, indent=1))
    print("report ->", out)


if __name__ == "__main__":
    main()
