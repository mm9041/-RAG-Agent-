"""把 ``server`` 命名空间的日志接到业务层已有的 handler 上。

两个理由，都不是"好看"层面的：

1. **不接就看不到日志**。root logger 默认 level=WARNING 且没有 handler，
   ``logging.getLogger(__name__)`` 拿到的 logger 打 info 是静默丢弃的。
   表现就是：启动完成了终端没有任何"就绪"提示，初始化失败也只有前端能看见。

2. **脱敏**。``utils/logger_handler.py`` 的 handler 挂的是 ``RedactingFormatter``，
   会拦下手机号/地址/订单号，包括 ``logger.exception`` 的 traceback。
   SSE 层记的异常里可能带用户问题原文，走裸 logging 就漏了。

为什么是"复用 handler 对象"而不是 ``get_logger("server")``：
后者会再创建一个指向 ``logs/agent.log`` 的 TimedRotatingFileHandler。
两个 handler 同时打开同一个文件，在 Windows 上跨零点轮转时互相占用，
重命名会失败。直接共享同一批 handler 对象没有这个问题。
"""
from __future__ import annotations

import logging

from utils.logger_handler import logger as _business_logger


def setup_server_logging() -> logging.Logger:
    logger = logging.getLogger("server")
    logger.setLevel(logging.INFO)
    # 不再向 root 传播：uvicorn 自己往 stderr 写，重复输出没有意义
    logger.propagate = False
    if not logger.handlers:
        for handler in _business_logger.handlers:
            logger.addHandler(handler)
    return logger
