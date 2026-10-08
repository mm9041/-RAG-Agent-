"""
评估基线快照：把「跑出来的数字」存成可对比的凭据（**纯逻辑，不联网、不调模型**）

为什么需要它（2026-09-20 的两次事故是同一个根因）：
换 embedding 后沿用旧的距离阈值、以及"rerank 到底有没有用"的反复，
都出现过**静默的质量回退** —— 不报错，只是某些问题突然答不出，
而"只有评估集能发现"这句话漏了后半句：**没人把数字存下来，所以没人会去比对**。
于是"换模型必须重跑评估"只是一条靠人记的规矩。

有了基线，它就变成一个会红的检查：
    python -m eval.retrieval_eval --set=a --judge --snapshot   # 拍基线
    python -m eval.retrieval_eval --set=a --judge --check-baseline  # 之后每次比对

刻意与 eval/retrieval_eval.py 分开：那个模块 import 了向量库与模型工厂，
本模块**只依赖 json/os**，所以守护它的单测真的是离线的（项目性质：51 个用例 1.6 秒）。

⚠️ 为什么本模块**不做成**联网单测：任何真实评估都要 embedding 调用，
把它塞进 unittest 会同时毁掉"零联网"和"零成本"两条性质，
还会因为额度耗尽而随机变红。跑评估是一条命令，比对结果才是被测试的逻辑。
"""
import json
import os

# 基线文件位置（相对项目根，由调用方经 get_abs_path 传入绝对路径）
BASELINE_RELATIVE_PATH = os.path.join("eval", "baseline.json")

# 参与指纹比对的配置项：这些变了，旧基线的数字就没有可比性。
# 依据本项目自己的教训 ——「凡是标定出来的常量，都要在它依赖的东西变化时重新标定」。
FINGERPRINT_KEYS = (
    "corpus_hash", "prompts_hash", "cases_hash", "rag_raw_documents",
    "embedding_model",
    "chat_model_name",      # ② [无覆盖] 是提示词契约，换 chat 模型会影响拒答判据
    "rerank_model",
    "rerank_enabled",
    "max_distance",
    "k",
    "rerank_fetch_k",
    "chunk_size",
    "chunk_overlap",
    "blocks",               # 库内块数：data/ 变了数字就该重拍
)


def compare_baseline(baseline: dict | None, current: dict, *, allow_config_change: bool = False) -> list[tuple[str, str]]:
    """比对基线与本次结果。**纯函数**。

    返回 [(级别, 说明)]，级别 ∈ {"fail", "warn"}；**空列表表示全部通过**。

    判级原则（宁可响亮失败，不要静默给错结果）：
      - **fail**：模型判据下降、拒答数下降、指纹不一致（含缺字段）、总数对不上、基线缺失；
      - **warn**：关键词判据下降、本次没跑到基线里的某个集合。
    关键词判据为什么只算 warn：它是本项目**已知不可靠**的那一个
    （口语化问法下会从 95% 掉到 75%，且造判据时翻过 8 次车），
    它掉下来更可能是措辞问题而不是系统问题；模型判据才是主依据。
    """
    problems: list[tuple[str, str]] = []

    if not baseline:
        return [("fail", "还没有评估基线：先跑一次 python -m eval.retrieval_eval --judge --snapshot")]

    old_fp = baseline.get("fingerprint") or {}
    new_fp = current.get("fingerprint") or {}

    if not old_fp or not new_fp:
        return [("fail", "基线或本次结果缺少配置指纹，无法比对（基线请重新 --snapshot）")]

    # 指纹不一致时**不比数字**：换了 embedding/重排/切分参数之后，
    # 新旧数字本来就该不同，比出来的"回退"或"改善"都是假的。
    missing = [key for key in FINGERPRINT_KEYS if key not in old_fp or key not in new_fp]
    if missing:
        return [("fail", f"未验证：配置指纹缺少 {', '.join(missing)}，需重新评估")]
    mismatched = [key for key in FINGERPRINT_KEYS
                  if key in old_fp and key in new_fp and old_fp[key] != new_fp[key]]
    if mismatched and (not allow_config_change or any(k in mismatched for k in ("corpus_hash", "cases_hash"))):
        detail = "、".join(f"{k}: {old_fp[k]!r} -> {new_fp[k]!r}" for k in mismatched)
        return [("fail", f"基线已过期（配置变了：{detail}）——"
                         "这不是质量回退，是基线不再适用。确认新配置后重跑 --snapshot")]

    if mismatched:
        problems.append(("warn", "对照实验：配置已变化，继续比较固定语料与题集上的成绩"))
    for name, old_set in (baseline.get("sets") or {}).items():
        new_set = (current.get("sets") or {}).get(name)
        if new_set is None:
            problems.append(("fail", f"评估集 {name.upper()} 本次没有跑，未验证"))
            continue

        if old_set.get("total") != new_set.get("total"):
            problems.append(("fail", f"评估集 {name.upper()} 题数变了："
                                     f"{old_set.get('total')} -> {new_set.get('total')}，题目集被改过"))
            continue

        old_model, new_model = old_set.get("model"), new_set.get("model")
        if old_model is None or new_model is None:
            problems.append(("fail", f"评估集 {name.upper()} 缺少模型判据，未验证，请加 --judge"))
        if old_model is not None and new_model is not None and new_model < old_model:
            problems.append(("fail", f"评估集 {name.upper()} 模型判据回退："
                                     f"{old_model}/{old_set['total']} -> {new_model}/{new_set['total']}"))

        old_kw, new_kw = old_set.get("keyword"), new_set.get("keyword")
        if old_kw is not None and new_kw is not None and new_kw < old_kw:
            problems.append(("warn", f"评估集 {name.upper()} 关键词判据下降："
                                     f"{old_kw} -> {new_kw}（该判据本身不可靠，人工看分歧用例）"))

    old_ref, new_ref = baseline.get("refusals"), current.get("refusals")
    if old_ref and new_ref:
        # 拒答是**结构性保证**：max_distance 关掉后，"没资料"完全靠模型的 [无覆盖] 约定，
        # 所以这一项掉下来没有任何商量的余地（见 config/chroma.yml 的决策记录）。
        if new_ref.get("total") != old_ref.get("total") or new_ref.get("passed") is None:
            problems.append(("fail", "拒答题数不一致或缺少成绩，未验证"))
        elif new_ref.get("passed") < old_ref.get("passed"):
            problems.append(("fail", f"拒答能力回退：{old_ref['passed']}/{old_ref.get('total')}"
                                     f" -> {new_ref['passed']}/{new_ref.get('total')}"
                                     "（[无覆盖] 约定失效，查 rag_summarize 提示词）"))
    elif old_ref and not new_ref:
        problems.append(("fail", "基线里有拒答检查但本次没跑，未验证 —— 加 --refusals"))

    return problems


def format_problems(problems: list[tuple[str, str]]) -> str:
    """把比对结果渲染成可读文本（也是纯函数，界面与命令行共用）"""
    if not problems:
        return "与基线一致 ✅"
    icon = {"fail": "✗", "warn": "!"}
    return "\n".join(f"  {icon.get(level, '?')} [{level}] {message}"
                     for level, message in problems)


def has_failures(problems: list[tuple[str, str]]) -> bool:
    return any(level == "fail" for level, _ in problems)


def load_baseline(path: str) -> dict | None:
    """读基线；文件不存在返回 None，**解析失败直接抛**（静默的坏基线比没有更危险）"""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_baseline(path: str, snapshot: dict) -> None:
    """写入基线。缺目录时自动建；**整份覆盖**，所以调用方要先 merge 再存
    （见 eval.retrieval_eval 里的 merge 逻辑 —— 分两次跑 A/B 集不该互相冲掉）。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


def merge_snapshot(old: dict | None, new: dict) -> dict:
    """把本次结果并进已有基线（按评估集名与拒答分别覆盖，其余保留）"""
    merged = dict(old or {}) if old and old.get("fingerprint") == new["fingerprint"] else {}
    merged["fingerprint"] = new["fingerprint"]
    sets = dict(merged.get("sets") or {})
    sets.update(new.get("sets") or {})
    merged["sets"] = sets
    if new.get("refusals"):
        merged["refusals"] = new["refusals"]
    merged["note"] = ("由 eval.retrieval_eval --snapshot 生成。"
                      "配置指纹任一项变化都会使基线过期，需重拍。")
    return merged
