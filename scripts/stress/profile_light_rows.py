"""下钻 _light_rows：COUNT / MAX / IN 查询（有无 join）分别计时。"""
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

import random  # noqa: E402

from sqlalchemy import func, select  # noqa: E402

from app.db.models import FaceIdentity, FaceScore  # noqa: E402


def main() -> None:
    rng = random.Random(1)
    with SessionLocal() as db:
        for attempt in range(3):
            t0 = time.perf_counter()
            total = db.execute(select(func.count()).select_from(FaceIdentity)).scalar_one()
            t_count = time.perf_counter() - t0

            t0 = time.perf_counter()
            max_id = db.execute(select(func.max(FaceIdentity.id))).scalar_one()
            t_max = time.perf_counter() - t0

            picks = [rng.randint(1, max_id) for _ in range(608)]

            t0 = time.perf_counter()
            rows1 = db.execute(
                select(FaceIdentity.id, FaceIdentity.video_id)
                .where(FaceIdentity.id.in_(picks))).all()
            t_plain = time.perf_counter() - t0

            t0 = time.perf_counter()
            rows2 = db.execute(
                select(FaceIdentity.id, FaceIdentity.video_id,
                       FaceScore.base_score, FaceScore.personalized_score)
                .outerjoin(FaceScore, FaceScore.identity_id == FaceIdentity.id)
                .where(FaceIdentity.id.in_(picks))).all()
            t_join = time.perf_counter() - t0

            t0 = time.perf_counter()
            n_pairs = db.execute(select(func.count()).select_from(FaceScore)).scalar_one()
            t_count_fs = time.perf_counter() - t0

            print(f"attempt {attempt}: count={t_count*1000:.0f}ms max={t_max*1000:.1f}ms "
                  f"plain_in={t_plain*1000:.0f}ms join_in={t_join*1000:.0f}ms "
                  f"count_score={t_count_fs*1000:.0f}ms rows={len(rows2)}")


if __name__ == "__main__":
    main()
