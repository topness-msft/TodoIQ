import json
from pathlib import Path

from src.services import workiq_setup
from src.services.workiq_setup import WorkIQSetup


def make_runtime(tmp_path, version="1.0.0"):
    root = tmp_path / "runtime"
    package = root / "node_modules" / "@microsoft" / "workiq"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": version}))
    (package / "bin" / "win-x64").mkdir(parents=True)
    (package / "bin" / "win-x64" / "workiq.exe").write_text("")
    (package / "bin" / "win-arm64").mkdir(parents=True)
    (package / "bin" / "win-arm64" / "workiq.exe").write_text("")
    node = tmp_path / "node.exe"
    node.write_text("")
    return root, node


def test_setup_detects_missing_and_version_mismatch(tmp_path):
    setup = WorkIQSetup(runtime_root=tmp_path / "missing", node_path=tmp_path / "node.exe")
    assert setup.inspect()["state"] == "missing_cli"

    root, node = make_runtime(tmp_path, version="0.9.0")
    setup = WorkIQSetup(runtime_root=root, node_path=node)
    status = setup.inspect()
    assert status["state"] == "version_mismatch"
    assert status["required_version"] == "1.0.0"


def test_runtime_command_uses_only_app_owned_pinned_install(tmp_path):
    root, node = make_runtime(tmp_path)
    setup = WorkIQSetup(runtime_root=root, node_path=node)
    assert setup.runtime_command() == [
        str(
            (
                root
                / "node_modules/@microsoft/workiq/bin/win-x64/workiq.exe"
            ).resolve()
        ),
        "mcp",
    ]


def test_accept_eula_uses_fixed_argv_and_redacts_output(tmp_path):
    root, node = make_runtime(tmp_path)
    calls = []

    def executor(argv, **kwargs):
        calls.append((argv, kwargs))
        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": "Bearer secret-token", "stderr": ""},
        )()

    setup = WorkIQSetup(runtime_root=root, node_path=node, executor=executor)
    result = setup.accept_eula()
    assert result["ok"] is True
    assert calls[0][0] == [
        str((root / "node_modules/@microsoft/workiq/bin/win-x64/workiq.exe").resolve()),
        "accept-eula",
    ]
    assert calls[0][1]["shell"] is False
    assert "secret-token" not in json.dumps(result)


def test_arm64_uses_native_package_binary(tmp_path, monkeypatch):
    root, node = make_runtime(tmp_path)
    monkeypatch.setattr(workiq_setup.platform, "machine", lambda: "ARM64")
    setup = WorkIQSetup(runtime_root=root, node_path=node)
    assert setup.entry_point == (
        root / "node_modules/@microsoft/workiq/bin/win-arm64/workiq.exe"
    )
    assert setup.runtime_command() == [
        str((root / "node_modules/@microsoft/workiq/bin/win-arm64/workiq.exe").resolve()),
        "mcp",
    ]
