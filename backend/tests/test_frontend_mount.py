"""前端静态托管（进程收敛）：dist 挂 "/"、缺失优雅降级、API 路由不被 mount 吞。

mount_frontend 抽成带 dist 参数的函数就是为了这里不依赖真实 npm build：
临时目录造假 dist，真实产物只在存在时多验一遍模块级 app。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.main import FRONTEND_DIST, mount_frontend


def _make_dist(root):
    """最小可用的 vite 产物形态：index.html + assets/ 下静态资源。"""
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><title>mem-xy 记忆系统</title><div id="root"></div>',
        encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return root


def _api_app():
    """模拟 main.py 的结构：API 路由先注册，静态挂载最后（Starlette 按序匹配）。"""
    app = FastAPI()

    @app.get("/v1/metrics")
    def metrics():
        return {"ok": True}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


def test_mount_serves_index_at_root(tmp_path):
    app = _api_app()
    assert mount_frontend(app, dist=_make_dist(tmp_path / "dist")) is True

    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "mem-xy" in resp.text


def test_api_routes_win_over_mount(tmp_path):
    """★ mount("/") 会吃掉一切未匹配路径——顺序反了 /v1 就没了。"""
    app = _api_app()
    mount_frontend(app, dist=_make_dist(tmp_path / "dist"))

    client = TestClient(app)
    assert client.get("/v1/metrics").json() == {"ok": True}
    assert client.get("/healthz").json() == {"status": "ok"}


def test_static_assets_and_unknown_path(tmp_path):
    app = _api_app()
    mount_frontend(app, dist=_make_dist(tmp_path / "dist"))

    client = TestClient(app)
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/v1/no-such-endpoint").status_code == 404


def test_missing_dist_degrades_gracefully(tmp_path):
    """dist 不存在：不挂载、不抛，API 照常，UI 路径 404。"""
    app = _api_app()
    assert mount_frontend(app, dist=tmp_path / "no-dist") is False

    client = TestClient(app)
    assert client.get("/").status_code == 404
    assert client.get("/healthz").status_code == 200


def test_half_built_dist_treated_as_missing(tmp_path):
    """目录在但没 index.html（构建到一半）→ 视同未构建，不挂载。"""
    half = tmp_path / "half"
    (half / "assets").mkdir(parents=True)

    assert mount_frontend(_api_app(), dist=half) is False


@pytest.mark.skipif(not (FRONTEND_DIST / "index.html").is_file(),
                    reason="frontend/dist 未构建")
def test_real_app_mounts_real_dist():
    """模块级 app 在 import 时已执行 mount_frontend(真实 dist)——端到端再验一遍。"""
    from app.api.main import app

    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert '<div id="root">' in resp.text
