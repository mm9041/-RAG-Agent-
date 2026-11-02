"""守住"async 函数里不得做同步阻塞调用"这条线。

背景：``async def`` 里的阻塞调用不会报错、不会变慢（墙钟时间一样），
只会把整个事件循环一起冻住——期间所有其他请求（包括正在流的 SSE）全部停摆。
所以它无法靠功能测试发现，只能做静态检查。

检查范围是 ``server/`` 下所有 ``AsyncFunctionDef`` 的函数体：
- 进入嵌套的 ``async def``（它同样在循环上跑）
- **跳过嵌套的同步 ``def``**：那正是"抽成同步函数再 to_thread"的写法，
  函数体在执行时已经不在循环线程上
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 只要出现在 async 函数体里就必然是阻塞的（这些方法没有异步版本）
BLOCKING_ATTRS = {
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "iterdir",
    "glob",
    "rglob",
}

# 需要看限定名才能判断的：asyncio.sleep 是好的，time.sleep 是坏的
QUALIFIED_BLOCKING = {
    ("time", "sleep"),
    ("requests", "get"),
    ("requests", "post"),
    ("requests", "put"),
    ("requests", "delete"),
    ("PIL.Image", "open"),
    ("sqlite3", "connect"),
    ("subprocess", "run"),
    ("subprocess", "check_output"),
}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def find_blocking_calls(source: str, filename: str = "<test>") -> list[str]:
    """返回 ``文件:行 调用`` 形式的阻塞调用列表。"""
    tree = ast.parse(source, filename=filename)
    found: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            # 嵌套同步函数体不在这里执行（约定是交给 to_thread）
            if isinstance(child, (ast.FunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else None
                if name in BLOCKING_ATTRS:
                    found.append(f"{filename}:{child.lineno} {name}()")
                dotted = _dotted(func)
                for prefix, meth in QUALIFIED_BLOCKING:
                    if dotted == f"{prefix}.{meth}" or dotted.endswith(f".{prefix}.{meth}"):
                        found.append(f"{filename}:{child.lineno} {dotted}()")
            walk(child)

    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            walk(node)
    return found


class AsyncBlockingTests(unittest.TestCase):
    def test_server_has_no_blocking_calls_in_async_functions(self):
        offenders: list[str] = []
        for path in sorted((ROOT / "server").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            offenders.extend(
                find_blocking_calls(path.read_text(encoding="utf-8"), path.name)
            )
        self.assertEqual(offenders, [], "async 函数里出现同步阻塞调用：\n" + "\n".join(offenders))

    def test_checker_would_catch_the_known_regression(self):
        """自检：检查器必须能抓到 preview 那次事故的原始写法。"""
        bad = '''
async def preview_knowledge(file):
    text = await asyncio.to_thread(preview, file)
    existing = "\\n".join(p.read_text(encoding="utf-8") for p in kb_files())
    return existing
'''
        self.assertEqual(len(find_blocking_calls(bad)), 1)

    def test_checker_allows_sync_helper_and_async_sleep(self):
        good = '''
async def handler(file):
    await asyncio.sleep(0.1)
    return await asyncio.to_thread(_scan, file)


def _scan(file):
    return "\\n".join(p.read_text(encoding="utf-8") for p in kb_files())
'''
        self.assertEqual(find_blocking_calls(good), [])

    def test_checker_catches_time_sleep_but_not_asyncio_sleep(self):
        self.assertEqual(
            find_blocking_calls("async def f():\n    time.sleep(1)\n"), ["<test>:2 time.sleep()"]
        )
        self.assertEqual(find_blocking_calls("async def f():\n    await asyncio.sleep(1)\n"), [])


if __name__ == "__main__":
    unittest.main()
