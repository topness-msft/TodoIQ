"""Email signatures on Graph-sent mail.

Phil: "How should we handle email signatures? Can they pull from outlook?"

Measured first. The email voice layer has always told the drafter "The
signature block auto-appends, so do not retype it." That is true of the
Outlook client, which applies a signature at compose time. It is NOT true of
Microsoft Graph: /me/sendMail and /me/messages/{id}/reply send exactly the
body they are given. Graph exposes no signature API at all.

Fetched the real message Riveter sent on 2026-08-25 (action 264, "Agent 365
data access for CAPE"). It ends:

    Thanks,<br>Phil

No signature block. Three of three delivered emails were drafted by the
structured/WorkIQ engine, which until 9b49ce2 received no voice layer at
all -- so the false claim had not even been reaching the drafter. Now that
it does, the claim has to be true or gone.

The signature is appended to the DRAFT, not at send time, because the user
approves the exact text that goes out. Appending after approval would send
something they never read. And it is appended deterministically rather than
asked for in the prompt: a signature is a fixed block, exactly the
"mechanical, checkable" class that test_meeting_preferences records as
needing the inline floor rather than model judgement.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.services import cowork_runner as cr  # noqa: E402
from src.services import workspace_settings as ws  # noqa: E402


def _with(doc):
    return mock.patch.object(ws, "_read_settings", lambda: doc)


class EmailSignatureTest(unittest.TestCase):
    def setUp(self):
        cr.reset_voice_settings_cache()
        self.addCleanup(cr.reset_voice_settings_cache)

    def test_absent_when_unconfigured(self):
        with _with({}):
            self.assertIsNone(cr.email_signature())

    def test_explicit_text_wins(self):
        with _with({"email_signature": {"text": "Phil\nCAT"}}):
            self.assertEqual(cr.email_signature(), "Phil\nCAT")

    def test_blank_text_is_absent_not_empty(self):
        with _with({"email_signature": {"text": "   "}}):
            self.assertIsNone(cr.email_signature())

    def test_it_reads_the_outlook_signature_file(self, ):
        with mock.patch.object(cr, "OUTLOOK_SIGNATURE_DIR", _fixture_dir()):
            with _with({"email_signature": {"outlook_name": "Work"}}):
                self.assertEqual(cr.email_signature(), "Phil Topness\nCAT")

    def test_it_reads_the_utf16_export_outlook_actually_writes(self):
        """The real file on this machine is UTF-16, not UTF-8.

        The first fixture was written UTF-8, so the test passed while the
        real signature raised UnicodeDecodeError -- which is not an OSError,
        so it would have crashed the preview instead of reporting none.
        """
        with mock.patch.object(cr, "OUTLOOK_SIGNATURE_DIR", _fixture_dir()):
            with _with({"email_signature": {"outlook_name": "Utf16"}}):
                self.assertEqual(cr.email_signature(), "Phil Topness\nCAT")

    def test_undecodable_bytes_report_no_signature_rather_than_raising(self):
        with mock.patch.object(cr, "OUTLOOK_SIGNATURE_DIR", _fixture_dir()):
            with _with({"email_signature": {"outlook_name": "Binary"}}):
                self.assertIsNone(cr.email_signature())

    def test_a_missing_outlook_file_is_absent_not_an_error(self):
        with mock.patch.object(cr, "OUTLOOK_SIGNATURE_DIR", _fixture_dir()):
            with _with({"email_signature": {"outlook_name": "Nope"}}):
                self.assertIsNone(cr.email_signature())

    def test_a_name_cannot_escape_the_signature_directory(self):
        """The name is a filename, not a path."""
        for name in ("..\\..\\secrets", "../../secrets", "a/b", "a\\b"):
            with mock.patch.object(cr, "OUTLOOK_SIGNATURE_DIR", _fixture_dir()):
                with _with({"email_signature": {"outlook_name": name}}):
                    self.assertIsNone(cr.email_signature(), name)

    def test_non_string_configuration_is_rejected(self):
        for block in (5, "text", ["a"], {"text": 5}, {"outlook_name": 7}):
            with _with({"email_signature": block}):
                self.assertIsNone(cr.email_signature())


class EmailDraftSignatureTest(unittest.TestCase):
    """The signature has to be in the text the user approves."""

    def setUp(self):
        cr.reset_voice_settings_cache()
        self.addCleanup(cr.reset_voice_settings_cache)

    def _draft(self, body):
        from src.services import structured_delivery as sd

        payload = {
            "channel": "email", "mode": "new", "to": ["a@x.com"],
            "subject": "Hello", "body": body,
        }
        sd.apply_email_signature(payload)
        return payload["body"], sd._preview_draft(payload)

    def test_the_signature_is_appended_to_the_body(self):
        with _with({"email_signature": {"text": "Phil Topness\nCAT"}}):
            body, draft = self._draft("Hi Sally,\n\nThoughts?\n\nPhil")
        self.assertTrue(body.endswith("Phil Topness\nCAT"))
        # The user approves the draft, so it must show what will be sent.
        self.assertIn("Phil Topness\nCAT", draft)

    def test_it_is_not_appended_twice(self):
        with _with({"email_signature": {"text": "Phil Topness\nCAT"}}):
            body, _ = self._draft("Hi Sally,\n\nPhil\n\nPhil Topness\nCAT")
        self.assertEqual(body.count("Phil Topness"), 1)

    def test_nothing_changes_when_no_signature_is_configured(self):
        with _with({}):
            body, _ = self._draft("Hi Sally,\n\nPhil")
        self.assertEqual(body, "Hi Sally,\n\nPhil")

    def test_only_email_payloads_are_touched(self):
        from src.services import structured_delivery as sd

        payload = {"channel": "teams", "body": "Hi - quick one"}
        with _with({"email_signature": {"text": "Phil Topness\nCAT"}}):
            sd.apply_email_signature(payload)
        self.assertEqual(payload["body"], "Hi - quick one")


class EmailVoiceClaimTest(unittest.TestCase):
    def setUp(self):
        cr.reset_voice_settings_cache()
        self.addCleanup(cr.reset_voice_settings_cache)

    def test_the_voice_no_longer_claims_a_signature_auto_appends(self):
        """Graph appends nothing. Now that this layer actually reaches the
        email drafter, the claim has to stop being made."""
        with _with({}):
            voice = cr.voice_layer("email")
        self.assertNotIn("auto-append", voice)


def _fixture_dir():
    import pathlib
    import tempfile

    directory = pathlib.Path(tempfile.mkdtemp())
    (directory / "Work.txt").write_text(
        "Phil Topness\nCAT\n \n", encoding="utf-8"
    )
    # What Outlook actually writes on this machine: UTF-16 with a BOM.
    (directory / "Utf16.txt").write_bytes(
        "Phil Topness\r\nCAT\r\n \r\n".encode("utf-16")
    )
    (directory / "Binary.txt").write_bytes(b"\xff\x81\x00\x81\x0f")
    return directory


if __name__ == "__main__":
    unittest.main()
