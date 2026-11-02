"""测量 preview_knowledge 阻塞事件循环的时长。

做法：临时造一个"知识库变大"的目录（默认 60 个 1MB txt），直接调用
``preview_knowledge``，同时在循环里跑一个 5ms 周期的心跳任务，记录心跳的
最大延迟。延迟越大，说明这段时间整个事件循环被占住了 —— 期间所有其他
请求（包括正在流的 SSE）都会一起停摆。

用法：
    python tools/loop-lag-probe.py            # 默认 60×1MB
    python tools/loop-lag-probe.py 20 2       # 20 个 2MB
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.routers import admin  # noqa: E402


class FakeUpload:
    """只实现 preview_knowledge 用到的那一小块。"""

    def __init__(self, name: str, content: bytes) -> None:
        self.filename = name
        self._content = content

    async def read(self) -> bytes:
        return self._content


async def measure(files: int, mb: int, legacy: bool = False, idle: bool = False) -> None:
    root = Path(tempfile.mkdtemp(prefix="lag-probe-"))
    try:
        unit = "扫地机器人日常维护：清理尘盒、检查边刷、擦拭传感器。".encode()
        target = mb * 1024 * 1024
        payload = (unit * (target // len(unit) + 1))[:target]
        for i in range(files):
            (root / f"doc-{i:03d}.txt").write_bytes(payload)

        admin._data_root = lambda: root  # type: ignore[assignment]
        admin._kb_files = lambda: sorted(  # type: ignore[assignment]
            p for p in root.iterdir() if p.is_file() and p.suffix.lower() in (".txt", ".pdf")
        )

        total_mb = files * len(payload) / 1024 / 1024
        print(f"知识库规模：{files} 个文件 × {len(payload)/1024/1024:.1f} MB ≈ {total_mb:.0f} MB")

        upload = lambda: FakeUpload("new-doc.txt", "新资料：滤网每 3 个月更换。".encode())

        # 预热：第一次 to_thread 要创建线程池、首次导入 knowledge_admin，
        # 这些一次性开销不算在稳态里。
        await admin.preview_knowledge(file=upload(), _="probe-token")

        max_lag = 0.0
        samples = 0
        stop = asyncio.Event()
        lags: list[tuple[float, float]] = []
        started = time.perf_counter()

        async def heartbeat() -> None:
            nonlocal max_lag, samples
            tick = 0.005
            while not stop.is_set():
                t0 = time.perf_counter()
                await asyncio.sleep(tick)
                lag = time.perf_counter() - t0 - tick
                samples += 1
                max_lag = max(max_lag, lag)
                if lag > 0.01:
                    lags.append((t0 - started, lag))

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)  # 先让心跳跑稳

        t0 = time.perf_counter()
        if idle:
            # 对照：不做任何请求，只让心跳跑同样长的时间，得到这台机器的噪声底。
            await asyncio.sleep(0.15)
            body = {"warnings": []}
        elif legacy:
            # 旧写法：在协程里直接读全部 txt，只把 conflict_hints 丢进线程。
            # 保留它只为对照，生产代码里不要出现。
            from utils.knowledge_admin import conflict_hints, preview as parse

            text = await asyncio.to_thread(parse, "new-doc.txt", upload()._content)
            existing = "\n".join(
                p.read_text(encoding="utf-8", errors="replace")
                for p in admin._kb_files()
                if p.name != "new-doc.txt" and p.suffix.lower() == ".txt"
            )
            body = {"warnings": await asyncio.to_thread(conflict_hints, text, existing)}
        else:
            body = await admin.preview_knowledge(file=upload(), _="probe-token")
        elapsed = time.perf_counter() - t0

        stop.set()
        await hb

        print(f"  接口耗时      : {elapsed * 1000:7.1f} ms")
        print(f"  心跳最大延迟  : {max_lag * 1000:7.1f} ms   ← 这段期间循环被占住")
        print(f"  心跳采样次数  : {samples}")
        if lags:
            print("  超过 10ms 的停顿：")
            for offset, lag in lags[:8]:
                print(f"    +{offset*1000:7.1f} ms 起，停顿 {lag*1000:6.1f} ms")
        else:
            print("  超过 10ms 的停顿：无")
        print(f"  返回 warnings : {body['warnings']}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    legacy = "--legacy" in sys.argv
    idle = "--idle" in sys.argv
    n = int(args[0]) if len(args) > 0 else 60
    size = int(args[1]) if len(args) > 1 else 1
    mode = "空转对照（不发请求）" if idle else ("旧写法（协程内同步读）" if legacy else "当前代码（全部 off-loop）")
    print(f"模式：{mode}")
    asyncio.run(measure(n, size, legacy, idle))
