"""面容库查询 + 评分 + 两两对比（M3/M5；R5.2 下发 stream_url + 随机顺序）。

路由顺序注意：/faces/pair 必须先于 /faces/{identity_id} 声明。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import FaceIdentity, FaceScore, PairComparison, UserRating, Video
from app.db.session import get_db
from app.services import streamer
from app.services.pairs import pick_pair

router = APIRouter(prefix="/faces", tags=["faces"])


def _latest_ratings(db: Session, identity_ids: list[int]) -> dict[int, str]:
    """每个 identity 最新一条评分（任意 type），无则缺失。"""
    if not identity_ids:
        return {}
    rows = db.execute(
        select(UserRating.identity_id, UserRating.rating_value)
        .where(UserRating.identity_id.in_(identity_ids))
        .order_by(UserRating.id.desc())
    ).all()
    latest: dict[int, str] = {}
    for iid, val in rows:
        if iid not in latest:
            latest[iid] = val
    return latest


def _rep_timestamps(db: Session, rep_face_ids: list[int | None]) -> dict[int, float | None]:
    """rep_face_id -> timestamp_sec 批量查询。"""
    ids = [i for i in rep_face_ids if i is not None]
    if not ids:
        return {}
    from app.db.models import Face
    rows = db.execute(select(Face.id, Face.timestamp_sec).where(Face.id.in_(ids))).all()
    return dict(rows)


def _item(identity: FaceIdentity, video: Video | None, score: FaceScore | None,
          my_rating: str | None = None, timestamp_sec: float | None = None) -> dict:
    # R5：rep_thumb 带 ?v={updated_at} 内容版本——重处理重建 identity 后 URL
    # 变化，配合 thumbs 端点的 immutable 缓存头，浏览器可长缓存缩略图
    v = identity.updated_at or "0"
    # R5.2：评分/对比页直接播放——后端统一下发流地址与档位（与 videos API 同源）
    stream_url = stream_mode = hls_url = None
    if video is not None:
        stream_url = f"/api/videos/{video.id}/stream"
        hls_url = f"/api/videos/{video.id}/hls"  # R8(ADR-029)：移动端 HLS 会话
        stream_mode = streamer.decide_mode(
            video.path, video.vcodec, video.acodec, settings.transcode_enabled)
    return {
        "id": identity.id,
        "video_id": identity.video_id,
        "video_filename": video.filename if video else None,
        "video_path": video.path if video else None,
        "stream_url": stream_url,
        "hls_url": hls_url,
        "stream_mode": stream_mode,
        "rep_thumb": f"/api/thumbs/{identity.id}.jpg?v={v}",
        "timestamp_sec": timestamp_sec,
        "n_samples": identity.n_samples,
        "female_prob_mean": identity.female_prob_mean,  # R1(P1)：identity 级性别置信度
        "clip_female_mean": identity.clip_female_mean,  # R2：CLIP 第二意见（一致同意门控第二票）
        "occluded": bool(identity.occluded),            # R1(P2)：遮挡标记（auto/manual）
        "base_score": score.base_score if score else None,
        "personalized_score": score.personalized_score if score else None,
        "my_rating": my_rating,
    }


@router.get("")
def list_faces(page: int = 1, page_size: int = 50, video_id: int | None = None,
               unrated: bool = False, order: str = "id",
               db: Session = Depends(get_db)) -> dict:
    """R5.2：order=random 时未评面容随机抽样（评分页不按 id 顺序刷屏）。"""
    if order not in ("id", "random"):
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_ORDER", "message": "order must be id|random"})
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    filters = []
    if video_id is not None:
        filters.append(FaceIdentity.video_id == video_id)
    if unrated:
        # R5：NOT EXISTS 关联子查询替代 NOT IN (SELECT DISTINCT ...)——
        # 数十万 identity 规模下免物化整个已评集合，走 user_rating.identity_id 索引
        filters.append(~exists(
            select(UserRating.identity_id)
            .where(UserRating.identity_id == FaceIdentity.id)
            .correlate(FaceIdentity)))

    count_stmt = select(func.count()).select_from(FaceIdentity)
    for f in filters:
        count_stmt = count_stmt.where(f)
    total = db.execute(count_stmt).scalar_one()

    stmt = (select(FaceIdentity, Video, FaceScore)
            .outerjoin(Video, Video.id == FaceIdentity.video_id)
            .outerjoin(FaceScore, FaceScore.identity_id == FaceIdentity.id))
    for f in filters:
        stmt = stmt.where(f)
    if order == "random":
        # 随机序：先在过滤后的 id 上 ORDER BY random() 取一页，再按序取整行
        #（大库下每次取评是一批一次排序，换批才有新随机面——可接受）
        id_rows = db.execute(
            select(FaceIdentity.id).where(*filters)
            .order_by(func.random()).limit(page_size)).all()
        ids = [r[0] for r in id_rows]
        if not ids:
            rows = []
        else:
            by_id = {}
            for row in db.execute(
                    stmt.where(FaceIdentity.id.in_(ids))).all():
                by_id[row[0].id] = row
            rows = [by_id[i] for i in ids if i in by_id]
    else:
        stmt = stmt.order_by(FaceIdentity.id).offset((page - 1) * page_size).limit(page_size)
        rows = db.execute(stmt).all()
    ratings = _latest_ratings(db, [i.id for i, _, _ in rows])
    stamps = _rep_timestamps(db, [i.rep_face_id for i, _, _ in rows])
    return {"items": [_item(i, v, s, ratings.get(i.id), stamps.get(i.rep_face_id))
                      for i, v, s in rows],
            "total": total, "page": page, "page_size": page_size}


@router.get("/pair")
def get_pair(strategy: str = "diverse", db: Session = Depends(get_db)) -> dict:
    """抽一对面容供两两对比（ADR-013；R4 默认 diverse，ADR-019）。

    - diverse：人物优先（明显不同人物 > 不同视频 > 分差小 > 随机）；
    - similar：分差接近优先；random：随机。无可比对时 404 NO_PAIR。
    """
    if strategy not in ("diverse", "similar", "random"):
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_STRATEGY",
            "message": f"strategy must be diverse|similar|random: {strategy}"})
    picked = pick_pair(db, strategy=strategy)
    if picked is None:
        raise HTTPException(status_code=404, detail={
            "code": "NO_PAIR", "message": "no comparable face pair available"})
    rows = db.execute(
        select(FaceIdentity, Video, FaceScore)
        .outerjoin(Video, Video.id == FaceIdentity.video_id)
        .outerjoin(FaceScore, FaceScore.identity_id == FaceIdentity.id)
        .where(FaceIdentity.id.in_(picked))
    ).all()
    by_id = {i.id: (i, v, s) for i, v, s in rows}
    a, b = picked
    stamps = _rep_timestamps(db, [by_id[a][0].rep_face_id, by_id[b][0].rep_face_id])
    return {"a": _item(*by_id[a], timestamp_sec=stamps.get(by_id[a][0].rep_face_id)),
            "b": _item(*by_id[b], timestamp_sec=stamps.get(by_id[b][0].rep_face_id))}


def _maybe_autotrain(db: Session) -> None:
    """ADR-008 自动触发：累计评分+对比数达到门槛且恰为 auto_train_every 整数倍时，
    自动提交训练任务（不自动启用版本）。R6.3：阈值改为运行时设置（WebUI 可调，0=关闭）。"""
    from sqlalchemy import func

    from app.db.models import PairComparison
    from app.services import runtime_settings
    from app.services.jobs import has_active_job, submit_job
    from app.services.personalizer import MIN_TOTAL_SAMPLES

    n_rating = db.execute(select(func.count()).select_from(UserRating)).scalar_one()
    n_pair = db.execute(select(func.count()).select_from(PairComparison)).scalar_one()
    total = n_rating + n_pair
    every = runtime_settings.get_value(db, "auto_train_every")
    if (total >= MIN_TOTAL_SAMPLES and every > 0
            and total % every == 0
            and not has_active_job(db, "train")):
        submit_job(db, "train")


@router.post("/pair/compare", status_code=200)
def compare_pair(body: dict, db: Session = Depends(get_db)) -> dict:
    """记录对比结果 {winner_id, loser_id}。"""
    winner_id, loser_id = body.get("winner_id"), body.get("loser_id")
    if not isinstance(winner_id, int) or not isinstance(loser_id, int):
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_RATING", "message": "winner_id/loser_id must be int"})
    if winner_id == loser_id:
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_RATING", "message": "winner and loser must differ"})
    if db.get(FaceIdentity, winner_id) is None or db.get(FaceIdentity, loser_id) is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": "face identity not found"})
    db.add(PairComparison(winner_identity_id=winner_id, loser_identity_id=loser_id))
    db.commit()
    _maybe_autotrain(db)
    return {"ok": True}


class RatingIn(BaseModel):
    type: str            # score | thumbs
    value: str | int

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in ("score", "thumbs"):
            raise ValueError("type must be score|thumbs")
        return v


@router.post("/{identity_id}/occlusion")
def set_occlusion(identity_id: int, body: dict, db: Session = Depends(get_db)) -> dict:
    """R1(P2)：人工遮挡标记 {occluded: bool}，写 occluded + occluded_source='manual'。

    遮挡面容保留在库内可评分（偏好信号），仅影响个性化训练集
    （fully-occluded 无干净样本时被 personalizer 剔除——见 pipeline 写库口径）。
    manual 覆盖 auto。
    """
    identity = db.get(FaceIdentity, identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": f"face identity {identity_id} not found"})
    occluded = body.get("occluded")
    if not isinstance(occluded, bool):
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_REQUEST", "message": "occluded must be bool"})
    identity.occluded = 1 if occluded else 0
    identity.occluded_source = "manual"
    db.commit()
    return {"ok": True, "occluded": bool(identity.occluded)}


@router.post("/{identity_id}/rating")
def rate_face(identity_id: int, body: RatingIn, db: Session = Depends(get_db)) -> dict:
    if db.get(FaceIdentity, identity_id) is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": f"face identity {identity_id} not found"})
    if body.type == "score":
        try:
            v = int(body.value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={  # noqa: B904
                "code": "INVALID_RATING", "message": "score value must be 1-10"})
        if not 1 <= v <= 10:
            raise HTTPException(status_code=400, detail={
                "code": "INVALID_RATING", "message": "score value must be 1-10"})
        value = str(v)
    else:  # thumbs
        if body.value not in ("up", "down"):
            raise HTTPException(status_code=400, detail={
                "code": "INVALID_RATING", "message": "thumbs value must be up|down"})
        value = str(body.value)
    db.add(UserRating(identity_id=identity_id, rating_type=body.type, rating_value=value))
    db.commit()
    _maybe_autotrain(db)
    return {"ok": True}


@router.get("/{identity_id}")
def get_face(identity_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.execute(
        select(FaceIdentity, Video, FaceScore)
        .outerjoin(Video, Video.id == FaceIdentity.video_id)
        .outerjoin(FaceScore, FaceScore.identity_id == FaceIdentity.id)
        .where(FaceIdentity.id == identity_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": f"face identity {identity_id} not found"})
    identity, video, score = row
    rep = None
    if identity.rep_face_id:
        from app.db.models import Face
        rep = db.get(Face, identity.rep_face_id)
    ratings = _latest_ratings(db, [identity_id])
    return {
        **_item(identity, video, score, ratings.get(identity_id)),
        "timestamp_sec": rep.timestamp_sec if rep else None,
        "rep_quality": rep.quality_score if rep else None,
    }
