from __future__ import annotations

from lgtvcompanion.agent.streams import ProcessStreamWatcher


def test_process_watcher_fires_on_match():
    events = []
    w = ProcessStreamWatcher(["parsec*", "chrome-remote-desktop*"], events.append)
    w.poll_once({"bash", "firefox"})
    assert events == []                       # nothing matched
    w.poll_once({"bash", "parsecd"})
    assert events == [True]                    # parsecd matches parsec*
    w.poll_once({"bash", "parsecd"})
    assert events == [True]                     # still active, no re-fire
    w.poll_once({"bash"})
    assert events == [True, False]             # gone


def test_process_watcher_case_insensitive():
    events = []
    w = ProcessStreamWatcher(["Parsec*"], events.append)
    w.poll_once({"parsecd"})
    assert events == [True]


def test_or_combination_in_agent(tmp_path):
    # exercise the agent's OR logic without Qt/dbus: drive _set_stream_source
    from lgtvcompanion.agent.main import Agent
    a = Agent(str(tmp_path / "sock"))
    sent = []
    # capture what would be queued
    a._send_queue.put_nowait = lambda item: sent.append(item)  # type: ignore
    a._set_stream_source("sunshine", True)
    assert sent == [{"streaming": True}]
    a._set_stream_source("process", True)     # already streaming → no new report
    assert sent == [{"streaming": True}]
    a._set_stream_source("sunshine", False)   # process still active → stays on
    assert sent == [{"streaming": True}]
    a._set_stream_source("process", False)    # all clear → off
    assert sent == [{"streaming": True}, {"streaming": False}]
