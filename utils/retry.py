"""
带退避的重试工具

为什么需要它（2026-09-20 被两次线上故障逼出来的）：
项目里所有外部调用原本**没有任何显式超时或重试**，全靠 SDK 默认值。而实测默认行为不够：
openai 客户端默认 `max_retries=2`，但它**只按 HTTP 状态码重试 408/409/429/5xx**；
DashScope 把限流报成 **400**（body 里写 `"type": "ServiceUnavailable"`）、
把额度用尽报成 **403** —— 两者都不在重试范围内，于是一次抖动就直接失败。
把限流打在建库过程中尤其糟：全量重建是"先 drop 再写"，失败会留下半成品。

**关键在分类 —— 不是所有错误都该重试：**

| 情况 | 例子 | 该重试吗 |
|---|---|---|
| 限流 / 服务暂时不可用 | `Too many requests`、`ServiceUnavailable`、429、503 | ✅ 等一会儿能好 |
| 网络抖动 | 超时、连接被重置 | ✅ 通常能好 |
| **额度用尽** | `Free quota exhausted`、`AllocationQuota` | ❌ **等几秒不会变好，重试纯属浪费** |
| 鉴权/参数错 | `Access denied`、`Model not exist`、401 | ❌ 重试无用 |

所以这里**按错误消息分类**（而不是只用异常类型）——
因为 DashScope 会把"限流"和"模型不存在"都包成同一个 400。
"""
import time

from utils.logger_handler import logger

# 命中这些标记 -> 可重试（等一会儿可能就好）
_TRANSIENT_MARKERS = (
    "too many requests", "throttl", "rate limit", "rate_limit",
    "serviceunavailable", "service unavailable", "temporarily unavailable",
    "timed out", "timeout", "connection reset", "connection aborted",
    "connection error", "remote end closed", "server disconnected",
    "bad gateway", "gateway timeout", "internal server error",
)

# 命中这些标记 -> 不可重试（重试也不会好，纯浪费时间）
_PERMANENT_MARKERS = (
    "quota exhausted", "allocationquota", "insufficient balance", "arrears",
    "access denied", "model not exist", "invalid api key", "unauthorized",
    "authentication", "permission denied",
)

# 状态码兜底（消息里没有可用标记时用）
_TRANSIENT_STATUS = (408, 409, 429, 500, 502, 503, 504)


def is_retryable(error: BaseException) -> bool:
    """判断这个异常是否值得重试。

    先看"不可重试"标记 —— 它更具体。
    例：`Access denied... 403` 里既有 403 也可能含别的词，必须先判永久类。
    """
    message = str(error).lower()

    if any(marker in message for marker in _PERMANENT_MARKERS):
        return False

    if any(marker in message for marker in _TRANSIENT_MARKERS):
        return True

    status = getattr(error, "status_code", None) or getattr(
        getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return status in _TRANSIENT_STATUS

    # 认不出来就不重试：宁可快速失败，也别在一个可能永久失败的问题上空转
    return False


def call_with_retry(callable_, *, what: str, retries: int = 3, backoff: float = 2.0):
    """执行 callable_，遇到**可重试**的错误就指数退避重试。

    退避间隔 = backoff * 2^i（默认 2s / 4s / 8s）。
    不可重试的错误**立即抛出**，不做无谓等待。
    """
    last_error: BaseException | None = None

    for attempt in range(retries + 1):
        try:
            return callable_()
        except Exception as error:          # noqa: BLE001 —— 分类交给 is_retryable
            last_error = error

            if attempt >= retries or not is_retryable(error):
                raise

            wait = backoff * (2 ** attempt)
            logger.warning(
                f"[重试]{what} 第 {attempt + 1}/{retries} 次失败"
                f"（{type(error).__name__}: {str(error)[:120]}），"
                f"{wait:.0f}s 后重试"
            )
            time.sleep(wait)

    raise last_error      # 理论上到不了这里
