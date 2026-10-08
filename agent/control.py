import threading
from uuid import uuid4

_lock = threading.Lock()
_events = {}

def register(event):
    token = uuid4().hex
    with _lock:
        _events[token] = event
    return token

def cancelled(token):
    with _lock:
        event = _events.get(token)
    return event is not None and event.is_set()

def unregister(token):
    with _lock:
        _events.pop(token, None)
