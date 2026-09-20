"""R6：访问口令鉴权测试（ADR-026，PLAN_v1.2.0 T1）。

覆盖：无口令全放行 / 启用后 API 401 分型 / 登录与错误密码 / Cookie 会话
放行 / 文档端点保护 / 静态 SPA 放行 / 改密码使旧 Cookie 失效。
"""
import pytest


@pytest.fixture()
def locked(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "access_password", "letmein")
    return settings


def test_open_when_no_password(client):
    """默认（SOPHOS_ACCESS_PASSWORD 为空）：不鉴权，行为与旧版一致。"""
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/videos").status_code == 200
    body = client.get("/api/auth/status").json()
    assert body == {"required": False, "authed": True}


def test_api_protected_without_cookie(client, locked):
    r = client.get("/api/videos")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"
    # 登录自举端点与健康检查保持开放
    assert client.get("/api/health").status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"required": True, "authed": False}


def test_login_rejects_wrong_password(client, locked):
    r = client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 401
    assert r.json()["code"] == "BAD_PASSWORD"


def test_login_sets_cookie_and_unlocks_api(client, locked):
    r = client.post("/api/auth/login", json={"password": "letmein"})
    assert r.status_code == 200
    assert "sophos_session" in r.cookies
    assert client.get("/api/videos").status_code == 200
    assert client.get("/api/jobs").status_code == 200


def test_docs_protected_but_spa_allowed(client, locked):
    """API 文档（/docs、/openapi.json）受保护；SPA 静态壳放行（无数据）。"""
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401
    # "/" 是静态托管（dist 存在→200 HTML；不存在→404），两条路径都不应 401
    assert client.get("/").status_code != 401


def test_password_change_invalidates_old_cookie(client, locked):
    assert client.post("/api/auth/login", json={"password": "letmein"}).status_code == 200
    assert client.get("/api/videos").status_code == 200
    # 改密码 → 旧会话 Cookie 立即失效（token 由密码派生）
    locked.access_password = "newpass"
    assert client.get("/api/videos").status_code == 401
    assert client.post("/api/auth/login", json={"password": "newpass"}).status_code == 200
    assert client.get("/api/videos").status_code == 200


def test_logout_clears_session(client, locked):
    client.post("/api/auth/login", json={"password": "letmein"})
    assert client.get("/api/videos").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/videos").status_code == 401
