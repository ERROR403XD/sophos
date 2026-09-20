"""查看 S3 流水线压测报告 + 种子库规模。"""
import json
import os
from pathlib import Path

r = json.loads(Path(r"D:\SophosStress\pipe_stress.json").read_text(encoding="utf-8"))
print("process sec:", r["process"]["sec"], "| pause at done =", r["pause"]["at_done"])
print("process jobs:", len(r["process_jobs"]))
for j in r["process_jobs"][:6]:
    print(" ", j)
print("  ...")
for j in r["process_jobs"][-3:]:
    print(" ", j)

db = Path(r"D:\SophosStress\data_query\sophos.db")
if db.exists():
    print("seed db MB:", round(os.path.getsize(db) / 1048576, 1))
