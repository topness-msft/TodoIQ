"""The API-transport feature flag must fail closed.

TodoIQ is Phil's daily driver and the `cowork` subprocess path is the proven
one. The API transport has a bake window measured in weeks, so anything other
than an explicit `true` must leave the subprocess path in charge.
"""

import json
import unittest
from unittest import mock

from src.services import workspace_settings
from src.services.workspace_settings import api_transport_enabled


class FlagTestBase(unittest.TestCase):
    def _settings(self, payload, exists=True):
        return mock.patch.object(
            workspace_settings, "SETTINGS_PATH",
            mock.Mock(
                exists=mock.Mock(return_value=exists),
                read_text=mock.Mock(return_value=json.dumps(payload)),
            ),
        )


class TestDefaultsOff(FlagTestBase):
    def test_missing_settings_file_is_off(self):
        with self._settings({}, exists=False):
            self.assertFalse(api_transport_enabled())

    def test_empty_settings_is_off(self):
        with self._settings({}):
            self.assertFalse(api_transport_enabled())

    def test_unrelated_settings_do_not_enable_it(self):
        with self._settings({"task_workspaces": {"enabled": True}}):
            self.assertFalse(api_transport_enabled())

    def test_explicit_false_is_off(self):
        with self._settings({"cowork_api_transport": False}):
            self.assertFalse(api_transport_enabled())


class TestOnlyExplicitTrueEnables(FlagTestBase):
    def test_true_enables(self):
        with self._settings({"cowork_api_transport": True}):
            self.assertTrue(api_transport_enabled())

    def test_truthy_string_does_not_enable(self):
        """"false" is a truthy string. Requiring `is True` avoids that trap."""
        with self._settings({"cowork_api_transport": "false"}):
            self.assertFalse(api_transport_enabled())

    def test_truthy_number_does_not_enable(self):
        with self._settings({"cowork_api_transport": 1}):
            self.assertFalse(api_transport_enabled())


class TestMalformedFailsClosed(FlagTestBase):
    def test_invalid_json_is_off(self):
        broken = mock.Mock(
            exists=mock.Mock(return_value=True),
            read_text=mock.Mock(return_value="{not json"),
        )
        with mock.patch.object(workspace_settings, "SETTINGS_PATH", broken):
            self.assertFalse(api_transport_enabled())

    def test_unreadable_file_is_off(self):
        broken = mock.Mock(
            exists=mock.Mock(return_value=True),
            read_text=mock.Mock(side_effect=OSError("locked")),
        )
        with mock.patch.object(workspace_settings, "SETTINGS_PATH", broken):
            self.assertFalse(api_transport_enabled())

    def test_a_json_list_is_off(self):
        with self._settings([1, 2, 3]):
            self.assertFalse(api_transport_enabled())


class TestDoesNotDisturbWorkspaceSettings(FlagTestBase):
    def test_workspace_settings_still_read_independently(self):
        """Both keys live in one document; neither may break the other."""
        with self._settings({"cowork_api_transport": True}):
            self.assertEqual(
                workspace_settings.get_workspace_settings(), {"enabled": False}
            )
            self.assertTrue(api_transport_enabled())


if __name__ == "__main__":
    unittest.main()
