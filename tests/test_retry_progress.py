import threading
import time
import unittest
from unittest.mock import Mock, patch

from utils.progress import answer_delta, current_progress_context, phase, progress_context
from utils.retry import RetryCancelled, RetryDeadlineExceeded, call_with_retry


class RetryProgressTests(unittest.TestCase):
    def test_actual_backoff_reports_safe_progress_and_resumes(self):
        events = []
        call = Mock(side_effect=[RuntimeError("rate limit: private-token"), "ok"])
        with progress_context(on_event=lambda event, data: events.append((event, data))):
            with patch("utils.retry.time.sleep") as sleep:
                self.assertEqual(call_with_retry(call, what="private-query", retries=1, backoff=2), "ok")
        sleep.assert_called_once_with(2)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(events[0][0], "phase")
        self.assertIn("2 秒后自动重试", events[0][1])
        self.assertEqual(events[1], ("phase", "正在重新连接服务"))
        self.assertFalse(any("private" in value for _, value in events))

    def test_cancelling_backoff_prevents_next_request(self):
        cancel = threading.Event()
        waiting = threading.Event()
        result = []
        call = Mock(side_effect=RuntimeError("Service unavailable"))

        def run():
            with progress_context(cancel_event=cancel, on_event=lambda *args: waiting.set()):
                try:
                    call_with_retry(call, what="test", retries=2, backoff=10)
                except RetryCancelled as error:
                    result.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(cancel.set)
        self.assertTrue(waiting.wait(2))
        started = time.monotonic()
        cancel.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(len(result), 1)

    def test_expired_budget_never_starts_a_request(self):
        call = Mock()
        with progress_context(deadline=time.monotonic() - 1):
            with self.assertRaises(RetryDeadlineExceeded):
                call_with_retry(call, what="test")
        call.assert_not_called()

    def test_deadline_limits_backoff_and_prevents_next_request(self):
        call = Mock(side_effect=RuntimeError("Service unavailable"))
        started = time.monotonic()
        with progress_context(cancel_event=threading.Event(), deadline=started + 0.04):
            with self.assertRaises(RetryDeadlineExceeded):
                call_with_retry(call, what="test", retries=3, backoff=10)
        self.assertEqual(call.call_count, 1)
        self.assertLess(time.monotonic() - started, 1)

    def test_permanent_error_does_not_wait_or_report_retry(self):
        events = []
        call = Mock(side_effect=RuntimeError("invalid api key"))
        with progress_context(on_event=lambda *args: events.append(args)):
            with patch("utils.retry.time.sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "invalid api key"):
                    call_with_retry(call, what="test", backoff=10)
        sleep.assert_not_called()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(events, [])

    def test_nested_context_keeps_callback_and_earliest_budget(self):
        events = []
        cancel = threading.Event()
        deadline = time.monotonic() + 10
        with progress_context(cancel_event=cancel, deadline=deadline,
                              on_event=lambda *args: events.append(args)) as outer:
            with progress_context(deadline=deadline + 20) as inner:
                self.assertIs(inner.cancel_event, cancel)
                self.assertEqual(inner.deadline, deadline)
                phase("进度")
                answer_delta("正文")
            self.assertIs(current_progress_context(), outer)
        self.assertIsNone(current_progress_context())
        self.assertEqual(events, [("phase", "进度"), ("content", "正文")])

    def test_graph_writer_and_job_callback_do_not_duplicate_events(self):
        callback, writer = Mock(), Mock()
        with progress_context(on_event=callback), patch("langgraph.config.get_stream_writer", return_value=writer):
            phase("检索")
            answer_delta("正文")
        callback.assert_not_called()
        self.assertEqual(writer.call_args_list[0].args[0], {"phase": "检索"})
        self.assertEqual(writer.call_args_list[1].args[0], {"answer_delta": "正文"})


if __name__ == "__main__":
    unittest.main()
