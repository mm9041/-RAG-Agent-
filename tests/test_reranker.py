"""
重排的超时透传与健康状态（对应 A2 + dashscope 依赖缺失）

为什么这条测试**必须不联网**却又要真的去构造请求对象：
`request_timeout` 不是 `TextReRank.call` 的文档化参数 —— 它是靠
    kwargs → parameters(splat) → BaseApi.call(**kwargs)
    → _build_api_request(request_timeout=…)   ← 具名参数，被传输层消费
    → HttpRequest.timeout
这条**未文档化**的 plumbing 生效的。上游一旦把那个具名参数改名或改成收进 body，
我们的超时会**静默失效**（回到 SDK 默认的 300 秒），而表现只是"偶尔卡很久"。
所以这里直接断言"配置值 == 构造出的请求对象的 timeout"，让它坏的时候变红。

全程不发网络请求、不需要真实 API key（api_key 传的是假串，只用于对象构造）。
"""
import unittest

from utils.config_handler import chroma_conf


class TestRerankTimeoutPlumbing(unittest.TestCase):
    def setUp(self):
        self._original = chroma_conf.get("rerank_timeout")

    def tearDown(self):
        if self._original is None:
            chroma_conf.pop("rerank_timeout", None)
        else:
            chroma_conf["rerank_timeout"] = self._original

    def _build(self, **extra):
        """离线构造一次重排请求对象（等价于 Reranker.rerank 里的那一次调用）"""
        from dashscope.api_entities.api_request_factory import _build_api_request
        from dashscope.rerank.text_rerank import _build_rerank_request
        from rag.reranker import get_reranker

        reranker = get_reranker()
        task_group, fn, inp, parameters = _build_rerank_request(
            model=reranker.model, query="问题", documents=["资料一", "资料二"],
            top_n=3, request_timeout=reranker.timeout, **extra)
        return _build_api_request(model=reranker.model, task_group=task_group,
                                  task="text-rerank", function=fn,
                                  api_key="sk-这是测试用的假Key",
                                  input=inp, **parameters)

    def test_配置的超时会透到传输层对象(self):
        """★ 核心：配置数字必须真的变成 HttpRequest.timeout"""
        chroma_conf["rerank_timeout"] = 17
        request = self._build()
        self.assertEqual(17, request.timeout,
                         "重排超时没有透到传输层 —— SDK 升级改动了参数链路，"
                         "此时会退回默认的 300 秒，界面会长时间干等")

    def test_换个配置值确实换个结果防写死(self):
        chroma_conf["rerank_timeout"] = 33
        self.assertEqual(33, self._build().timeout)

    def test_超时不会混进请求体(self):
        """它是传输层参数，不是发给服务的字段 —— 混进去会被服务端判为非法参数。

        `data.parameters` 就是要发出去的 body 参数集合，实测只有 {'top_n': 3}。
        """
        chroma_conf["rerank_timeout"] = 21
        request = self._build()
        parameters = request.data.parameters
        self.assertNotIn("request_timeout", parameters)
        self.assertEqual({"top_n": 3}, parameters)

    def test_配置缺项时回落到一个正数而不是抛异常(self):
        from rag.reranker import get_reranker
        chroma_conf.pop("rerank_timeout", None)
        self.assertGreater(get_reranker().timeout, 0)


class TestRerankHealth(unittest.TestCase):
    """侧栏那一行"重排健康状态" —— 存在的理由是防**静默降级**"""

    def setUp(self):
        self._enabled = chroma_conf.get("rerank_enabled")

    def tearDown(self):
        chroma_conf["rerank_enabled"] = self._enabled

    def test_已启用且依赖就绪时报出模型与超时(self):
        from rag.reranker import rerank_health
        chroma_conf["rerank_enabled"] = True
        health = rerank_health()
        self.assertNotIn("未安装", health)
        self.assertIn("降级", health)      # 明确说明失败会降级

    def test_未启用时直说不启用(self):
        from rag.reranker import rerank_health
        chroma_conf["rerank_enabled"] = False
        self.assertIn("未启用", rerank_health())

    def test_依赖缺失必须肉眼可见地告警(self):
        """★ 回归：dashscope 曾不在 requirements.txt 里，而 ImportError 会被
        "重排失败降级"吞掉 → 干净环境静默失去重排（块级 hit@3 12/12 → 58%）。"""
        from rag import reranker as module

        original = module.importlib.util.find_spec
        module.importlib.util.find_spec = (
            lambda name: None if name == "dashscope" else original(name))
        try:
            health = module.rerank_health()
        finally:
            module.importlib.util.find_spec = original

        self.assertIn("⚠️", health)
        self.assertIn("静默降级", health)


if __name__ == "__main__":
    unittest.main()
