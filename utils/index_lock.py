"""跨进程构建锁；检索无需持锁，读取已发布的不可变集合。"""
import os
import time
from contextlib import contextmanager

@contextmanager
def index_lock(path, timeout=30):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+b") as f:
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("知识库正在由其他进程更新，请稍后重试")
                time.sleep(0.1)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
