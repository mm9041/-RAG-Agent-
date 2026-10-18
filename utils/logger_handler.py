import logging
import os
from logging.handlers import TimedRotatingFileHandler

from utils.path_tool import get_abs_path
from utils.redact import redact

# 日志保存的根目录
LOG_ROOT = get_abs_path("logs")

# 确保日志的目录存在
os.makedirs(LOG_ROOT, exist_ok=True)

# 日志文件名与保留天数
LOG_FILE_NAME = "agent.log"
LOG_BACKUP_DAYS = 7

class RedactingFormatter(logging.Formatter):
    """在格式化结果上统一脱敏，一处覆盖所有日志出口。

    挂在 Formatter 而不是 Filter 上，是刻意的选择：
    Filter 只能改写 ``record.msg`` / ``record.args``，而异常栈是 ``format()``
    阶段才拼进去的 —— ``logger.exception`` 的 traceback 里如果带了手机号、
    地址，Filter 完全拦不住。改在 ``format()`` 的最终字符串上，msg、args、
    stack_info、exc_info 就都在覆盖范围内了。

    另外 Formatter 是挂在 handler 上的，各模块无论用哪个 logger 名字，
    只要日志最终落到这两个 handler，就一定会经过这里。
    """

    def format(self, record):
        return redact(super().format(record))


# 日志的格式配置  error info debug
DEFAULT_LOG_FORMAT = RedactingFormatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
)


def get_logger(
        name: str = "agent",
        console_level: int = logging.INFO,
        file_level: int = logging.DEBUG,
        log_file=None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 避免重复添加Handler
    if logger.handlers:
        return logger

    # 控制台Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(DEFAULT_LOG_FORMAT)

    logger.addHandler(console_handler)

    # 文件Handler（按天轮转）
    if not log_file:
        log_file = os.path.join(LOG_ROOT, LOG_FILE_NAME)

    # 用 TimedRotatingFileHandler 而不是 FileHandler，一次解决两个问题：
    #   1. 磁盘不再无限增长 —— 自动只保留最近 LOG_BACKUP_DAYS 天；
    #   2. 跨零点自动换文件 —— 旧版把日期写死在文件名里（agent_20260919.log），
    #      而那是 import 时算出来的，进程跨零点后仍会一直写前一天的文件。
    # 轮转后的文件名为 agent.log.2026-09-19（suffix 默认即 %Y-%m-%d）。
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=LOG_BACKUP_DAYS,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(DEFAULT_LOG_FORMAT)

    logger.addHandler(file_handler)

    return logger


# 快捷获取日志器
logger = get_logger()


if __name__ == '__main__':
    logger.info("信息日志")
    logger.error("错误日志")
    logger.warning("警告日志")
    logger.debug("调试日志")

    for handler in logger.handlers:
        if isinstance(handler, TimedRotatingFileHandler):
            print(f"轮转配置: {handler.baseFilename} | 每 {handler.interval} 天 | "
                  f"保留 {handler.backupCount} 份")
