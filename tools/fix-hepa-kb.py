"""统一 HEPA 滤网的水洗口径（2026-10-31）。

背景：知识库里同时存在两种相反说法——
  data/扫地机器人100问2.txt  「滤网可每月用清水冲洗」
  data/维护保养.txt          「每1-2个月用清水冲洗（不可揉搓）」
  data/扫地机器人100问.pdf    「水洗会降低过滤效果」
模型拿到任一组原文都可能给出误导结论，此前靠 agent/grounding.py 的
wash_clarification 预置话术兜底。改为从资料层面消除矛盾后，话术可以去掉。

写法上不是二选一，而是按型号分条件：现实里 HEPA 确实分可水洗
（PET/不锈钢等）与不可水洗（玻璃纤维）两类，写成条件分支才既准确又自洽。

走 knowledge_admin.publish 而不是直接改文件：它会备份旧版本到 kb_versions、
原子写、并触发索引重建，失败自动回滚。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 从 tools/ 直接运行时，项目根不在 sys.path 上
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.env import ensure_env_loaded

ensure_env_loaded()

from utils.knowledge_admin import publish  # noqa: E402

EDITS: dict[str, list[tuple[str, str]]] = {
    "扫地机器人100问2.txt": [
        (
            "- 滤网可每月用清水冲洗，阴凉处晾干后使用，不可暴晒，使用3-6个月后建议更换新滤网。",
            "- 取决于型号，先看滤网或说明书上的清洗标识。标注“可水洗”的型号：可每月用清水冲洗，"
            "阴凉处晾干后使用，不可暴晒；未标注“可水洗”的型号（如玻璃纤维滤网）：不可水洗，"
            "水洗会破坏滤纸结构、降低过滤效果，只能做干式清理。两类均建议3-6个月更换新滤网。",
        ),
        (
            "- 滤网堵塞会导致吸力下降、机器人异响，拆卸滤网后用软毛刷轻刷灰尘，清水冲洗后晾干装回。",
            "- 滤网堵塞会导致吸力下降、机器人异响，拆卸滤网后用软毛刷轻刷灰尘；"
            "标注“可水洗”的型号可再用清水冲洗后晾干装回，未标注“可水洗”的型号只做干式清理。",
        ),
    ],
    "维护保养.txt": [
        (
            "1. HEPA滤网：每周用软毛刷轻刷表面灰尘，每1-2个月用清水冲洗（不可揉搓），"
            "阴凉处晾干，3-6个月更换新滤网。",
            "1. HEPA滤网：能否水洗取决于型号，先看滤网或说明书上的清洗标识。"
            "标注“可水洗”的型号：每周用软毛刷轻刷表面灰尘，每1-2个月用清水冲洗（不可揉搓），"
            "阴凉处晾干后装回；未标注“可水洗”的型号（如玻璃纤维滤网）：只做干式清理，"
            "水洗会破坏滤纸结构、降低过滤效果。两类均建议3-6个月更换新滤网。",
        ),
    ],
}


def main() -> None:
    password = os.getenv("ADMIN_PASSWORD", "")
    if not password:
        raise SystemExit("ADMIN_PASSWORD 未配置，无法发布知识文件")

    for name, pairs in EDITS.items():
        path = os.path.join("data", name)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        for old, new in pairs:
            if old not in text:
                raise SystemExit(f"{name}: 未找到待替换文本，文件可能已被改动：{old[:40]}...")
            text = text.replace(old, new)
        message = publish(
            name, text.encode("utf-8"), password,
            model="全系", scope="说明书口径统一：HEPA 按型号区分是否可水洗",
            replace=True,
        )
        print(f"{name} -> {message}")


if __name__ == "__main__":
    main()
