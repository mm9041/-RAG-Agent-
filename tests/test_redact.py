"""敏感信息脱敏的单元测试。

守的是「数据已经在日志/反馈里了」这一类问题：功能测试全绿、界面上一切正常，
但 ``logs/agent.log`` 和 ``feedback.sqlite`` 里躺着用户的手机号、地址、订单号。
这类问题不会让任何用例失败，只会在某天有人翻日志时才发现。

跑法：
    python -m unittest tests.test_redact -v
"""
import logging
import os
import tempfile
import unittest
from pathlib import Path

from utils.redact import redact


class PatternTests(unittest.TestCase):

    def test_手机号保留前三后四(self):
        self.assertEqual(redact("手机号 13812345678"), "手机号 138****5678")

    def test_身份证保留地区码与后四位(self):
        self.assertIn("110101********1234", redact("身份证 110101199003071234"))

    def test_银行卡只留后四位(self):
        masked = redact("卡号 6222021234567890")
        self.assertIn("**** **** **** 7890", masked)
        self.assertNotIn("622202", masked)

    def test_邮箱保留首字母与完整域名(self):
        self.assertEqual(redact("联系 zhangsan@example.com"), "联系 z***@example.com")

    def test_订单号保留前后各两位(self):
        self.assertIn("订单号：AB****78", redact("订单号：AB12345678"))

    def test_地址整体掩掉(self):
        masked = redact("收货地址：广东省深圳市南山区科技园路1号A栋502")
        self.assertIn("[地址已脱敏]", masked)
        self.assertNotIn("科技园路", masked)

    def test_口语句式里的地址也要盖住(self):
        for text in ("我家在深圳市南山区某小区3栋", "住在广州市天河区体育西路8号",
                     "寄到杭州市西湖区文三路100号"):
            self.assertNotIn("区", redact(text), text)


class NoFalsePositiveTests(unittest.TestCase):
    """误伤的代价比漏判更高：日志读不懂，排查就断了。"""

    def test_产品参数与型号不被误伤(self):
        for text in (
            "X1 OMNI 售价 4999 元，吸力 5000Pa，水箱 0.5L",
            "型号 T10 Pro，滤网每 2 周清洗一次",
            "参考月份 2026-10，用户ID 1001",
            "HEPA 滤网建议 3 个月更换一次",
        ):
            self.assertEqual(redact(text), text)

    def test_IP地址不是住址(self):
        self.assertEqual(redact("服务监听 IP地址：192.168.1.10"), "服务监听 IP地址：192.168.1.10")
        self.assertEqual(redact("MAC地址 aa:bb:cc:dd:ee:ff"), "MAC地址 aa:bb:cc:dd:ee:ff")


class IdempotentTests(unittest.TestCase):

    def test_重复脱敏不再改变(self):
        text = ("手机 13812345678 / 身份证 110101199003071234 / 卡号 6222021234567890 "
                "邮箱 zhangsan@example.com / 订单号 AB12345678 / "
                "收货地址：广东省深圳市南山区科技园路1号A栋502")
        once = redact(text)
        self.assertEqual(redact(once), once)
        for secret in ("13812345678", "110101199003071234", "6222021234567890",
                       "zhangsan", "AB12345678", "科技园路"):
            self.assertNotIn(secret, once)


class EdgeCaseTests(unittest.TestCase):

    def test_空值与非字符串原样返回(self):
        self.assertIsNone(redact(None))
        self.assertEqual(redact(""), "")
        self.assertEqual(redact(123), 123)
        self.assertEqual(redact({"phone": "13812345678"}), {"phone": "13812345678"})

    def test_普通中文不受影响(self):
        text = "扫地机器人回充失败时，请先检查充电座位置。"
        self.assertEqual(redact(text), text)


class LogRedactionTests(unittest.TestCase):
    """日志出口：验证改在 Formatter 上确实一处覆盖全部。"""

    def _logger(self, tmp, name):
        from utils.logger_handler import get_logger
        logger = get_logger(name=name, log_file=os.path.join(tmp, "agent.log"),
                            console_level=logging.CRITICAL)
        logger.propagate = False      # 别让记录冒泡到 root 污染测试输出
        return logger

    @staticmethod
    def _close(logger):
        # Windows 上 handler 不关会占着文件句柄，TemporaryDirectory 清理时报
        # WinError 32。关闭前会自动 flush。
        for handler in logger.handlers:
            handler.flush()
            handler.close()

    def test_落盘前自动脱敏(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = self._logger(tmp, "redact-log-basic")
            logger.info("用户手机号 13812345678，邮箱 zhangsan@example.com")
            logger.info("收货地址：广东省深圳市南山区科技园路1号A栋502")
            self._close(logger)
            content = Path(tmp, "agent.log").read_text(encoding="utf-8")

        self.assertNotIn("13812345678", content)
        self.assertIn("138****5678", content)
        self.assertNotIn("zhangsan", content)
        self.assertNotIn("科技园路", content)

    def test_异常栈里的敏感信息也不漏(self):
        """这是选 Formatter 而不是 Filter 的理由：traceback 不经过 record.msg。"""
        with tempfile.TemporaryDirectory() as tmp:
            logger = self._logger(tmp, "redact-log-exc")
            try:
                raise ValueError("下游拒绝了手机号 13812345678")
            except ValueError:
                logger.exception("调用失败")
            self._close(logger)
            content = Path(tmp, "agent.log").read_text(encoding="utf-8")

        self.assertIn("ValueError", content)
        self.assertNotIn("13812345678", content)
        self.assertIn("138****5678", content)


class FeedbackRedactionTests(unittest.TestCase):
    """反馈出口：入库前脱敏，但不影响界面展示的原文。"""

    def test_反馈入库前脱敏(self):
        from utils.customer_service import feedback_rows, save_feedback

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "feedback.sqlite")
            save_feedback(
                "1001", "user-1001", "answer-1",
                "我手机号 13812345678，邮箱 zhangsan@example.com，订单号 AB12345678",
                "已记录。收货地址：广东省深圳市南山区科技园路1号A栋502",
                "没帮助", "请回电 13900001111", [], "v1", db,
            )
            row = feedback_rows(db)[0]

        self.assertIn("138****5678", row["question"])
        self.assertNotIn("13812345678", row["question"])
        self.assertNotIn("zhangsan", row["question"])
        self.assertNotIn("AB12345678", row["question"])
        self.assertNotIn("科技园路", row["answer"])
        self.assertNotIn("13900001111", row["reason"])


if __name__ == "__main__":
    unittest.main()
