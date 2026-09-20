"""S3 产物统计：data_pipe 的 identity/face 产量、缩略图分片、job 结果明细。"""
import json
import sqlite3
from pathlib import Path

db = Path(r"D:\SophosStress\data_pipe\sophos.db")
con = sqlite3.connect(db)
n_ident, n_face = con.execute(
    "SELECT (SELECT COUNT(*) FROM face_identity), (SELECT COUNT(*) FROM face)"
).fetchone()
n_vid_done = con.execute(
    "SELECT COUNT(*) FROM video WHERE status='done'").fetchone()[0]
thumbs = sorted((Path(r"D:\SophosStress\data_pipe\thumbs")).glob("*/*.jpg"))
print(f"videos done={n_vid_done} identities={n_ident} faces={n_face} thumbs={len(thumbs)}")
shards = {p.parent.name for p in thumbs}
print("thumb shard dirs:", len(shards), "->", sorted(shards)[:6])
con.close()

r = json.loads(Path(r"D:\SophosStress\pipe_stress.json").read_text(encoding="utf-8"))
print("process sec:", r["process"]["sec"], "620 videos ->",
      round(620 / r["process"]["sec"], 2), "videos/s")
