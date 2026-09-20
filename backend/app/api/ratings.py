"""评分统计（M5）。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PairComparison, UserRating
from app.db.session import get_db

router = APIRouter(prefix="/ratings", tags=["ratings"])


@router.get("/stats")
def rating_stats(db: Session = Depends(get_db)) -> dict:
    by_type = dict(db.execute(
        select(UserRating.rating_type, func.count())
        .group_by(UserRating.rating_type)
    ).all())

    histogram = {str(i): 0 for i in range(1, 11)}
    for value, count in db.execute(
        select(UserRating.rating_value, func.count())
        .where(UserRating.rating_type == "score")
        .group_by(UserRating.rating_value)
    ).all():
        histogram[str(value)] = count

    unique_rated = db.execute(
        select(func.count(func.distinct(UserRating.identity_id)))
    ).scalar_one()
    pair_count = db.execute(select(func.count()).select_from(PairComparison)).scalar_one()

    return {
        "score_count": by_type.get("score", 0),
        "thumbs_count": by_type.get("thumbs", 0),
        "pair_count": pair_count,
        "score_histogram": histogram,
        "unique_faces_rated": unique_rated,
    }
