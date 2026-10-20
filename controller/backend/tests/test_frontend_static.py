"""前端靜態檔同源提供（FLOW_CONTROLLER_FRONTEND_DIST）與 SPA fallback。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dispatch import FakeDispatcher
from app.main import create_app

INDEX = "<!doctype html><div id=app></div>"


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("不能被讀到", encoding="utf-8")
    return root


def _settings(tmp_path) -> Settings:
    return Settings(db_url=f"sqlite:///{tmp_path / 'static.db'}", dispatcher="fake", scan_interval=0)


def _client(tmp_path, **kwargs) -> TestClient:
    app = create_app(_settings(tmp_path), dispatcher=FakeDispatcher(inline=True), **kwargs)
    return TestClient(app)


def test_serves_index_and_spa_fallback(tmp_path, dist):
    with _client(tmp_path, frontend_dist=str(dist)) as client:
        for path in ["/", "/flows", "/flows/fl_123", "/inbox", "/approvals/ap_1?token=abc", "/login"]:
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.text == INDEX
            assert resp.headers["content-type"].startswith("text/html")
            assert resp.headers["cache-control"] == "no-cache"


def test_serves_existing_files_with_cache_headers(tmp_path, dist):
    with _client(tmp_path, frontend_dist=str(dist)) as client:
        asset = client.get("/assets/index-abc123.js")
        assert asset.status_code == 200
        assert asset.text == "console.log(1)"
        assert "immutable" in asset.headers["cache-control"]
        assert client.get("/favicon.svg").text == "<svg/>"
        # 缺少的 assets 檔回 404，不回 index.html
        assert client.get("/assets/missing.js").status_code == 404
        assert client.head("/").status_code == 200


def test_api_and_healthz_are_not_shadowed(tmp_path, dist):
    with _client(tmp_path, frontend_dist=str(dist)) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/api/healthz").json()["status"] == "ok"
        # 需要登入的 API 照常回 401（JSON），不是前端頁面
        resp = client.get("/api/flows")
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "unauthorized"
        # /api 底下不存在的路徑維持 404 JSON
        for path in ["/api", "/api/", "/api/nope", "/api/nope/deeper", "/healthz/x"]:
            resp = client.get(path)
            assert resp.status_code == 404, path
            assert resp.headers["content-type"].startswith("application/json"), path
        # 確認頁 API 仍由後端處理（一次性 token 無效）
        assert client.get("/api/approvals/ap_x", params={"token": "t"}).status_code == 404


def test_path_traversal_is_blocked(tmp_path, dist):
    with _client(tmp_path, frontend_dist=str(dist)) as client:
        for path in ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"]:
            resp = client.get(path)
            assert "不能被讀到" not in resp.text, path


def test_reads_env_var(tmp_path, dist, monkeypatch):
    monkeypatch.setenv("FLOW_CONTROLLER_FRONTEND_DIST", str(dist))
    with _client(tmp_path) as client:
        assert client.get("/flows").text == INDEX


def test_not_mounted_without_setting_or_index(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("FLOW_CONTROLLER_FRONTEND_DIST", raising=False)
    with _client(tmp_path) as client:
        assert client.get("/flows").status_code == 404
        assert client.get("/healthz").status_code == 200

    empty = tmp_path / "empty"
    empty.mkdir()
    with caplog.at_level("WARNING", logger="flow_controller"), _client(
        tmp_path, frontend_dist=str(empty)
    ) as client:
        assert client.get("/").status_code == 404
        assert client.get("/healthz").status_code == 200
    assert "找不到 index.html" in caplog.text


def test_api_status_codes_unchanged_by_frontend(tmp_path, dist, monkeypatch):
    """掛上前端前後，/api 與 /healthz 的狀態碼（含 404、405）必須完全一樣。"""
    monkeypatch.delenv("FLOW_CONTROLLER_FRONTEND_DIST", raising=False)
    cases = [
        ("GET", "/api/nope"),
        ("POST", "/api/nope"),
        ("DELETE", "/api/flows"),
        ("GET", "/api/flows/fl_x/cancel"),  # 只接受 POST 的路由：405
        ("POST", "/healthz"),
        ("GET", "/api"),
    ]
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    with _client(plain_dir) as plain, _client(tmp_path, frontend_dist=str(dist)) as mounted:
        for method, path in cases:
            expected = plain.request(method, path)
            actual = mounted.request(method, path)
            assert actual.status_code == expected.status_code, (method, path)
            assert actual.json() == expected.json(), (method, path)


def test_invalid_paths_do_not_crash(tmp_path, dist):
    with _client(tmp_path, frontend_dist=str(dist)) as client:
        # NUL 字元、過長檔名不能變成 500
        assert client.get("/a%00b").text == INDEX
        assert client.get("/" + "a" * 5000).text == INDEX
        assert client.get("/assets/" + "b" * 5000 + ".js").status_code == 404
