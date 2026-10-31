"""真实模型延迟基准：直接打 SSE 接口，量首字 / final / 结束时间。

为什么要走 HTTP 而不是直接调 ReactAgent：前端看到的延迟还包含
SSE 桥接、事件分发、网络往返，只量 agent 内部会低估用户体感。

用法：
    python tools/bench.py --label before
    python tools/bench.py --label after
结果写 eval/bench-<label>.json，并打印汇总表。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
USER = "1001"

QUESTIONS = [
    ("闲聊 · 无工具", "你好"),
    ("检索 · HEPA", "HEPA 滤网多久需要更换一次？"),
    ("检索 · 吸力", "扫地机器人吸力多大合适？"),
]


def ask(question: str, thread_id: str) -> dict:
    body = json.dumps({
        "query": question,
        "thread_id": thread_id,
        "request_id": f"bench-{int(time.time() * 1000)}",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/chat", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )

    started = time.monotonic()
    first_content = None
    final_at = None
    phases: list[list] = []
    answer = ""
    error = None

    with urllib.request.urlopen(request, timeout=180) as response:
        event = None
        for raw in response:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[5:].strip()).get("d")
            except (ValueError, AttributeError):
                continue

            elapsed = time.monotonic() - started
            if event == "phase":
                phases.append([round(elapsed, 2), str(payload)])
            elif event == "content" and payload and first_content is None:
                first_content = elapsed
            elif event == "final":
                final_at = elapsed
                answer = str(payload or "")
            elif event == "error":
                error = str(payload)

    return {
        "question": question,
        "first_content_s": round(first_content, 2) if first_content is not None else None,
        "final_s": round(final_at, 2) if final_at is not None else None,
        "total_s": round(time.monotonic() - started, 2),
        "answer_len": len(answer),
        "answer_head": answer[:90],
        "is_fallback": "暂未能核实完整答复" in answer,
        "error": error,
        "phases": phases,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    args = parser.parse_args()

    rows = []
    for index, (name, question) in enumerate(QUESTIONS, start=1):
        thread = f"user-{USER}-bench-{args.label}-{index}"
        print(f"[{name}] {question} ...", flush=True)
        result = ask(question, thread)
        result["name"] = name
        rows.append(result)
        print(f"   首字 {result['first_content_s']}s | final {result['final_s']}s "
              f"| 总长 {result['answer_len']} 字 | 回退={result['is_fallback']}", flush=True)

    out = Path(__file__).resolve().parent.parent / "eval" / f"bench-{args.label}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")


if __name__ == "__main__":
    main()
