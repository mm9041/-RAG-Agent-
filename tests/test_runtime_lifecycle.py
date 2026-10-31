"""runtime 生命周期的边界回归：非阻塞重试、失败耗时冻结、卸载、配置写锁快速失败。"""
import asyncio
import sys
import tempfile
import time
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from server import runtime
from server.routers import system as system_router


def _reset_runtime():
    runtime.shutdown()
    runtime._update(
        agent=None, vector=None, kb=None, ready=False, booting=False,
        error=None, started_at=None, ready_at=None, finished_at=None, stage="",
    )


class RuntimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        _reset_runtime()

    def tearDown(self):
        _reset_runtime()
        # 若某个用例中途持锁失败，保证后续用例不会全部卡住
        if runtime._boot_lock.locked():
            runtime._boot_lock.release()

    def test_bootstrap_returns_immediately_when_already_booting(self):
        runtime._boot_lock.acquire()
        runtime._update(booting=True, started_at=time.time(), stage="检查知识库")
        try:
            started = time.monotonic()
            state = runtime.bootstrap()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(state["booting"])
            self.assertFalse(state["ready"])
        finally:
            runtime._boot_lock.release()

    def test_failure_records_error_and_freezes_elapsed(self):
        fake = types.ModuleType("rag.vector_store")

        def boom():
            raise RuntimeError("embedding unavailable")

        fake.VectorStoreService = boom
        with patch.dict(sys.modules, {"rag.vector_store": fake}), \
                patch("utils.model_settings.configuration_guard", nullcontext), \
                patch("utils.model_settings.refresh_model_settings"):
            with self.assertRaises(RuntimeError):
                runtime.bootstrap()

        first = runtime.status()
        self.assertEqual(first["stage"], "初始化失败")
        self.assertFalse(first["booting"])
        self.assertIn("embedding unavailable", first["error"])
        self.assertIsNotNone(first["boot_seconds"])

        time.sleep(0.05)
        # 失败后 boot_seconds 必须停在结束时，而不是随查询时间继续增长
        self.assertEqual(runtime.status()["boot_seconds"], first["boot_seconds"])

    def test_success_publishes_state_and_shutdown_clears_it(self):
        agent = object()

        class FakeVector:
            def load_document(self):
                return "内容未变化，直接加载：3 块"

        fake_vector = types.ModuleType("rag.vector_store")
        fake_vector.VectorStoreService = FakeVector
        fake_agent = types.ModuleType("agent.react_agent")
        fake_agent.ReactAgent = lambda: agent

        with patch.dict(sys.modules, {"rag.vector_store": fake_vector,
                                      "agent.react_agent": fake_agent}), \
                patch("utils.model_settings.configuration_guard", nullcontext), \
                patch("utils.model_settings.refresh_model_settings"):
            state = runtime.bootstrap()

        self.assertTrue(state["ready"])
        self.assertEqual(state["stage"], "就绪")
        self.assertEqual(state["kb"], "内容未变化，直接加载：3 块")
        self.assertIs(runtime.get_agent(), agent)

        frozen = runtime.status()["boot_seconds"]
        time.sleep(0.05)
        self.assertEqual(runtime.status()["boot_seconds"], frozen)

        runtime.shutdown()
        stopped = runtime.status()
        self.assertFalse(stopped["ready"])
        self.assertEqual(stopped["stage"], "已停止")
        with self.assertRaises(RuntimeError):
            runtime.get_agent()

    def test_write_guard_fails_fast_while_initialization_reads(self):
        from utils.model_settings import ConfigurationBusy, configuration_guard

        with tempfile.TemporaryDirectory() as tmp, \
                patch("utils.model_settings.get_abs_path",
                      side_effect=lambda name: str(Path(tmp) / name)):
            with configuration_guard():
                started = time.monotonic()
                with self.assertRaises(ConfigurationBusy):
                    with configuration_guard(write=True, timeout=0.2):
                        pass
                # 不应等到建库结束（真实场景可能几分钟），而是超时后立刻失败
                self.assertLess(time.monotonic() - started, 2)


class RetryAndAdminBusyTests(unittest.TestCase):
    def test_retry_while_booting_returns_without_second_boot(self):
        state = {
            "ready": False, "booting": True, "stage": "检查知识库",
            "error": None, "boot_seconds": 12.0, "kb": None,
        }
        with patch("server.runtime.status", return_value=state), \
                patch("server.runtime.bootstrap") as boot:
            result = asyncio.run(system_router.retry())
        boot.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertTrue(result["booting"])
        self.assertEqual(result["stage"], "检查知识库")

    def test_admin_model_change_maps_busy_to_409(self):
        from fastapi import HTTPException

        from server.routers import admin
        from server.schemas import ModelsRequest
        from utils.model_settings import ConfigurationBusy

        payload = ModelsRequest(
            chat="chat-x", embedding="embed-x", rerank="rank-x", confirm_rebuild=True,
        )
        with patch.dict("utils.config_handler.model_conf",
                        {"embedding_model_name": "embed-old"}), \
                patch("utils.model_settings.apply_models",
                      side_effect=ConfigurationBusy("知识库正在初始化")):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(admin.put_models(payload, token="token"))
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
