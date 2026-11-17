"""Layer 4b UI walk tests: DOM cleanliness + session aggregation.
(The browser walk itself runs in the gauntlet layer under ~/.venvs/shot.)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import ui_walk as uw                          # noqa: E402


from dashboard import ui_walk_worker as worker               # noqa: E402


def test_dom_clean():
    assert worker.dom_clean("<html><body>queue: 3 tickets</body></html>")
    assert not worker.dom_clean(
        "<pre>Traceback (most recent call last):\n  File ...</pre>")
    assert not worker.dom_clean("NameError: name 'x' is not defined")


def test_run_walk_all_pass(monkeypatch):
    def fake(url, token, session, timeout=180.0):
        return {"session": session, "ok": True,
                "responses": {"/dashboard": 200}, "failures": []}
    monkeypatch.setattr(uw, "run_session", fake)
    out = uw.run_walk("http://127.0.0.1:1", "tok", workers=3)
    assert out["ok"]
    assert len(out["data"]["sessions"]) == 3


def test_run_walk_fails_on_bad_session(monkeypatch):
    def fake(url, token, session, timeout=180.0):
        return {"session": session, "ok": session != 1,
                "responses": {},
                "failures": [] if session != 1 else ["500 on /dashboard"]}
    monkeypatch.setattr(uw, "run_session", fake)
    out = uw.run_walk("http://127.0.0.1:1", "tok", workers=3)
    assert not out["ok"]
    assert any("500" in ln for ln in out["lines"])


def test_run_walk_parallel_sessions(monkeypatch):
    seen = []
    def fake(url, token, session, timeout=180.0):
        seen.append(session)
        return {"session": session, "ok": True, "responses": {},
                "failures": []}
    monkeypatch.setattr(uw, "run_session", fake)
    out = uw.run_walk("http://127.0.0.1:1", "tok", workers=4)
    assert out["ok"]
    assert sorted(seen) == [0, 1, 2, 3]


def test_worker_script_parses():
    import ast
    src = (os.path.join(os.path.dirname(uw.__file__),
                        "ui_walk_worker.py"))
    ast.parse(open(src).read())
