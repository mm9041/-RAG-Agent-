"""清理不属于任何用户的检查点线程（测试残留）。

判据：thread_id 不匹配 `user-<数字>` 或 `user-<数字>-...`。这类线程永远不会出现在
任何人的会话列表里（`list_threads` 按 `user-{id}` 前缀过滤），只会在启动时白占
反序列化开销。

安全措施：
1. 先打印将要删除的清单，`--dry-run` 只打印不动手
2. 删除前用 sqlite 的 backup API 做一份一致快照（直接复制文件会漏掉 WAL 里的内容）
3. 只删匹配判据的，`user-\\d+` 前缀的一条不动

用法：
    python tools/purge-orphan-threads.py --dry-run
    python tools/purge-orphan-threads.py --yes [--vacuum]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "checkpoints.sqlite"
KEEP = re.compile(r"^user-\d+(-|$)")


def orphans(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT thread_id, COUNT(*) FROM checkpoints WHERE checkpoint_ns = '' GROUP BY thread_id"
    ).fetchall()
    return [(t, n) for t, n in rows if not KEEP.match(t)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印，不删除")
    parser.add_argument("--yes", action="store_true", help="确认执行删除")
    parser.add_argument("--vacuum", action="store_true", help="删完回收空间（需要独占，先停服务）")
    args = parser.parse_args()

    if not DB.exists():
        print(f"找不到 {DB}")
        return 1

    conn = sqlite3.connect(DB)
    targets = orphans(conn)
    total_points = sum(n for _, n in targets)
    print(f"待清理线程 {len(targets)} 个，共 {total_points} 个检查点：")
    for thread_id, count in sorted(targets, key=lambda x: -x[1]):
        print(f"  {count:3} 检查点  {thread_id}")

    if not targets:
        conn.close()
        return 0
    if args.dry_run or not args.yes:
        print("\n（未删除。加 --yes 执行）")
        conn.close()
        return 0

    backup = DB.with_name(f"checkpoints.sqlite.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with sqlite3.connect(backup) as dst:
        conn.backup(dst)
    print(f"\n已备份到 {backup.name}（{backup.stat().st_size / 1024 / 1024:.1f} MB）")

    from langgraph.checkpoint.sqlite import SqliteSaver

    saver = SqliteSaver(conn)
    for thread_id, _ in targets:
        saver.delete_thread(thread_id)
    conn.commit()

    remaining = orphans(conn)
    print(f"删除完成：剩 {len(remaining)} 个残留线程")
    left = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    print(f"检查点总数：{left}")

    if args.vacuum:
        conn.execute("VACUUM")
        conn.commit()
        print("已 VACUUM 回收空间")

    conn.close()
    print(f"库文件：{DB.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
