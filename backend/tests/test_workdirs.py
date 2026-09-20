"""workdirs API 测试（M2）：CRUD + 目录存在性校验 + 统一错误体。"""


def test_workdirs_crud(client, tmp_path):
    d = tmp_path / "movies"
    d.mkdir()

    r = client.post("/api/workdirs", json={"path": str(d)})
    assert r.status_code == 201
    assert r.json()["workdirs"] == [str(d)]

    assert client.get("/api/workdirs").json()["workdirs"] == [str(d)]

    # 重复添加幂等
    r2 = client.post("/api/workdirs", json={"path": str(d)})
    assert r2.status_code == 201
    assert r2.json()["workdirs"] == [str(d)]

    r = client.post("/api/workdirs", json={"path": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert r.json()["code"] == "DIR_NOT_FOUND"

    r = client.delete("/api/workdirs", params={"path": str(d)})
    assert r.status_code == 200
    assert client.get("/api/workdirs").json()["workdirs"] == []

    assert client.delete("/api/workdirs", params={"path": str(d)}).status_code == 404
