"""临时工具：打印 github search json 结果。"""
import json
import sys
from pathlib import Path

for f in sys.argv[1:]:
    p = Path(f)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f, "ERR", e)
        continue
    print("==", p.name)
    for i in d.get("items", [])[:10]:
        print(" ", i["full_name"], "|", (i.get("description") or "")[:80])
