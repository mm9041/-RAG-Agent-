"""
向量库与会话安全的单元测试

这两组都是**发生过真实事故**的地方，所以必须有回归测试：

- `_db_is_complete`：报告二十三节 —— 全量重建中途失败会留下
  "元数据在、向量为 0" 的半成品，旧的完整性检查会把它判为**完整**，
  导致应用静默加载一个空库、对任何问题都答"知识库暂无相关资料"。
- `delete_thread` 保护：报告十八、二十节 —— 测试脚本误删过用户的真实会话两次。

跑法：
    python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from utils.config_handler import chroma_conf


class TestDbIsComplete(unittest.TestCase):
    """半成品向量库不能被判为"完整" """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_test_")
        self._original = chroma_conf["persist_directory"]
        chroma_conf["persist_directory"] = self.tmp

    def tearDown(self):
        chroma_conf["persist_directory"] = self._original
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _service(self):
        from rag.vector_store import VectorStoreService
        from unittest.mock import patch
        from tests.test_improvements import LocalEmbeddings
        with patch("rag.vector_store.get_embed_model", return_value=LocalEmbeddings()):
            return VectorStoreService()

    def test_什么都没有时判为不完整(self):
        self.assertFalse(self._service()._db_is_complete())

    def test_元数据在但向量为0时必须判为不完整(self):
        """★ 事故现场：全量重建中被额度报错打断 → 旧库已 drop、新库还没写，
        而上一次成功构建留下的 kb_meta.json 还在。旧逻辑会返回 True。"""
        service = self._service()
        with open(service.meta_path, "w", encoding="utf-8") as f:
            json.dump({"params": {}, "files": {}}, f)
        # chroma 的 sqlite 已由构造过程创建
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "chroma.sqlite3")), "构造后应已存在 sqlite")
        self.assertEqual(service.count(), 0, "此时库里应当没有向量")
        self.assertFalse(
            service._db_is_complete(),
            "元数据在、向量为 0 的**半成品**必须判为不完整，否则应用会静默加载空库",
        )

    def test_集合里有向量时判为完整(self):
        service = self._service()
        with open(service.meta_path, "w", encoding="utf-8") as f:
            json.dump({"params": {}, "files": {}}, f)
        # 直接写向量，绕过 embedding 接口（这样测试不联网）
        service.vector_store._collection.add(
            ids=["t1"], embeddings=[[0.1] * 8], documents=["占位内容"],
            metadatas=[{"source": "/tmp/x.txt"}],
        )
        self.assertGreater(service.count(), 0)
        self.assertFalse(service._db_is_complete(), "非空但没有文件清单，不能判为完整")
        service._save_meta({"/tmp/x.txt": {"chunks": 1}})
        self.assertTrue(service._db_is_complete())


class TestStaleSegmentDirs(unittest.TestCase):
    """段目录残留的清理判定

    背景：全量重建走的是 `reset_collection()`，它**只清数据、不删段目录** ——
    每重建一次就留下一个空目录（本项目一天重建十几次就积了 5 个）。

    删除条件刻意收得很紧：**既未登记、又是空目录**，两个都要满足。
    下面的用例就是这个安全边界的守卫。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="seg_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mkdir(self, name, files=()):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        for f in files:
            with open(os.path.join(path, f), "w") as fh:
                fh.write("x")
        return path

    def test_未登记且为空_应被清理(self):
        from rag.vector_store import find_stale_segment_dirs
        path = self._mkdir("aaaa-1111")
        self.assertEqual(find_stale_segment_dirs(self.tmp, {"bbbb-2222"}), [path])

    def test_已登记的段目录不能动(self):
        """当前库正在用的段，名字在 segments 表里 —— 动了就毁库"""
        from rag.vector_store import find_stale_segment_dirs
        self._mkdir("aaaa-1111")
        self.assertEqual(find_stale_segment_dirs(self.tmp, {"aaaa-1111"}), [])

    def test_未登记但非空_一律不碰(self):
        """★ 最关键的安全边界：只要目录里有文件，就不管它登不登记，都不删"""
        from rag.vector_store import find_stale_segment_dirs
        self._mkdir("aaaa-1111", files=["header.bin", "data_level0.bin"])
        self.assertEqual(find_stale_segment_dirs(self.tmp, set()), [])

    def test_普通文件不受影响(self):
        """chroma.sqlite3 / kb_meta.json 这些是文件不是目录，不该被判为残留"""
        from rag.vector_store import find_stale_segment_dirs
        with open(os.path.join(self.tmp, "chroma.sqlite3"), "w") as fh:
            fh.write("x")
        with open(os.path.join(self.tmp, "kb_meta.json"), "w") as fh:
            fh.write("{}")
        self.assertEqual(find_stale_segment_dirs(self.tmp, set()), [])

    def test_目录不存在时返回空(self):
        from rag.vector_store import find_stale_segment_dirs
        self.assertEqual(find_stale_segment_dirs(os.path.join(self.tmp, "nope"), set()), [])


class TestCleanupWiringIsFailSafe(unittest.TestCase):
    """★ 回归：`_cleanup_stale_segments` 在拿不到登记信息时必须**一个都不删**

    纯函数 `find_stale_segment_dirs` 本身的判定是对的，出事的是**接线**：
    `_registered_segment_ids()` 原先在读不到时返回**空集**，
    而空集在纯函数里的语义是"没有任何段登记在册" —— 于是清理范围被放到**最大**，
    连正在使用的空段目录都会进待删列表（实测复现：registered_ids=set() 时
    已登记的空目录出现在结果里）。

    原注释写的是"读失败就返回空集并放弃清理"，**与代码实际行为相反**。
    这类"空集默认成宽松语义"的 bug 不会因为逻辑看起来谨慎而失效，所以测接线。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="seg_wire_test_")
        self._original = chroma_conf["persist_directory"]
        chroma_conf["persist_directory"] = self.tmp
        # 一个"正在使用但恰好为空"的段目录 + 一个真残留
        self.registered = os.path.join(self.tmp, "aaaa-1111")
        self.stale = os.path.join(self.tmp, "bbbb-2222")
        os.makedirs(self.registered)
        os.makedirs(self.stale)

    def tearDown(self):
        chroma_conf["persist_directory"] = self._original
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _service(self):
        from rag.vector_store import VectorStoreService
        from unittest.mock import patch
        from tests.test_improvements import LocalEmbeddings
        with patch("rag.vector_store.get_embed_model", return_value=LocalEmbeddings()):
            return VectorStoreService()

    def test_读不到登记信息时跳过清理两个目录都在(self):
        service = self._service()
        service._registered_segment_ids = lambda: None
        self.assertEqual(0, service._cleanup_stale_segments())
        self.assertTrue(os.path.isdir(self.registered))
        self.assertTrue(os.path.isdir(self.stale), "拿不到依据时连真残留也不碰")

    def test_登记集为空集时同样跳过(self):
        """正常重建后至少应有 1 个段；一个都没有说明依据不可靠，不冒险。"""
        service = self._service()
        service._registered_segment_ids = lambda: set()
        self.assertEqual(0, service._cleanup_stale_segments())
        self.assertTrue(os.path.isdir(self.stale))

    def test_登记信息正常时只删未登记的那个(self):
        service = self._service()
        service._registered_segment_ids = lambda: {"aaaa-1111"}
        self.assertEqual(1, service._cleanup_stale_segments())
        self.assertTrue(os.path.isdir(self.registered), "在用段绝不能动")
        self.assertFalse(os.path.isdir(self.stale))

    def test_chroma文件名变化时返回None并跳过清理(self):
        """sqlite 文件不在 → 返回 None（而不是空集）→ 清理跳过。

        用"另指到一个没有 chroma.sqlite3 的目录"来模拟，而不是删掉那个文件 ——
        Chroma 持有文件句柄，Windows 上删不掉（实测 PermissionError）。
        这两个方法都只依赖 `self.persist_directory`，所以改指目录是等价的。
        """
        service = self._service()

        empty_dir = tempfile.mkdtemp(prefix="no_sqlite_")
        registered = os.path.join(empty_dir, "aaaa-1111")
        os.makedirs(registered)
        try:
            service.persist_directory = empty_dir
            self.assertIsNone(service._registered_segment_ids())
            self.assertEqual(0, service._cleanup_stale_segments())
            self.assertTrue(os.path.isdir(registered), "读不到依据时一个目录都不许删")
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)


class TestThreadIdHelpers(unittest.TestCase):

    def test_按身份隔离会话(self):
        from agent.react_agent import ReactAgent
        self.assertEqual(ReactAgent.thread_id_for("1001"), "user-1001")
        self.assertNotEqual(ReactAgent.thread_id_for("1001"),
                            ReactAgent.thread_id_for("1009"))

    def test_新会话带身份前缀(self):
        from agent.react_agent import ReactAgent
        tid = ReactAgent.new_thread_id("1001")
        self.assertTrue(tid.startswith("user-1001-"))
        self.assertNotEqual(tid, ReactAgent.new_thread_id("1001"), "每次应是新的")


class TestDeleteThreadGuard(unittest.TestCase):
    def test_ownership_before_delete(self):
        from agent.react_agent import ReactAgent
        from unittest.mock import Mock
        agent = object.__new__(ReactAgent)
        agent.checkpointer = Mock()
        for bad in ["default", "cli-test", "", "user", "user-10010-x", "user-2002-x"]:
            with self.assertRaises(PermissionError):
                agent.delete_thread(bad, "1001")
        agent.checkpointer.delete_thread.assert_not_called()
        agent.delete_thread("user-1001-test", "1001")
        agent.checkpointer.delete_thread.assert_called_once_with("user-1001-test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
