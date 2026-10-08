"""后台问答；工作线程不调用 Streamlit API。停止是协作式的，不强杀网络请求。"""
import queue
import threading
import time
from uuid import uuid4


class AnswerJob:
    def __init__(self, agent, query, thread_id, user_id, **device):
        self.query, self.thread_id, self.user_id = query, thread_id, user_id
        self.history = agent.load_history(thread_id, user_id)
        self.request_id = uuid4().hex
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.events = queue.Queue()
        self.error = None
        self.ended = None
        self.started = time.monotonic()
        self.timings = []
        def run():
            try:
                for event, data in agent.stream_events(query, thread_id, user_id,
                        request_id=self.request_id, cancel_event=self.cancel, **device):
                    self.events.put((event, data))
                    if event == "phase":
                        self.timings.append((data, round(time.monotonic()-self.started, 2)))
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                self.ended = time.monotonic()
                self.done.set()
        threading.Thread(target=run, daemon=True, name="robot-answer").start()
