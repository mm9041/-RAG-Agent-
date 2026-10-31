"""报告链路验证：发一条"生成使用报告"，打印工具调用序列与最终报告。

为什么单独写一个：报告模式的正确性不体现在延迟，而体现在
「调了哪些工具、按什么顺序、最终答复是否遵循报告规范」，
bench.py 只量时间不够用。
"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
# 每次跑用新会话，避免上一轮的记录/上下文影响判断
THREAD = f"user-1001-reporttest-{int(time.time())}"
QUESTION = "生成我的 2025年12月使用报告"


def main() -> None:
    body = json.dumps({
        "query": QUESTION,
        "thread_id": THREAD,
        "request_id": f"report-{int(time.time() * 1000)}",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/chat", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )

    started = time.monotonic()
    tools: list[str] = []
    answer = ""
    event = None

    with urllib.request.urlopen(request, timeout=180) as response:
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

            if event == "tool_start":
                name = (payload or {}).get("name", "")
                tools.append(name)
                print(f"  [{elapsed:5.1f}s] → 调用 {name} {json.dumps((payload or {}).get('args', {}), ensure_ascii=False)}")
            elif event == "tool_end":
                content = str((payload or {}).get("content", ""))[:70].replace("\n", " ")
                print(f"  [{elapsed:5.1f}s] ← 返回 {content}")
            elif event == "phase" and "报告" in str(payload):
                print(f"  [{elapsed:5.1f}s] # {payload}")
            elif event == "final":
                answer = str(payload or "")
            elif event == "error":
                print("  流内错误:", payload)

    print(f"\n总耗时 {time.monotonic() - started:.1f}s | 工具序列: {' → '.join(tools) or '（无）'}")
    print(f"答复 {len(answer)} 字\n")
    print(answer[:900])
    print("\n--- 检查 ---")
    print("含报告标题:", "黑马程序员扫地机器人使用情况报告与保养建议" in answer)
    print("调用了 enter_report_mode:", "enter_report_mode" in tools)
    print("调用了 fetch_external_data:", "fetch_external_data" in tools)
    print("调用了 get_current_month:", "get_current_month" in tools)


if __name__ == "__main__":
    main()
