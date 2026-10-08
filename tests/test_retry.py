"""
重试逻辑的单元测试

这些用例**直接来自 2026-09-20 的两次真实故障**，把当时看到的原始报错
抄进断言里 —— 以后如果分类逻辑被改坏，会立刻被这些用例挡下来。

跑法：
    python -m unittest discover -s tests -v
"""
import unittest

from utils.retry import call_with_retry, is_retryable


# ── 真实报错原文（截取）─────────────────────────────────────────────
# 情况 A：DashScope 把限流报成 400 —— SDK 只按状态码重试，所以这个不会被重试
THROTTLED = (
    "Error code: 400 - {'error': {'message': 'Too many requests. Your requests are "
    "being throttled due to system capacity limits. Please try again later.', "
    "'type': 'ServiceUnavailable', 'code': 'ServiceUnavailable'}}"
)
# 情况 B：额度用尽（403）—— 等几秒不会变好，不该重试
QUOTA_EXHAUSTED = (
    "status=403, message=Free quota exhausted. To continue accessing the model on a "
    "paid basis, please add funds or disable the \"use free tier only\" mode"
)
# 情况 C：模型名写错（400）—— 不该重试
MODEL_NOT_EXIST = "status=400, message=Model not exist."
# 情况 D：无权限（403）—— 不该重试
ACCESS_DENIED = "status=403, message=Access denied. For details, see: https://..."

TRANSIENT = [THROTTLED,
             "Connection error: Read timed out.",
             "Error code: 503 - Service Unavailable",
             "rate limit exceeded"]
PERMANENT = [QUOTA_EXHAUSTED, MODEL_NOT_EXIST, ACCESS_DENIED,
             "invalid api key", "Unauthorized"]


class TestIsRetryable(unittest.TestCase):

    def test_真实限流应该重试(self):
        """DashScope 的限流报成 400，必须能被识别为可重试（否则建库会被一次抖动打挂）"""
        self.assertTrue(is_retryable(RuntimeError(THROTTLED)))

    def test_真实额度用尽不应重试(self):
        """额度用尽等几秒也不会好，重试纯属浪费时间"""
        self.assertFalse(is_retryable(RuntimeError(QUOTA_EXHAUSTED)))

    def test_真实模型不存在不应重试(self):
        self.assertFalse(is_retryable(RuntimeError(MODEL_NOT_EXIST)))

    def test_真实无权限不应重试(self):
        self.assertFalse(is_retryable(RuntimeError(ACCESS_DENIED)))

    def test_可重试的错误都能识别(self):
        for message in TRANSIENT:
            with self.subTest(message=message):
                self.assertTrue(is_retryable(RuntimeError(message)))

    def test_不可重试的错误都能识别(self):
        for message in PERMANENT:
            with self.subTest(message=message):
                self.assertFalse(is_retryable(RuntimeError(message)))

    def test_永久标记优先于状态码(self):
        """`Access denied ... 403` 里同时有 403，但必须判为不可重试 —— 永久标记要先判"""
        error = RuntimeError("Access denied")
        error.status_code = 403
        self.assertFalse(is_retryable(error))

    def test_状态码兜底(self):
        for status, expected in [(429, True), (503, True), (500, True),
                                 (401, False), (403, False), (404, False)]:
            with self.subTest(status=status):
                error = RuntimeError("no marker in message")
                error.status_code = status
                self.assertIs(is_retryable(error), expected)

    def test_认不出来的错误不重试(self):
        """宁可快速失败，也别在一个可能永久失败的问题上空转"""
        self.assertFalse(is_retryable(RuntimeError("something totally unexpected")))


class TestCallWithRetry(unittest.TestCase):

    def test_可重试错误会重试到成功(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError(THROTTLED)
            return "ok"

        self.assertEqual(call_with_retry(flaky, what="test", retries=3, backoff=0), "ok")
        self.assertEqual(len(attempts), 3)

    def test_不可重试错误立即抛出不做等待(self):
        attempts = []

        def permanent_failure():
            attempts.append(1)
            raise RuntimeError(QUOTA_EXHAUSTED)

        with self.assertRaises(RuntimeError):
            call_with_retry(permanent_failure, what="test", retries=3, backoff=0)

        self.assertEqual(len(attempts), 1, "不可重试的错误只应尝试一次")

    def test_重试次数用尽后抛出原异常(self):
        attempts = []

        def always_flaky():
            attempts.append(1)
            raise RuntimeError(THROTTLED)

        with self.assertRaises(RuntimeError):
            call_with_retry(always_flaky, what="test", retries=2, backoff=0)

        self.assertEqual(len(attempts), 3, "首次 + 2 次重试")

    def test_retries为零表示不重试(self):
        attempts = []

        def flaky():
            attempts.append(1)
            raise RuntimeError(THROTTLED)

        with self.assertRaises(RuntimeError):
            call_with_retry(flaky, what="test", retries=0, backoff=0)

        self.assertEqual(len(attempts), 1)


class TestRetryingEmbeddings(unittest.TestCase):
    """包装层要覆盖**建库路径**（embed_documents）—— 那条路径失败会留下半成品"""

    def _make(self, fail_times, error):
        from model.factory import RetryingEmbeddings

        class FakeInner:
            def __init__(self):
                self.calls = 0

            def embed_documents(self, texts, **kwargs):
                self.calls += 1
                if self.calls <= fail_times:
                    raise RuntimeError(error)
                return [[0.1] * 3 for _ in texts]

            def embed_query(self, text, **kwargs):
                self.calls += 1
                if self.calls <= fail_times:
                    raise RuntimeError(error)
                return [0.1, 0.2, 0.3]

        inner = FakeInner()
        return inner, RetryingEmbeddings(inner, retries=3, backoff=0)

    def test_限流后能重试成功(self):
        inner, wrapper = self._make(fail_times=2, error=THROTTLED)
        result = wrapper.embed_documents(["a", "b"])
        self.assertEqual(len(result), 2)
        self.assertEqual(inner.calls, 3)

    def test_额度用尽立即失败不重试(self):
        inner, wrapper = self._make(fail_times=99, error=QUOTA_EXHAUSTED)
        with self.assertRaises(RuntimeError):
            wrapper.embed_documents(["a"])
        self.assertEqual(inner.calls, 1, "额度用尽不该重试")

    def test_查询路径同样有重试(self):
        inner, wrapper = self._make(fail_times=1, error=THROTTLED)
        self.assertEqual(len(wrapper.embed_query("x")), 3)
        self.assertEqual(inner.calls, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
