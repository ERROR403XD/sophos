"""压测工装 S1b（v2）：批量播种大规模测试库（Core executemany，治 SMB 逐行往返）。

v1 教训（STRESS_LOG S6）：ORM add+flush-per-identity 触发逐行 RETURNING INSERT，
百万行 × SMB 往返 = 小时级。v2 全部走 Core insert executemany（每批 ~2000 行）、
id 显式分配（免 RETURNING）、逐批提交。规模口径不变：
  --videos 30000 × 6-10 identity（均 8）≈ 24 万 identity × 4 face ≈ 百万级行。
"""
from __future__ import annotations

import argparse
import time

import cv2
import numpy as np
from sqlalchemy import insert

from _common import STRESS_ROOT, bootstrap, ensure_cap

N_PERSONS = 40
EMB_DIM = 512
CHUNK = 2000


def _chunked(session, table, rows: list[dict], label: str, t0: float) -> None:
    for i in range(0, len(rows), CHUNK):
        session.execute(insert(table), rows[i:i + CHUNK])
    session.commit()
    if label:
        print(f"  {label} ({time.time()-t0:.0f}s)", flush=True)


def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(STRESS_ROOT / "data_query"))
    ap.add_argument("--videos", type=int, default=30000)
    ap.add_argument("--min-identities", type=int, default=6)
    ap.add_argument("--max-identities", type=int, default=10)
    ap.add_argument("--faces-per-identity", type=int, default=4)
    ap.add_argument("--thumbs", type=int, default=0)
    ap.add_argument("--ratings", type=int, default=0)
    ap.add_argument("--pairs", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20260918)
    args = ap.parse_args()

    bootstrap(args.data_dir)
    from app.config import settings
    from app.db import session as db_session_mod
    from app.db.models import (Face, FaceIdentity, FaceScore, PairComparison,
                               UserRating, Video, VideoScore)
    from app.services.thumbs import thumb_path

    db_session_mod.init_engine(settings)
    SessionLocal = db_session_mod.SessionLocal
    rng = np.random.default_rng(args.seed)
    print(f"seeding into {settings.final_db_path()}", flush=True)

    bases = rng.normal(0, 1, (N_PERSONS, EMB_DIM)).astype(np.float32)
    bases /= np.linalg.norm(bases, axis=1, keepdims=True)

    t0 = time.time()
    with SessionLocal() as db:
        # ---- video（id 显式 1..N）----
        lib_count = 8
        vrows = []
        for vid in range(1, args.videos + 1):
            lib = f"D:/SophosStress/virtual/lib{vid % lib_count}"
            vrows.append(dict(
                id=vid, path=f"{lib}/video_{vid:06d}.mkv",
                filename=f"video_{vid:06d}.mkv", dir_path=lib,
                size_bytes=int(rng.integers(3e8, 9e8)),
                mtime=float(rng.integers(1_500_000_000, 1_800_000_000)),
                status="done", duration_sec=float(rng.integers(900, 2700)),
                vcodec="hevc", acodec="ac3"))
        _chunked(db, Video, vrows, f"videos={args.videos}", t0)

        # ---- identity / face / score（先算好 id，再批量插）----
        ident_rows, face_rows, score_rows, vs_rows = [], [], [], []
        next_ident = 1
        n_faces = 0
        for vid in range(1, args.videos + 1):
            k = int(rng.integers(args.min_identities, args.max_identities + 1))
            top = []
            for _ in range(k):
                iid = next_ident
                next_ident += 1
                person = (vid * 7 + iid * 13) % N_PERSONS
                emb = bases[person] + rng.normal(0, 0.25, EMB_DIM).astype(np.float32)
                emb /= max(np.linalg.norm(emb), 1e-9)
                base_score = float(np.clip(rng.normal(55, 15), 5, 98))
                top.append(base_score)
                ident_rows.append(dict(
                    id=iid, video_id=vid, n_samples=int(rng.integers(1, 9)),
                    female_prob_mean=float(rng.uniform(0.7, 1.0)),
                    clip_female_mean=float(rng.uniform(0.6, 1.0)),
                    occluded=1 if rng.random() < 0.05 else 0,
                    mean_embedding=emb.astype(np.float32).tobytes()))
                score_rows.append(dict(identity_id=iid, base_score=round(base_score, 2),
                                       base_model_version="scut_resnet18_v1"))
                for f in range(args.faces_per_identity):
                    face_rows.append(dict(
                        video_id=vid, identity_id=iid,
                        timestamp_sec=f * 2.0 + vid % 3,
                        bbox_x1=100.0 + f, bbox_y1=80.0, bbox_x2=220.0, bbox_y2=260.0,
                        det_score=0.85, quality_score=float(rng.uniform(0.3, 0.9)),
                        female_prob=float(rng.uniform(0.6, 1.0)),
                        pose_yaw=0.0, pose_pitch=0.0, pose_class="frontal",
                        occlusion_score=0.1))
                    n_faces += 1
            top.sort(reverse=True)
            base_final = round(sum(top[:3]) / len(top[:3]), 2)
            vs_rows.append(dict(video_id=vid, final_score=base_final,
                                base_final=base_final, identity_count=k,
                                score_model_version="base:scut_resnet18_v1"))
            if vid % 2500 == 0 or vid == args.videos:
                _chunked(db, FaceIdentity, ident_rows, "", t0)
                _chunked(db, FaceScore, score_rows, "", t0)
                _chunked(db, Face, face_rows, "", t0)
                _chunked(db, VideoScore, vs_rows,
                         f"video {vid}/{args.videos} idents={next_ident-1} faces={n_faces}", t0)
                ident_rows, face_rows, score_rows, vs_rows = [], [], [], []
                ensure_cap(STRESS_ROOT)

        # ---- 评分/对比（S5）----
        if args.ratings:
            rrows = [dict(identity_id=i, rating_type="score",
                          rating_value=str(int(rng.integers(1, 11))))
                     for i in range(1, min(args.ratings, next_ident - 1) + 1)]
            _chunked(db, UserRating, rrows, f"ratings={len(rrows)}", t0)
        if args.pairs:
            pool = list(range(1, max(args.ratings, 500) + 1))
            pair_rows, seen = [], set()
            while len(pair_rows) < args.pairs:
                w, l = rng.choice(pool, 2, replace=False)
                key = (min(int(w), int(l)), max(int(w), int(l)))
                if key in seen:
                    continue
                seen.add(key)
                pair_rows.append(dict(winner_identity_id=key[1], loser_identity_id=key[0]))
            _chunked(db, PairComparison, pair_rows, f"pairs={len(pair_rows)}", t0)

        # ---- 分片缩略图文件 ----
        if args.thumbs:
            img = np.full((240, 160, 3), 110, dtype=np.uint8)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
            assert ok
            payload = buf.tobytes()
            for n, iid in enumerate(range(1, args.thumbs + 1), 1):
                p = thumb_path(iid)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(payload)
                if n % 5000 == 0:
                    ensure_cap(STRESS_ROOT)
                    print(f"  thumbs {n}/{args.thumbs} ({time.time()-t0:.0f}s)", flush=True)
            print(f"thumbs={args.thumbs} ({time.time()-t0:.0f}s)", flush=True)

    ensure_cap(STRESS_ROOT)
    print(f"seed done in {time.time()-t0:.0f}s; root="
          f"{ensure_cap(STRESS_ROOT) / (1 << 30):.2f} GB", flush=True)


if __name__ == "__main__":
    _main()
