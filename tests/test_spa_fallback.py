"""SPA 兜底、API 404 与静态资源缓存头的回归测试。"""
import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from server.main import DIST, app, spa


class SpaFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 不用 with TestClient：避免触发 lifespan 里的真实建库
        cls.client = TestClient(app)

    def test_unknown_api_get_returns_json_404(self):
        res = self.client.get("/api/does-not-exist")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(res.headers["content-type"].startswith("application/json"))
        self.assertEqual(res.json()["detail"], "接口不存在")

    def test_unknown_api_post_is_404_not_405(self):
        res = self.client.post("/api/does-not-exist", json={})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["detail"], "接口不存在")

    def test_real_api_route_is_not_shadowed(self):
        res = self.client.get("/api/system/status")
        self.assertEqual(res.status_code, 200)
        self.assertIn("ready", res.json())
        self.assertEqual(res.headers.get("cache-control"), "no-store")

    def test_bare_api_and_trailing_slash_are_404(self):
        for path in ("/api", "/api/"):
            with self.subTest(path=path):
                res = self.client.get(path)
                self.assertEqual(res.status_code, 404)
                self.assertEqual(res.json()["detail"], "接口不存在")

    def test_client_route_falls_back_to_index_html(self):
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])
        self.assertIn('id="app"', res.text)
        self.assertEqual(res.headers.get("cache-control"), "no-cache")

    def test_unknown_page_falls_back_to_index_html(self):
        res = self.client.get("/no-such-page")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])

    def test_path_traversal_is_rejected(self):
        # 直接调路由，避开 httpx 对 URL 点段的规范化
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(spa("../../.env"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertNotIn("DASHSCOPE", str(ctx.exception.detail))

    def test_files_inside_dist_still_served(self):
        response = asyncio.run(spa("index.html"))
        self.assertIsInstance(response, FileResponse)

    def test_favicon_is_204_without_icon_file(self):
        res = self.client.get("/favicon.ico")
        self.assertEqual(res.status_code, 204)

    def test_hashed_asset_gets_immutable_cache(self):
        assets = DIST / "assets"
        if not assets.is_dir():
            self.skipTest("前端未构建，静态挂载不存在")
        probe = assets / "__cache-probe-test.js"
        probe.write_text("window.__probe = 1", encoding="utf-8")
        self.addCleanup(probe.unlink, missing_ok=True)
        res = self.client.get("/assets/__cache-probe-test.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("immutable", res.headers.get("cache-control", ""))


class LifespanTests(unittest.TestCase):
    def test_startup_spawns_daemon_boot_and_shutdown_clears_state(self):
        from server import runtime

        # 不用真实线程：只验证 lifespan 是否按预期参数拉起预热，避免真建库
        with patch("server.main.threading") as threading:
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/system/status").status_code, 200)
                threading.Thread.assert_called_once()
                self.assertTrue(threading.Thread.call_args.kwargs["daemon"])
                self.assertEqual(threading.Thread.call_args.kwargs["name"], "bootstrap")
                threading.Thread.return_value.start.assert_called_once()
        self.assertEqual(runtime.status()["stage"], "已停止")
        self.assertFalse(runtime.status()["ready"])


if __name__ == "__main__":
    unittest.main()
