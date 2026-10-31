"""测试公共设置：清空角色模型配置。

绝大多数测试用 ``patch('...get_chat_model')`` 注入假模型。如果生产配置
（``config/model.yml``）把 decision/grounding 指向了别的模型名，代码会绕过
这个 patch 去构造真实客户端——测试要么因缺 API key 失败，要么真发网络请求。

需要验证角色模型本身的用例，请在自己的 setUp 里显式设置这两个键并在
tearDown 还原。
"""
from utils.config_handler import model_conf

model_conf["decision_model_name"] = ""
model_conf["grounding_model_name"] = ""
