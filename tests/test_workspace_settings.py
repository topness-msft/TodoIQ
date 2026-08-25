"""Validation tests for the disabled-by-default task workspace setting."""

import json
from pathlib import Path

import pytest


class TestWorkspaceSettings:
    def test_absent_settings_file_is_reported_not_assumed(
        self, tmp_path, monkeypatch
    ):
        """Losing settings.json degraded behaviour with no signal anywhere.

        The file was left behind in a checkout migration. Meeting preferences
        and the Cowork transport flag silently reverted to defaults, and the
        only symptom was meeting times on the hour days later. A missing file
        is a fact worth stating.
        """
        import src.services.workspace_settings as ws

        monkeypatch.setattr(ws, "SETTINGS_PATH", tmp_path / "missing.json")
        warning = ws.missing_settings_warning()
        assert warning is not None
        assert "missing.json" in warning

        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"cowork_api_transport": True}), encoding="utf-8")
        monkeypatch.setattr(ws, "SETTINGS_PATH", path)
        assert ws.missing_settings_warning() is None

    def test_missing_file_is_disabled(self, tmp_path, monkeypatch):
        import src.services.workspace_settings as ws

        monkeypatch.setattr(ws, "SETTINGS_PATH", tmp_path / "missing.json")
        assert ws.get_workspace_settings() == {"enabled": False}

    def test_malformed_file_is_disabled(self, tmp_path, monkeypatch):
        import src.services.workspace_settings as ws

        path = tmp_path / "settings.json"
        path.write_text("{bad", encoding="utf-8")
        monkeypatch.setattr(ws, "SETTINGS_PATH", path)
        assert ws.get_workspace_settings() == {"enabled": False}

    def test_valid_existing_absolute_root(self, tmp_path, monkeypatch):
        import src.services.workspace_settings as ws

        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {"task_workspaces": {"enabled": True, "root": str(tmp_path)}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(ws, "SETTINGS_PATH", path)
        settings = ws.get_workspace_settings()
        assert settings["enabled"] is True
        assert settings["root"] == str(tmp_path.resolve())

    def test_rejects_relative_nonexistent_and_unc_roots(self, tmp_path):
        from src.services.workspace_settings import validate_workspace_root

        assert validate_workspace_root("relative/path") is None
        assert validate_workspace_root(str(tmp_path / "missing")) is None
        assert validate_workspace_root(r"\\server\share") is None
        assert validate_workspace_root("//server/share") is None

    def test_onedrive_root_is_accepted_when_present(self):
        from src.services.workspace_settings import validate_workspace_root

        root = Path(
            r"C:\Users\phtopnes\OneDrive - Microsoft\Documents\__TodoIq"
        )
        if not root.exists():
            pytest.skip("OneDrive root not present on this machine")
        assert validate_workspace_root(str(root)) == root.resolve()
