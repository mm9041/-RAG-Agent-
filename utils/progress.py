def phase(label):
    try:
        from langgraph.config import get_stream_writer
        get_stream_writer()({"phase": label})
    except RuntimeError:
        pass


def answer_delta(text):
    if not text:
        return
    try:
        from langgraph.config import get_stream_writer
        get_stream_writer()({"answer_delta": text})
    except RuntimeError:
        pass
