"""角色模型分离：未配置时复用主模型单例，配置后按名字构造并缓存。

这条逻辑很容易被后来的改动破坏而不自知——比如把"未配置时也构造新实例"
写回去，功能上看不出差别，但会多一个客户端、并且让测试里的
patch('...get_chat_model') 全部失效。
"""
import unittest
from unittest.mock import Mock, patch

from utils.config_handler import model_conf

KEYS = ("decision_model_name", "grounding_model_name")


class RoleModelTests(unittest.TestCase):
    def setUp(self):
        self._saved = {key: model_conf.get(key) for key in KEYS}
        for key in KEYS:
            model_conf[key] = ""
        from model.factory import reset_models

        reset_models()

    def tearDown(self):
        for key, value in self._saved.items():
            model_conf[key] = value
        from model.factory import reset_models

        reset_models()

    def test_unconfigured_role_reuses_chat_model(self):
        from model.factory import get_chat_model, get_decision_model, get_grounding_model

        with patch("model.factory.ChatModelFactory.generator", return_value=Mock()) as build:
            self.assertIs(get_decision_model(), get_chat_model())
            self.assertIs(get_grounding_model(), get_chat_model())
        self.assertEqual(build.call_count, 1, "未配置角色时不应额外构造客户端")

    def test_configured_role_builds_named_model_and_caches(self):
        from model.factory import get_grounding_model

        model_conf["grounding_model_name"] = "fast-model"
        with patch("model.factory._build_chat_model", return_value=Mock()) as build:
            first = get_grounding_model()
            second = get_grounding_model()
            self.assertIs(first, second, "同名配置应复用同一个客户端")
            build.assert_called_once_with("fast-model")

            model_conf["grounding_model_name"] = "another-model"
            get_grounding_model()
            self.assertEqual(build.call_count, 2, "配置改了要重建")
