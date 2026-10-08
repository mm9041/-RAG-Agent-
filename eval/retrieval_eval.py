"""
检索质量评估（**块级**判据 + 双判据交叉验证）

用法：
    python -m eval.retrieval_eval              # 关键词判据，评估当前配置
    python -m eval.retrieval_eval --judge      # 同时用「独立模型判据」并列出分歧
    python -m eval.retrieval_eval --set=b              # 换用评估集 B（口语化问法）
    python -m eval.retrieval_eval --refusals           # 附加"该拒答的问题是否拒答"检查
    python -m eval.retrieval_eval --matrix     # 对比重排开关与 fetch_k
    python -m eval.retrieval_eval --matrix --judge
    python -m eval.retrieval_eval --judge --refusals --snapshot        # 拍/更新基线
    python -m eval.retrieval_eval --judge --refusals --check-baseline  # 与基线比对

━━ 为什么有基线比对（--snapshot / --check-baseline）━━
本项目已经吃过两次**静默质量回退**：换 embedding 后沿用旧的距离阈值、
以及在"文件级 hit@k"这个误导性指标下得出"rerank 没必要"。两次的共同点是
**不报错，只是某些问题突然答不出**，而"只有评估集能发现"这句结论漏了后半句 ——
**没人把数字存下来，就没有人能比对**。所以"换模型必须重跑评估"这条规矩
要变成一个会红的检查。比对逻辑、以及"配置指纹不一致时为什么不比数字"见 eval/baseline.py。

━━ 为什么是"块级"而不是"文件级" ━━
这个区别是本项目踩过的最大的坑，务必记住：

  文件级 hit@k 只判断"正确的**文件**在不在 top-k 里"。
  但一个文件里有几百条条目，**命中文件 ≠ 命中答案所在的块**。

实例：本库文件级 hit@3 曾测得 12/12 (100%)，看着完美；
换成块级判据后只有 7/12 (58%) —— 约四成问题的答案块根本没进 top-3，
而文件级指标把这一切掩盖了，并直接导致了一个错误结论（"rerank 没必要"）。

━━ 两种判据，各有缺陷，所以交叉验证 ━━

**A. 关键词判据（默认）**：给每题一个"答案必备关键词"，看 top-k 的块里有没有哪块包含它。
  - 关键词太宽 -> 命中同词不同义的块 = **假阳性**
    （实例：用「防撞条」时被"防撞条是否卡顿"命中，那是在讲碰撞家具）；
  - 关键词太严 -> 答案换了说法就判错 = **假阴性**
    （实例：问"拖布硬化怎么处理"，召回的块原文就是"拖布硬化…用温水浸泡"，
      但不含预设的「拖布是否脏污」，被判为未命中）。

**B. 模型判据（`--judge`）**：让独立模型读「问题 + 召回的块」，
  直接判断"仅凭这些资料能否回答问题"。不受关键词措辞影响，
  但自身有误差，且与生成答案的模型同源（存在自我偏好）。

⚠️ **两者不一致的用例才是最有价值的信息** —— 说明"系统"或"判据"至少一方有问题。
   此时**人工看一眼召回的块**，先确认是系统没召回，还是判据不贴合，再下结论。
"""
import os
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from eval.baseline import (BASELINE_RELATIVE_PATH, compare_baseline,
                           format_problems, has_failures, load_baseline,
                           merge_snapshot, save_baseline)
from model.factory import get_chat_model
from rag.vector_store import VectorStoreService
from utils.config_handler import chroma_conf, model_conf, agent_conf, prompts_conf
from utils.path_tool import get_abs_path

# ── 评估集 A：20 题（第一批，偏"知识库里的陈述性事实"）──────────────────
EVAL_CASES_A: list[tuple[str, str]] = [
    ("吸力应该选多大才够用？", "3000Pa"),
    ("小户型怎么选扫地机器人？", "小户型"),
    ("机身高度和床底高度怎么匹配？", "机身高度"),
    ("拖地时出水量可以调节吗？", "出水量可调"),
    ("机器人开机没反应怎么办？", "电源适配器"),
    ("机器人连不上WiFi怎么排查？", "WiFi"),
    ("主刷转速变慢怎么办？", "主刷旋转速度变慢"),
    ("防撞条缝隙的毛发怎么清理？", "防撞条缝隙"),
    ("驱动轮缠绕毛发怎么处理？", "驱动轮"),
    ("滤网多久更换一次？", "滤网"),
    ("尘盒卡扣有杂物会怎样？", "尘盒卡扣"),
    ("充电触点氧化怎么处理？", "充电触点"),
    ("首次使用扫地机器人需要做什么？", "首次使用"),
    ("机器人找不到充电座怎么办？", "充电座前方"),
    ("传感器异常报警怎么处理？", "传感器异常"),
    ("扫拖一体机器人可以只扫地不拖地吗？", "只扫地不拖地"),
    ("先扫后拖的清洁流程怎么设置？", "先扫后拖"),
    ("拖布硬化了怎么处理？", "拖布是否脏污"),
    ("LDS 激光导航和 VSLAM 视觉导航哪个更好？", "VSLAM"),
    ("dToF 导航技术是什么？", "dToF"),
]

# ── 评估集 B：20 题（第二批，**口语化问法**，检验 A 集结论是否可泛化）──────
# 出 B 集的目的：A 集是我按知识库内容自拟的、偏书面问法，可能过拟合。
# B 集刻意换成用户真实口吻（"很吵""出不来""地上全是水渍"），
# 并覆盖 A 集没考的方向（耗材/存放/环境适配/自动洗拖布机型）。
# 关键词同样逐个核对过：必须**只出现在真正回答该问题的块里**，
# 不能是"命中同词不同义"的宽词（这正是 A 集踩过的坑）。
# ⚠️ 2026-09-20 修正过 4 条判据关键词/问法，原委记录在此（都有人工核对证据）：
#   1. "扫不到墙角怎么办？/动态边刷"  —— 「动态边刷」出自**选购建议**，而问法是**故障排查**；
#      召回的块讲的是"检查边刷是否磨损"。改为与内容匹配的问法。
#   2. "拖完地地上全是水渍/干拖模式收尾" —— 该短语出自**没被召回**的块；
#      召回的块里写的是"避免快速拖地留下水痕"。
#   3. "尘盒应该多久清理一次？/尘盒卡扣" —— 「尘盒卡扣」是**另一件事**（清理卡扣杂物），
#      而问的是**清理频率**；召回的块原句是"至少每 2-3 次清扫清理一次"。
#   4. "机器人卡在沙发底下出不来/沙发底" —— 「沙发底」命中的是"选择机身高度"那块，
#      并不回答"出不来"。改为与内容匹配的问法。
# 教训：**关键词必须是"答案那句话里的特征短语"，而不是"语料里出现次数少的词"**。
#   我最初按"唯一性"选词，结果得到一批不回答该问题的判据 —— 判据的失败率因此虚高。
EVAL_CASES_B: list[tuple[str, str]] = [
    ("想扫到墙角该怎么选机型？", "动态边刷"),
    ("拖完地地上全是水渍", "避免快速拖地"),
    ("电池充不进电怎么办？", "电池充不进电"),
    ("尘盒应该多久清理一次？", "至少每 2-3 次清扫"),
    ("滤网可以直接水洗吗？", "可水洗滤网"),
    ("拖布发硬板结怎么处理？", "板结"),
    ("家里养宠物要注意什么？", "宠物专属"),
    ("APP 上清扫记录显示不对", "清扫记录显示错误"),
    ("灰尘从尘盒里漏出来", "尘盒安装后漏灰"),
    ("机器人工作时很吵", "噪音过大"),
    ("一次性拖布可以用水洗吗？", "一次性拖布"),
    ("机器人会从楼梯上掉下去吗？", "防跌落功能"),
    ("怎么设置禁区不让机器人进去？", "虚拟墙"),
    ("边刷磨损了怎么更换？", "边刷磨损"),
    ("水箱漏水怎么处理？", "水箱盖是否拧紧"),
    ("家具底部太矮机器人钻不进去", "测量家具底部高度"),
    ("清理刷多久换一次？", "清洁刷"),
    ("机器人长期不用该怎么存放？", "长期存放"),
    ("机器人在镜面前乱撞", "镜面"),
    ("散热口需要清理吗？", "散热口"),
]

# ── 拒答用例：知识库里**确实没有**的问题，必须被正确拒答 ──────────────
# 判据是**走完整工具链路**后，rag_summarize 是否返回 EMPTY_RETRIEVAL_MARKER 哨兵。
# 这组用例是"能否安全去掉 max_distance"的依据：
# 去掉阈值后，"没资料"改由模型的 [无覆盖] 标记判定，那这条能力就必须被持续测量，
# 而不能只靠临时脚本验证过一次。
REFUSAL_CASES: list[str] = [
    "量子计算机的退相干时间怎么延长？",      # 完全离题
    "今天 A 股大盘走势如何",                # 完全离题
    "推荐几本番茄种植的书",                  # 完全离题
    "扫地机器人的市场占有率是多少？",          # 半相关：话题沾边但库里没有
    "扫地机器人能防贼吗？",                  # 半相关
    "扫地机器人行业有哪些专利诉讼？",          # 半相关
]

SETS = {"a": EVAL_CASES_A, "b": EVAL_CASES_B}

# 兼容旧调用（老脚本 `from eval.retrieval_eval import EVAL_CASES`）
EVAL_CASES = EVAL_CASES_A

# --matrix 要对比的配置：(标签, 是否启用重排, fetch_k)
MATRIX: list[tuple[str, bool, int]] = [
    ("朴素 top-3（无重排）", False, 3),
    ("rerank fetch_k=10", True, 10),
    ("rerank fetch_k=20", True, 20),
]

JUDGE_PROMPT = PromptTemplate.from_template(
    "下面是从知识库检索到的资料片段。\n\n"
    "用户问题：{question}\n\n"
    "资料：\n{context}\n\n"
    "请判断：**仅凭上述资料**，能否回答用户的问题？\n"
    "- 只要能部分回答、或给出了相关做法，就算「能」\n"
    "- 资料与该问题无关、或完全没提到，才算「不能」\n"
    "只输出 YES 或 NO，不要任何其它内容。"
)

_JUDGE_CHAIN = None


def judge_with_model(question: str, chunks: list[str]) -> bool:
    """让模型判断"仅凭这些资料能否回答问题"。"""
    global _JUDGE_CHAIN
    if _JUDGE_CHAIN is None:
        _JUDGE_CHAIN = JUDGE_PROMPT | get_chat_model() | StrOutputParser()

    verdict = _JUDGE_CHAIN.invoke({"question": question,
                                   "context": "\n\n".join(chunks)})
    return verdict.strip().upper().startswith("YES")


def evaluate(store: VectorStoreService, cases: list[tuple[str, str]],
             use_judge: bool = False) -> tuple[int, int, list[tuple]]:
    """在给定评估集上评估。返回 (关键词命中数, 模型命中数, 逐题明细)

    明细每项：(问题, 关键词, 关键词是否命中, 模型是否命中或 None)
    """
    keyword_hits = model_hits = 0
    rows: list[tuple] = []

    for query, keyword in cases:
        chunks = [doc.page_content for doc, _ in store.retrieve(query).hits]
        keyword_ok = any(keyword in chunk for chunk in chunks)
        keyword_hits += keyword_ok

        model_ok = None
        if use_judge:
            model_ok = judge_with_model(query, chunks) if chunks else False
            model_hits += model_ok

        rows.append((query, keyword, keyword_ok, model_ok))

    return keyword_hits, model_hits, rows


def evaluate_refusals() -> tuple[int, list[str]]:
    """走**完整工具链路**验证拒答能力（见 REFUSAL_CASES 的说明）"""
    from agent.runtime import EMPTY_RETRIEVAL_MARKER
    from agent.tools.agent_tools import rag_summarize

    passed, leaked = 0, []
    for query in REFUSAL_CASES:
        output, _ = rag_summarize.func(query, SimpleNamespace(context={"report": True}))
        if output.startswith(EMPTY_RETRIEVAL_MARKER):
            passed += 1
        else:
            leaked.append(query)
    return passed, leaked


def report(rows: list[tuple], keyword_hits: int, model_hits: int,
           total: int, use_judge: bool, indent: int = 2) -> None:
    pad = " " * indent
    print(f"{pad}关键词判据 hit@3 = {keyword_hits:2d}/{total} = {keyword_hits / total:>3.0%}")

    if not use_judge:
        misses = [row for row in rows if not row[2]]
        if misses:
            print(f"{pad}未命中（加 --judge 可交叉验证是系统问题还是判据问题）：")
            for query, keyword, _, _ in misses:
                print(f"{pad}  「{query}」  期望含 {keyword!r}")
        return

    print(f"{pad}模型判据   hit@3 = {model_hits:2d}/{total} = {model_hits / total:>3.0%}")

    disagree = [row for row in rows if row[2] != row[3]]
    if disagree:
        print(f"{pad}⚠️ 两判据分歧 {len(disagree)} 例（**这些最该人工核对**）：")
        for query, keyword, keyword_ok, model_ok in disagree:
            reason = ("关键词说没答上，模型说能答"
                      if model_ok else "关键词说命中，模型说答不上")
            print(f"{pad}  「{query}」 期望含 {keyword!r} -> {reason}")
    else:
        print(f"{pad}两判据无分歧 ✅")


def config_fingerprint(store: VectorStoreService) -> dict:
    """当前会影响检索质量的配置集合，随基线一起存。

    为什么要存指纹：本项目吃过"换了 embedding 但沿用旧阈值"的亏 ——
    数字变了却没人知道是配置变了还是质量回退。所以**指纹不一致就直接判过期**，
    宁可让人重拍一次基线，也不要拿旧数字比出一个假的"回退/改善"。
    """
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {
        "corpus_hash": digest({os.path.relpath(k, store.data_path): v for k, v in store._scan_files().items()}),
        "prompts_hash": digest({k: Path(get_abs_path(v)).read_text(encoding="utf-8") for k, v in prompts_conf.items()}),
        "cases_hash": digest({"sets": SETS, "refusals": REFUSAL_CASES}),
        "rag_raw_documents": bool(agent_conf.get("rag_raw_documents", False)),
        "embedding_model": model_conf.get("embedding_model_name"),
        "chat_model_name": model_conf.get("chat_model_name"),
        "rerank_model": chroma_conf.get("rerank_model"),
        "rerank_enabled": chroma_conf.get("rerank_enabled"),
        "max_distance": chroma_conf.get("max_distance"),
        "k": chroma_conf.get("k"),
        "rerank_fetch_k": chroma_conf.get("rerank_fetch_k"),
        "chunk_size": chroma_conf.get("chunk_size"),
        "chunk_overlap": chroma_conf.get("chunk_overlap"),
        "blocks": store.count(),
    }


def handle_baseline(store: VectorStoreService, sets_result: dict,
                    refusals_result: dict | None) -> None:
    """按 --snapshot / --check-baseline 写基线，或与基线比对"""
    path = get_abs_path(BASELINE_RELATIVE_PATH)
    current = {"fingerprint": config_fingerprint(store), "sets": sets_result}
    if refusals_result:
        current["refusals"] = refusals_result

    if "--snapshot" in sys.argv:
        save_baseline(path, merge_snapshot(load_baseline(path), current))
        print(f"\n已写入基线：{path}")
        print(f"  评估集 {sorted(sets_result)}"
              + ("  + 拒答检查" if refusals_result else ""))
        return

    if "--check-baseline" not in sys.argv:
        return

    problems = compare_baseline(load_baseline(path), current, allow_config_change="--compare-config" in sys.argv)
    print("\n── 与基线比对 ──")
    print(format_problems(problems))
    if has_failures(problems):
        # 非 0 退出码是给脚本/CI 用的：让"跑完了但变差了"不可能被当成成功
        sys.exit(1)


def main() -> None:
    set_name = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--set=")), "a")
    cases = [case for group in SETS.values() for case in group] if set_name == "all" else SETS.get(set_name)
    if cases is None:
        print(f"未知评估集 {set_name!r}，可用：{sorted(SETS)}")
        return

    total = len(cases)
    use_judge = "--judge" in sys.argv
    sets_result: dict[str, dict] = {}
    refusals_result: dict | None = None

    print(f"评估集 {set_name.upper()}：{total} 题"
          f"（块级判据：召回的块里是否含答案必备关键词）"
          f"{' + 独立模型判据' if use_judge else ''}\n")

    if "--matrix" not in sys.argv:
        started = time.time()
        store = VectorStoreService()
        store.load_document()
        print(f"当前配置：rerank={chroma_conf.get('rerank_enabled')} "
              f"fetch_k={chroma_conf.get('rerank_fetch_k')} "
              f"max_distance={chroma_conf.get('max_distance')} "
              f"chunk_size={chroma_conf.get('chunk_size')}")
        groups = SETS if set_name == "all" else {set_name: cases}
        for name, group in groups.items():
            keyword_hits, model_hits, rows = evaluate(store, group, use_judge)
            print(f"\n评估集 {name.upper()}")
            report(rows, keyword_hits, model_hits, len(group), use_judge)
            sets_result[name] = {"keyword": keyword_hits,
                                "model": model_hits if use_judge else None,
                                "total": len(group)}

        if "--refusals" in sys.argv:
            passed, leaked = evaluate_refusals()
            n_ref = len(REFUSAL_CASES)
            refusals_result = {"passed": passed, "total": n_ref}
            print(f"  拒答判据 = {passed}/{n_ref} = {passed / n_ref:.0%}"
                  f"" + (f"  ⚠️ 漏拒答：{leaked}" if leaked else " ✅"))

        handle_baseline(store, sets_result, refusals_result)
        print(f"  （耗时 {time.time() - started:.1f}s）")
    else:
        if "--snapshot" in sys.argv or "--check-baseline" in sys.argv:
            print("  （--matrix 会故意改配置，结果与基线不可比：本次不写也不比对）")

        original = (chroma_conf.get("rerank_enabled"), chroma_conf.get("rerank_fetch_k"))
        for label, enabled, fetch_k in MATRIX:
            chroma_conf["rerank_enabled"] = enabled
            chroma_conf["rerank_fetch_k"] = fetch_k
            store = VectorStoreService()
            store.load_document()
            keyword_hits, model_hits, rows = evaluate(store, cases, use_judge)
            print(f"  {label}")
            report(rows, keyword_hits, model_hits, total, use_judge, indent=4)
        chroma_conf["rerank_enabled"], chroma_conf["rerank_fetch_k"] = original


if __name__ == "__main__":
    main()
