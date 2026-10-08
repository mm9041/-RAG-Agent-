"""
文件后缀识别的回归测试（对应 A5）

背景：白名单原本用 `f.endswith(("txt","pdf"))`、读文件用 `path.endswith("txt")`，
两处都**硬编码小写** —— 于是一个 `维护保养.PDF` 会不进库、不报错、
也不出现在任何文件统计里。症状正好是最难定位的那类：某些问题突然答不出。
（实测把大写后缀文件放进 data/ 复现过：一个都不报错。）
"""
import os
import shutil
import tempfile
import unittest

from utils.file_handler import file_extension, listdir_with_allowed_type


class TestFileExtension(unittest.TestCase):
    def test_大写小写归一到同一个小写后缀(self):
        self.assertEqual("pdf", file_extension("维护保养.PDF"))
        self.assertEqual("txt", file_extension("保养.Txt"))
        self.assertEqual("txt", file_extension("故障排除.txt"))

    def test_无后缀与多点文件名(self):
        self.assertEqual("", file_extension("README"))
        self.assertEqual("txt", file_extension("a.b.txt"))
        self.assertEqual("", file_extension(".gitignore"))

    def test_完整路径也可以(self):
        self.assertEqual("pdf", file_extension(os.path.join("data", "文档.PDF")))


class TestListdirWithAllowedType(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ext_test_")
        for name in ["故障排除.txt", "维护保养.PDF", "选购指南.Txt",
                     "说明.md", "无后缀"]:
            open(os.path.join(self.tmp, name), "w", encoding="utf-8").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_大写后缀必须被收进来(self):
        """★ 回归：旧实现只用 endswith 比小写，这三条里会后两条全丢。"""
        found = {os.path.basename(p)
                 for p in listdir_with_allowed_type(self.tmp, ("txt", "pdf"))}
        self.assertEqual({"故障排除.txt", "维护保养.PDF", "选购指南.Txt"}, found)

    def test_配置里写带点的后缀也等价(self):
        plain = listdir_with_allowed_type(self.tmp, ("txt", "pdf"))
        dotted = listdir_with_allowed_type(self.tmp, (".txt", ".PDF"))
        self.assertEqual(set(plain), set(dotted))

    def test_不匹配的后缀不收(self):
        found = {os.path.basename(p)
                 for p in listdir_with_allowed_type(self.tmp, ("md",))}
        self.assertEqual({"说明.md"}, found)

    def test_路径不存在返回空元组而不是类型白名单(self):
        """旧版这里踩过坑：错误分支返回了 allowed_types，调用方会把 "txt" 当路径往下传。"""
        self.assertEqual((), listdir_with_allowed_type(os.path.join(self.tmp, "没有这个目录"),
                                                       ("txt",)))
        self.assertEqual((), listdir_with_allowed_type(os.path.join(self.tmp, "无后缀"),
                                                       ("txt",)))


if __name__ == "__main__":
    unittest.main()
