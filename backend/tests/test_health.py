"""健康检查冒烟（M2）。运行：在 backend/ 目录执行 `python -m pytest`。"""


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"].startswith("1.2.")  # R6 起 1.2.x
    assert body["db"] == "ready"
