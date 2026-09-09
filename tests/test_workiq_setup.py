import json
import subprocess
from pathlib import Path

import pytest

from src.services import workiq_setup
from src.services.workiq_setup import (
    WorkIQAccountError,
    WorkIQAccountRequiredError,
    WorkIQSetup,
)


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


def test_configured_account_uses_pinned_config_show_and_returns_one_account(tmp_path):
    root, node = make_runtime(tmp_path)
    calls = []

    def executor(argv, **kwargs):
        calls.append((argv, kwargs))
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": (
                    "I-accept-EULA=true\n"
                    "defaultAccount=  user@example.com  \n"
                    "isMSITTenant=true\n"
                ),
                "stderr": "",
            },
        )()

    setup = WorkIQSetup(runtime_root=root, node_path=node, executor=executor)

    assert setup.configured_account() == "user@example.com"
    assert calls == [(
        [
            str(
                (
                    root
                    / "node_modules/@microsoft/workiq/bin/win-x64/workiq.exe"
                ).resolve()
            ),
            "config",
            "show",
        ],
        {
            "capture_output": True,
            "text": True,
            "timeout": 30,
            "shell": False,
            "cwd": str(workiq_setup.PROJECT_ROOT),
            **(
                {"creationflags": subprocess.CREATE_NO_WINDOW}
                if hasattr(subprocess, "CREATE_NO_WINDOW")
                else {}
            ),
        },
    )]


@pytest.mark.parametrize(
    ("stdout", "returncode", "error_type"),
    [
        ("I-accept-EULA=true\n", 0, WorkIQAccountRequiredError),
        ("defaultAccount=\n", 0, WorkIQAccountRequiredError),
        (
            "defaultAccount=one@example.com\ndefaultAccount=two@example.com\n",
            0,
            WorkIQAccountError,
        ),
        ("not key value output", 0, WorkIQAccountError),
        (
            "defaultAccount=user@example.com=unexpected\n",
            0,
            WorkIQAccountError,
        ),
        ("defaultAccount=private@example.com", 1, WorkIQAccountError),
    ],
)
def test_configured_account_fails_closed_without_leaking_output(
    tmp_path, stdout, returncode, error_type
):
    root, node = make_runtime(tmp_path)

    def executor(_argv, **_kwargs):
        return type(
            "Result",
            (),
            {"returncode": returncode, "stdout": stdout, "stderr": "token=secret"},
        )()

    setup = WorkIQSetup(runtime_root=root, node_path=node, executor=executor)

    with pytest.raises(error_type) as caught:
        setup.configured_account()

    message = str(caught.value)
    assert "private@example.com" not in message
    assert "one@example.com" not in message
    assert "secret" not in message


def test_configured_account_timeout_is_generic_and_redacted(tmp_path):
    root, node = make_runtime(tmp_path)

    def executor(_argv, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["workiq", "config", "show"],
            timeout=1,
            output="defaultAccount=private@example.com",
        )

    setup = WorkIQSetup(runtime_root=root, node_path=node, executor=executor)

    with pytest.raises(WorkIQAccountError) as caught:
        setup.configured_account(timeout=1)

    assert "private@example.com" not in str(caught.value)
