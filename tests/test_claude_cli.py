"""The claude-cli provider: a response is used only if the model ran with no tools at all."""

import json
import os
import shutil
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_pilot  # noqa: E402

FAKE = """#!{python}
import json, os, sys
argv = sys.argv[1:]
flag = lambda name: argv[argv.index(name) + 1]
assert flag("--tools") == "" and "--strict-mcp-config" in argv and flag("--setting-sources") == ""
assert os.listdir(os.getcwd()) == []
prompt = sys.stdin.read()
events = {events}
for e in events:
    if e.get("type") == "result":
        e["result"] = flag("--system-prompt") + "|" + prompt
    print(json.dumps(e))
{extra}
"""

INIT = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "model": "fake-1"}
RESULT = {"type": "result", "is_error": False, "num_turns": 1, "stop_reason": "end_turn"}
TEXT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    def install(events, extra=""):
        path = tmp_path / "claude"
        path.write_text(FAKE.format(python=sys.executable, events=repr(events), extra=extra))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setattr(run_pilot, "CLAUDE_BIN", str(path))
        monkeypatch.setattr(run_pilot, "CLI_SANDBOX", "none")
    return install


def test_response_and_prompts_pass_through(fake_claude):
    fake_claude([INIT, TEXT, RESULT])
    r = run_pilot.call_model("claude-cli:haiku", "SYS", "USER\nline two")
    assert r["text"] == "SYS|USER\nline two"
    assert r["model_id"] == "fake-1" and r["truncated"] is False and r["temperature_used"] is None


@pytest.mark.parametrize("events, extra", [
    ([dict(INIT, tools=["Bash"]), TEXT, RESULT], ""),
    ([dict(INIT, mcp_servers=[{"name": "x"}]), TEXT, RESULT], ""),
    ([{k: v for k, v in INIT.items() if k != "tools"}, TEXT, RESULT], ""),
    ([INIT, {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}, RESULT], ""),
    ([INIT, TEXT, dict(RESULT, num_turns=2)], ""),
    ([INIT, TEXT, RESULT], "open('made_by_model.txt', 'w').close()"),
])
def test_anything_but_a_tool_less_single_turn_is_refused(fake_claude, events, extra):
    fake_claude(events, extra)
    with pytest.raises(run_pilot.IsolationError):
        run_pilot.call_model("claude-cli:haiku", "SYS", "USER")


def test_missing_sandbox_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(run_pilot, "CLI_SANDBOX", "bwrap")
    monkeypatch.setattr(run_pilot.shutil, "which", lambda name: None)
    with pytest.raises(run_pilot.IsolationError):
        run_pilot.sandbox_prefix(str(tmp_path))


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bwrap not installed")
def test_sandboxed_process_sees_nothing_of_the_host(tmp_path, monkeypatch):
    """A stand-in for the CLI reports what it can see from inside the sandbox."""
    fake = tmp_path / "claude"
    fake.write_text("""#!/bin/sh
seen=$(ls -A /home /home/* /home/*/.claude / 2>&1 | tr '\\n' ' ')
echo '{"type":"system","subtype":"init","tools":[],"mcp_servers":[],"model":"fake-1"}'
echo '{"type":"result","is_error":false,"num_turns":1,"stop_reason":"end_turn","result":"'"$seen"'"}'
""")
    fake.chmod(0o755)
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setattr(run_pilot, "CLAUDE_BIN", str(fake))
    monkeypatch.setattr(run_pilot, "CLI_CREDENTIALS", str(creds))
    monkeypatch.setattr(run_pilot, "CLI_SANDBOX", "bwrap")
    r = run_pilot.call_model("claude-cli:haiku", "SYS", "USER", retries=1)
    seen = set(r["text"].replace(":", " ").split())
    assert r["isolation"] == "no-tools+bwrap"
    assert os.path.basename(os.path.expanduser("~")) not in seen
    assert seen <= {"/home", "/home/subject", "/home/subject/.claude", "/", "subject", ".claude",
                    ".credentials.json", "bin", "dev", "etc", "home", "lib", "lib64", "opt", "proc",
                    "tmp", "usr", "work"}, seen


def test_a_failed_call_is_an_error_not_a_response(fake_claude, monkeypatch):
    monkeypatch.setattr(run_pilot.time, "sleep", lambda s: None)
    fake_claude([INIT, dict(RESULT, is_error=True)])
    with pytest.raises(RuntimeError):
        run_pilot.call_model("claude-cli:haiku", "SYS", "USER", retries=2)


def test_no_system_prompt_is_refused(fake_claude, monkeypatch):
    monkeypatch.setattr(run_pilot.time, "sleep", lambda s: None)
    fake_claude([INIT, TEXT, RESULT])
    with pytest.raises(RuntimeError):
        run_pilot.call_model("claude-cli:haiku", "", "USER", retries=1)
