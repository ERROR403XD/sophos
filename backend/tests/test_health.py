"""健康检查冒烟（M2）。运行：在 backend/ 目录执行 `python -m pytest`。"""


from app import APP_VERSION


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION
    assert body["db"] == "ready"
