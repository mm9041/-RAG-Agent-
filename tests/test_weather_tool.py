"""get_weather / get_user_location 的单元测试。

守两件事：

1. ``get_user_location`` 不能"假装知道"城市 —— 系统没有定位能力，它只能把
   问题交回给用户，而不是返回一个写死的深圳。
2. ``get_weather`` 走 Tavily 实时搜索，任何失败都必须降级成一句可读的提示：
   不能抛异常打断 agent 循环，也不能让模型拿"查询失败"去编造天气。

跑法：
    python -m unittest discover -s tests -p "test_weather_tool.py" -v
"""
import unittest
from unittest.mock import patch

from agent.tools.agent_tools import (LOCATION_NEEDED_MARKER, get_user_location,
                                     get_weather)


class FakeTavily:
    """只实现 search 的最小替身，顺带记下调用参数。"""

    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class WeatherToolTests(unittest.TestCase):

    def test_成功时带来源与时滞提示(self):
        fake = FakeTavily({
            "results": [{"content": "深圳今天多云，26~31℃，湿度 78%。最后更新：18 Oct 2026",
                         "url": "https://weather.example/sz"}],
        })
        with patch("agent.tools.agent_tools._get_tavily_client", return_value=fake):
            text = get_weather.invoke({"city": "深圳"})

        self.assertIn("联网", text)
        self.assertIn("可能滞后", text)
        self.assertIn("深圳", text)
        self.assertIn("26~31", text)
        self.assertIn("https://weather.example/sz", text)
        self.assertIn("深圳", fake.calls[0]["query"])

    def test_查询参数走配置(self):
        fake = FakeTavily({"results": [{"content": "晴"}]})
        with patch("agent.tools.agent_tools._get_tavily_client", return_value=fake):
            get_weather.invoke({"city": "杭州市"})

        call = fake.calls[0]
        self.assertEqual(call["search_depth"], "basic")
        self.assertFalse(call["include_answer"],
                         "Tavily 的 answer 会把旧页面复述成今天，必须关掉")
        self.assertLessEqual(call["timeout"], 60, "单次搜索不能拖住整轮问答")

    def test_未配置key时明确说不可用(self):
        with patch("agent.tools.agent_tools._get_tavily_client", return_value=None):
            text = get_weather.invoke({"city": "深圳"})
        self.assertIn("不可用", text)
        self.assertIn("不要编造", text)

    def test_搜索异常不抛出(self):
        fake = FakeTavily(error=TimeoutError("boom"))
        with patch("agent.tools.agent_tools._get_tavily_client", return_value=fake):
            text = get_weather.invoke({"city": "深圳"})
        self.assertIn("失败", text)
        self.assertIn("不要编造", text)

    def test_没有结果时不猜(self):
        with patch("agent.tools.agent_tools._get_tavily_client",
                   return_value=FakeTavily({})):
            text = get_weather.invoke({"city": "深圳"})
        self.assertIn("没有查到", text)

    def test_代词与提示语不能当城市名(self):
        fake = FakeTavily({"results": [{"content": "不该被调用"}]})
        with patch("agent.tools.agent_tools._get_tavily_client", return_value=fake):
            for bad in ("这里", "深圳（模拟位置）", "", "我", LOCATION_NEEDED_MARKER):
                with self.subTest(city=bad):
                    text = get_weather.invoke({"city": bad})
                    self.assertIn("城市名无效", text)
        self.assertEqual(fake.calls, [], "非法城市名不应发出网络请求")


class LocationToolTests(unittest.TestCase):

    def test_不再假装知道用户位置(self):
        text = get_user_location.invoke({})
        self.assertIn(LOCATION_NEEDED_MARKER, text)
        self.assertIn("询问", text)
        self.assertNotIn("深圳", text)


if __name__ == "__main__":
    unittest.main()
