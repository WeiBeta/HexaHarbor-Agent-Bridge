import json
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from agent_bridge.adapters.antigravity import (
    AgyAdapter,
    _scoped_usage,
    collect_tool_paths,
    conversation_id_of,
    discovered_agy_commands,
    find_agy_project,
    is_agy_tool_schema_error,
    is_result_event,
    permission_mode_of,
    recovered_agy_tool_error,
    result_error_of,
    result_text_of,
    text_delta_of,
)
from agent_bridge.config import AgentConfig
from agent_bridge.models import Session, Task, agy_effort

FAKE_AGY = Path(__file__).resolve().parent / "fake_agy.py"

INIT = {
    "event": "init",
    "conversation_id": "c3b66b04-872b-4fbe-a3a4-058a026ef20a",
    "init": {"cwd": "/home/user/project", "tools": ["write_to_file"], "permission_mode": "always-proceed"},
}
STEP_TEXT = {
    "event": "step_update",
    "step_update": {
        "conversation_id": "c3b66b04-872b-4fbe-a3a4-058a026ef20a",
        "step_index": 3,
        "state": "DONE",
        "step_type": "agent_response",
        "text_delta": "Git rebase rewrites history.\n",
    },
}
RESULT = {
    "event": "result",
    "result": {
        "conversation_id": "c3b66b04-872b-4fbe-a3a4-058a026ef20a",
        "status": "SUCCESS",
        "response": "Git rebase rewrites history.\n",
        "usage": {"total_tokens": 11007},
    },
}
ERROR = {
    "event": "result",
    "result": {"conversation_id": "", "status": "ERROR", "response": "", "error": "authentication required"},
}


def test_init_is_not_a_result():
    assert conversation_id_of(INIT) == "c3b66b04-872b-4fbe-a3a4-058a026ef20a"
    assert is_result_event(INIT) is False
    assert result_text_of(INIT) == ""


def test_text_delta_and_result_envelope():
    assert text_delta_of(STEP_TEXT) == "Git rebase rewrites history.\n"
    assert is_result_event(RESULT) is True
    assert result_text_of(RESULT) == "Git rebase rewrites history.\n"
    assert result_error_of(RESULT) is None


def test_error_result():
    assert is_result_event(ERROR) is True
    assert result_error_of(ERROR) == "authentication required"


CODECONTENT_ERROR = (
    "declaring permissions: cortex tool write_to_file: convert tool call for "
    "permissions: model output error: invalid tool call error (invalid_signature) "
    "CodeContent is a required parameter. Please follow the function call schema exactly."
)


def test_tool_schema_error_is_not_a_hard_failure_when_the_turn_recovered():
    assert is_agy_tool_schema_error(CODECONTENT_ERROR) is True
    assert is_agy_tool_schema_error("authentication required") is False
    recovered = {
        "event": "result",
        "result": {
            "status": "ERROR",
            "response": "Implemented scoreboard and 26 tests passed.\n",
            "error": CODECONTENT_ERROR,
        },
    }
    assert recovered_agy_tool_error(recovered, recovered["result"]["response"], 0) is True
    assert recovered_agy_tool_error(recovered, "", 0) is False
    assert recovered_agy_tool_error(ERROR, "authentication required", 1) is False


def test_collect_tool_paths():
    files: set[str] = set()
    collect_tool_paths(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_info": {"name": "write_to_file", "parameters": {"Path": "/tmp/a.txt"}},
            },
        },
        files,
    )
    assert files == {"/tmp/a.txt"}


def test_collect_tool_paths_skips_read_only_tools():
    files: set[str] = set()
    for name, key in (("view_file", "AbsolutePath"), ("list_dir", "Path"), ("grep_search", "Path")):
        collect_tool_paths(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": name,
                    "tool_info": {"name": name, "parameters": {key: "/tmp/read-only.txt"}},
                },
            },
            files,
        )
    assert files == set()
    collect_tool_paths(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_info": {"name": "replace_file_content", "parameters": {"TargetFile": "/tmp/b.txt"}},
            },
        },
        files,
    )
    assert files == {"/tmp/b.txt"}


def test_agy_model_and_effort_precede_print(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: ["agy"],
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: projects)
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(
        session_id="sess_m",
        agent="antigravity",
        cwd=str(tmp_path),
        model="gemini-3.7-flash",
        effort="low",
    )
    task = Task(
        task_id="task_m",
        session_id=session.session_id,
        agent="antigravity",
        message="do it",
        cwd=str(tmp_path),
        model="gemini-3.7-flash",
        effort="low",
    )
    cmd = adapter._build_cmd(session, task)
    assert cmd.index("--model") < cmd.index("-p")
    assert cmd.index("--effort") < cmd.index("-p")
    assert cmd.index("--mode") < cmd.index("-p")
    assert cmd[cmd.index("--mode") + 1] == "auto"
    assert cmd.index("--add-dir") < cmd.index("-p")
    assert cmd.index("--new-project") < cmd.index("-p")
    assert cmd[cmd.index("--model") + 1] == "gemini-3.7-flash"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
    assert "--conversation" not in cmd
    assert "--sandbox" not in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "--input-format" in cmd
    assert cmd[cmd.index("-p") + 1] == ""
    assert task.message not in cmd


def test_agy_sandbox_and_skip_permissions_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: ["agy"],
    )
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: tmp_path / "projects")
    adapter = AgyAdapter(
        AgentConfig(
            name="antigravity",
            protocol="agy",
            command=["agy"],
            agy_mode="",
            agy_sandbox=True,
            skip_permissions=True,
        ),
        tmp_path,
    )
    session = Session(session_id="sess_s", agent="antigravity", cwd=str(tmp_path))
    task = Task(task_id="task_s", session_id=session.session_id, agent="antigravity", message="go", cwd=str(tmp_path))
    cmd = adapter._build_cmd(session, task)
    assert "--mode" not in cmd
    assert cmd.index("--sandbox") < cmd.index("-p")
    assert "--dangerously-skip-permissions" in cmd


def test_agy_effort_preserves_xhigh_and_max():
    assert agy_effort("off") == "low"
    assert agy_effort("low") == "low"
    assert agy_effort("medium") == "medium"
    assert agy_effort("high") == "high"
    assert agy_effort("xhigh") == "xhigh"
    assert agy_effort("max") == "max"


def test_permission_mode_of_reads_init_block():
    assert permission_mode_of({"event": "init", "init": {"permission_mode": "always-proceed"}}) == "always-proceed"
    assert permission_mode_of({"event": "init", "init": {}}) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows LOCALAPPDATA fallback")
def test_discovered_agy_commands_finds_localappdata_exe(tmp_path):
    exe = tmp_path / "agy" / "bin" / "agy.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    found = discovered_agy_commands({"LOCALAPPDATA": str(tmp_path)})
    assert found == [[str(exe.resolve())]]


def test_agy_follow_up_uses_conversation_not_new_project(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: ["agy"],
    )
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.agy_projects_dir",
        lambda: tmp_path / "projects",
    )
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(
        session_id="sess_c",
        agent="antigravity",
        cwd=str(tmp_path),
        native_session_id="conv-1",
    )
    task = Task(
        task_id="task_c",
        session_id=session.session_id,
        agent="antigravity",
        message="again",
        cwd=str(tmp_path),
    )
    cmd = adapter._build_cmd(session, task)
    assert cmd.index("--conversation") < cmd.index("-p")
    assert cmd[cmd.index("--conversation") + 1] == "conv-1"
    assert "--new-project" not in cmd
    assert "--project" not in cmd
    assert "--input-format" in cmd
    assert cmd[cmd.index("-p") + 1] == ""
    assert task.message not in cmd


def test_find_agy_project_matches_cwd_folder_uri(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (projects / "z-nonmatch.json").write_text(
        json.dumps(
            {
                "id": "other-id",
                "projectResources": {"resources": [{"folderUri": "file:///somewhere/else"}]},
            }
        ),
        encoding="utf-8",
    )
    (projects / "corrupt.json").write_text("{not json", encoding="utf-8")
    (projects / "m-b.json").write_text(
        json.dumps(
            {
                "id": "b-id",
                "projectResources": {"resources": [{"folderUri": "file://" + cwd.as_posix()}]},
            }
        ),
        encoding="utf-8",
    )
    (projects / "a-a.json").write_text(
        json.dumps(
            {
                "id": "a-id",
                "projectResources": {
                    "resources": [
                        {"gitFolder": {"folderUri": "file:///" + quote(str(cwd), safe=""), "defaultBranch": "main"}}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    assert find_agy_project(str(cwd), projects) == "a-id"
    assert find_agy_project(str(tmp_path / "elsewhere"), projects) is None
    assert find_agy_project(str(cwd), tmp_path / "missing") is None


def test_agy_new_session_reuses_existing_project(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: ["agy"],
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (projects / "p.json").write_text(
        json.dumps(
            {
                "id": "proj-1",
                "projectResources": {"resources": [{"folderUri": "file://" + cwd.as_posix()}]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: projects)
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(session_id="sess_p", agent="antigravity", cwd=str(cwd))
    task = Task(
        task_id="task_p",
        session_id=session.session_id,
        agent="antigravity",
        message="do it",
        cwd=str(cwd),
    )
    cmd = adapter._build_cmd(session, task)
    assert cmd.index("--project") < cmd.index("-p")
    assert cmd[cmd.index("--project") + 1] == "proj-1"
    assert "--new-project" not in cmd
    assert "--conversation" not in cmd


@pytest.mark.skipif(sys.platform != "win32", reason="drive-letter URI normalization is Windows-specific")
def test_find_agy_project_windows_drive_uris(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "p.json").write_text(
        json.dumps(
            {
                "id": "plain-id",
                "projectResources": {"resources": [{"folderUri": "file://E:/inkcraft-next"}]},
            }
        ),
        encoding="utf-8",
    )
    encoded = tmp_path / "encoded"
    encoded.mkdir()
    (encoded / "p.json").write_text(
        json.dumps(
            {
                "id": "encoded-id",
                "projectResources": {
                    "resources": [
                        {
                            "gitFolder": {
                                "folderUri": "file:///e%3A%5Cinkcraft-next",
                                "defaultBranch": "main",
                            }
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    assert find_agy_project("E:\\inkcraft-next", plain) == "plain-id"
    assert find_agy_project("E:\\inkcraft-next", encoded) == "encoded-id"


def _agy_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgyAdapter:
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: [sys.executable, str(FAKE_AGY)],
    )
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.agy_projects_dir",
        lambda: tmp_path / "projects",
    )
    return AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )


@pytest.mark.asyncio
async def test_run_turn_sends_prompt_over_stdin(tmp_path, monkeypatch):
    adapter = _agy_adapter(tmp_path, monkeypatch)
    report = tmp_path / "len.txt"
    monkeypatch.setenv("FAKE_AGY_REPORT", str(report))
    session = Session(session_id="sess_stdin", agent="antigravity", cwd=str(tmp_path))
    task = Task(
        task_id="task_stdin",
        session_id=session.session_id,
        agent="antigravity",
        message="x" * 50000,
        cwd=str(tmp_path),
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"
    assert result.text.startswith("echo:")
    assert report.read_text(encoding="utf-8") == "50000"
    assert session.native_session_id == "conv-fake-agy"
    assert result.usage["scope"] == "turn"


@pytest.mark.asyncio
async def test_run_turn_labels_resumed_usage_as_conversation(tmp_path, monkeypatch):
    adapter = _agy_adapter(tmp_path, monkeypatch)
    session = Session(
        session_id="sess_resume",
        agent="antigravity",
        cwd=str(tmp_path),
        native_session_id="conv-1",
    )
    task = Task(
        task_id="task_resume",
        session_id=session.session_id,
        agent="antigravity",
        message="again",
        cwd=str(tmp_path),
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"
    assert result.usage["scope"] == "conversation"


def test_scoped_usage_leaves_empty_dict_alone():
    assert _scoped_usage({}, True) == {}


@pytest.mark.asyncio
async def test_run_turn_captures_observed_permission_mode(tmp_path, monkeypatch):
    script = tmp_path / "emit_pm.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'event':'init','conversation_id':'c1','init':{'permission_mode':'always-proceed'}}))\n"
        "print(json.dumps({'event':'result','result':{'conversation_id':'c1','status':'SUCCESS','response':'done'}}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: [sys.executable, str(script)],
    )
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.agy_projects_dir",
        lambda: tmp_path / "projects",
    )
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(session_id="sess_pm", agent="antigravity", cwd=str(tmp_path))
    task = Task(task_id="task_pm", session_id=session.session_id, agent="antigravity", message="hi", cwd=str(tmp_path))
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"
    assert result.observed_permission_mode == "always-proceed"


@pytest.mark.asyncio
async def test_run_turn_early_exit_reports_stderr(tmp_path, monkeypatch):
    adapter = _agy_adapter(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_AGY_MODE", "early_exit")
    session = Session(session_id="sess_early", agent="antigravity", cwd=str(tmp_path))
    task = Task(
        task_id="task_early",
        session_id=session.session_id,
        agent="antigravity",
        message="hello",
        cwd=str(tmp_path),
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error"
    assert result.error is not None
    assert "agy exit 3" in result.error
    assert "fake agy refused to start" in result.error


def test_agy_build_cmd_distinguishes_xhigh_max_off_and_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: ["agy"],
    )
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: tmp_path / "projects")
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )

    session_xhigh = Session(session_id="sess_x", agent="antigravity", cwd=str(tmp_path), effort="xhigh")
    task_followup = Task(task_id="task_f", session_id="sess_x", agent="antigravity", message="m", cwd=str(tmp_path))
    cmd_xhigh = adapter._build_cmd(session_xhigh, task_followup)
    assert cmd_xhigh.index("--effort") < cmd_xhigh.index("-p")
    assert cmd_xhigh[cmd_xhigh.index("--effort") + 1] == "xhigh"

    task_max = Task(
        task_id="task_m",
        session_id="sess_x",
        agent="antigravity",
        message="m",
        cwd=str(tmp_path),
        effort="max",
    )
    cmd_max = adapter._build_cmd(session_xhigh, task_max)
    assert cmd_max[cmd_max.index("--effort") + 1] == "max"

    task_off = Task(
        task_id="task_o",
        session_id="sess_x",
        agent="antigravity",
        message="m",
        cwd=str(tmp_path),
        effort="off",
    )
    cmd_off = adapter._build_cmd(Session(session_id="sess_o", agent="antigravity", cwd=str(tmp_path)), task_off)
    assert cmd_off[cmd_off.index("--effort") + 1] == "low"

    task_none = Task(task_id="task_n", session_id="sess_n", agent="antigravity", message="m", cwd=str(tmp_path))
    cmd_none = adapter._build_cmd(Session(session_id="sess_n", agent="antigravity", cwd=str(tmp_path)), task_none)
    assert "--effort" not in cmd_none


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("gemini-4-hydrogen", "xhigh"),
        ("gemini-4-neon-high", None),
        ("gemini-4-neon-xhigh", None),
        ("gemini-pro-orchestrator-neon", None),
    ],
)
async def test_agy_fresh_turn_passes_exact_selection_to_subprocess(tmp_path, monkeypatch, model, effort):
    captured = tmp_path / "argv.json"
    script = tmp_path / "stub_agy.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.stdin.readline()\n"
        f"Path({str(captured)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "print(json.dumps({'event':'result','result':{'conversation_id':'c-xhigh','status':'SUCCESS','response':'ok'}}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: [sys.executable, str(script)],
    )
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: tmp_path / "projects")
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(
        session_id="sess_run_x",
        agent="antigravity",
        cwd=str(tmp_path),
        model=model,
        effort=effort,
    )
    task = Task(
        task_id="task_run_x",
        session_id=session.session_id,
        agent="antigravity",
        message="hi",
        cwd=str(tmp_path),
        model=model,
        effort=effort,
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"
    argv = json.loads(captured.read_text(encoding="utf-8"))
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == model
    assert argv.index("--model") < argv.index("-p")
    assert "--conversation" not in argv
    if effort is None:
        assert "--effort" not in argv
    else:
        assert argv[argv.index("--effort") + 1] == effort
        assert argv.index("--effort") < argv.index("-p")
