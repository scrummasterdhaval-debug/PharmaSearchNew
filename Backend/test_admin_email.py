"""Sign-up and reset emails, with every way of sending them replaced.

Backend/.env on a developer's machine may hold real mail settings, so each
test clears them and patches both senders: nothing here can send a message.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services import admin_auth

PASSWORD = "a-long-test-password"
HOST = "pharma-test.onrender.com"


class AdminEmailTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        settings = {
            "PHARMASEARCH_DB_PATH": str(Path(folder.name) / "accounts.db"),
            "SMTP_HOST": "", "SMTP_USER": "", "SMTP_PASSWORD": "", "BREVO_API_KEY": "",
            "PHARMASEARCH_BASE_URL": "", "RENDER_EXTERNAL_HOSTNAME": HOST, "FROM_EMAIL": "sender@example.com",
            "PHARMASEARCH_REQUIRE_EMAIL_CONFIRMATION": "1",
        }
        for patcher in (
            patch.dict(os.environ, settings, clear=False),
            patch("repository.DB_PATH", Path(settings["PHARMASEARCH_DB_PATH"]), create=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.smtp = patch("smtplib.SMTP").start()
        self.post = patch("requests.post", return_value=Mock(status_code=201, text="{}")).start()
        self.addCleanup(patch.stopall)

    def test_links_are_built_on_the_configured_address_not_localhost(self):
        self.assertEqual(admin_auth.public_base_url(), f"https://{HOST}")
        with patch.dict(os.environ, {"PHARMASEARCH_BASE_URL": "https://search.example.com/"}):
            self.assertEqual(admin_auth.public_base_url(), "https://search.example.com")
        with patch.dict(os.environ, {"RENDER_EXTERNAL_HOSTNAME": ""}):
            self.assertEqual(admin_auth.public_base_url(), "http://localhost:8000")

    def test_without_a_mail_route_nothing_is_sent_and_the_link_is_logged(self):
        with self.assertLogs(admin_auth.logger, level="WARNING") as logged:
            sent = admin_auth.send_verification_email("person@example.com", "tok123")
        self.assertFalse(sent)
        self.assertFalse(self.smtp.called)
        self.assertFalse(self.post.called)
        self.assertIn(f"https://{HOST}/admin/verify-email?token=tok123", "\n".join(logged.output))

    def test_the_mail_api_is_used_when_its_key_is_set(self):
        with patch.dict(os.environ, {"BREVO_API_KEY": "key"}):
            self.assertTrue(admin_auth.send_verification_email("person@example.com", "tok123"))
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["to"], [{"email": "person@example.com"}])
        self.assertEqual(body["sender"]["email"], "sender@example.com")
        self.assertIn(f"https://{HOST}/admin/verify-email?token=tok123", body["textContent"])
        self.assertFalse(self.smtp.called)

    def test_a_refused_send_is_a_failure_and_still_logs_the_link(self):
        self.post.return_value = Mock(status_code=401, text="unauthorized")
        with patch.dict(os.environ, {"BREVO_API_KEY": "bad"}):
            with self.assertLogs(admin_auth.logger, level="ERROR") as logged:
                self.assertFalse(admin_auth.send_password_reset_email("person@example.com", "tok456"))
        self.assertIn(f"https://{HOST}/admin/reset-password?token=tok456", "\n".join(logged.output))

    def test_smtp_is_used_with_a_timeout(self):
        with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}):
            self.assertTrue(admin_auth.send_verification_email("person@example.com", "tok123"))
        self.assertEqual(self.smtp.call_args.kwargs["timeout"], 20)
        self.assertTrue(self.smtp.return_value.__enter__.return_value.send_message.called)
        self.assertFalse(self.post.called)

    def test_sign_up_says_so_when_the_email_did_not_go(self):
        with patch("services.admin_auth.send_verification_email", return_value=False):
            self.assertEqual(admin_auth.register_user("one@example.com", PASSWORD), (True, admin_auth.EMAIL_NOT_SENT))
        with patch("services.admin_auth.send_verification_email", return_value=True):
            self.assertEqual(admin_auth.register_user("two@example.com", PASSWORD), (True, ""))

    def test_the_email_check_describes_the_key_without_showing_it(self):
        whole = "xkeysib-" + "a" * 64 + "-" + "b" * 16
        with patch.dict(os.environ, {"BREVO_API_KEY": whole}):
            status = admin_auth.mail_status()
        self.assertEqual(status["route"], "Brevo API (HTTPS)")
        self.assertTrue(status["key_looks_whole"])
        self.assertEqual((status["key_prefix"], status["key_length"]), ("xkeysib-", 89))
        self.assertNotIn(whole, str(status))
        for broken in ("xsmtpsib-" + "a" * 80, "xkeysib-short", "xkeysib-" + "*" * 81):
            with patch.dict(os.environ, {"BREVO_API_KEY": broken}):
                self.assertFalse(admin_auth.mail_status()["key_looks_whole"], broken[:12])
        self.assertEqual(admin_auth.mail_status()["route"], "")

    def test_a_test_send_reports_the_mail_services_own_answer(self):
        self.assertEqual(admin_auth.send_test_email("nonsense")[0], False)
        self.assertIn("No mail route", admin_auth.send_test_email("person@example.com")[1])
        self.post.return_value = Mock(status_code=401, text='{"message":"Key not found"}')
        with patch.dict(os.environ, {"BREVO_API_KEY": "bad"}):
            sent, detail = admin_auth.send_test_email("person@example.com")
        self.assertFalse(sent)
        self.assertIn("Key not found", detail)
        self.post.return_value = Mock(status_code=201, text="{}")
        with patch.dict(os.environ, {"BREVO_API_KEY": "good"}):
            self.assertTrue(admin_auth.send_test_email("person@example.com")[0])

    def test_an_admin_can_activate_an_account_no_email_reached(self):
        admin_auth.register_user("waiting@example.com", PASSWORD)
        self.assertEqual([p["username"] for p in admin_auth.pending_users()], ["waiting@example.com"])
        self.assertFalse(admin_auth.authenticate_db_user("waiting@example.com", PASSWORD)[0])
        self.assertTrue(admin_auth.activate_user("Waiting@Example.com"))
        self.assertEqual(admin_auth.pending_users(), [])
        self.assertTrue(admin_auth.authenticate_db_user("waiting@example.com", PASSWORD)[0])
        self.assertFalse(admin_auth.activate_user("waiting@example.com"))
        self.assertFalse(admin_auth.activate_user("nobody@example.com"))

    def test_the_admin_digest_is_read_however_it_was_pasted(self):
        digest = admin_auth.hash_password(PASSWORD)
        pasted = (
            digest, f"  {digest}  ", f'"{digest}"', f"PHARMASEARCH_ADMIN_PASSWORD_HASH={digest}",
            f"PHARMASEARCH_ADMIN_PASSWORD_HASH = '{digest}'",
        )
        for value in pasted:
            with patch.dict(os.environ, {"PHARMASEARCH_ADMIN_USERNAME": "admin", "PHARMASEARCH_ADMIN_PASSWORD_HASH": value}):
                self.assertTrue(admin_auth.authenticate("admin", PASSWORD), value[:40])
                self.assertFalse(admin_auth.authenticate("admin", "the-wrong-password"), value[:40])

    def test_the_admin_password_can_be_given_directly(self):
        digest_for_another = admin_auth.hash_password("a-different-password")
        direct = {
            "PHARMASEARCH_ADMIN_USERNAME": "admin", "PHARMASEARCH_ADMIN_PASSWORD": f"  {PASSWORD}  ",
            "PHARMASEARCH_ADMIN_PASSWORD_HASH": digest_for_another,
        }
        with patch.dict(os.environ, direct):
            self.assertTrue(admin_auth.admin_is_configured())
            self.assertTrue(admin_auth.authenticate("admin", PASSWORD))
            self.assertFalse(admin_auth.authenticate("admin", "a-different-password"))
            self.assertFalse(admin_auth.authenticate("someone", PASSWORD))
            self.assertFalse(admin_auth.authenticate("admin", ""))
        short = {"PHARMASEARCH_ADMIN_USERNAME": "admin", "PHARMASEARCH_ADMIN_PASSWORD": "short", "PHARMASEARCH_ADMIN_PASSWORD_HASH": ""}
        with patch.dict(os.environ, short):
            self.assertFalse(admin_auth.admin_is_configured())
            self.assertFalse(admin_auth.authenticate("admin", "short"))
        digest_only = {"PHARMASEARCH_ADMIN_USERNAME": "admin", "PHARMASEARCH_ADMIN_PASSWORD": "", "PHARMASEARCH_ADMIN_PASSWORD_HASH": admin_auth.hash_password(PASSWORD)}
        with patch.dict(os.environ, digest_only):
            self.assertTrue(admin_auth.authenticate("admin", PASSWORD))

    def test_the_sender_address_is_cleaned_and_a_bad_one_is_named(self):
        for pasted in (" sender@example.com ", '"sender@example.com"', "PharmaSearch <sender@example.com>"):
            with patch.dict(os.environ, {"FROM_EMAIL": pasted}):
                self.assertEqual(admin_auth.sender_address(), "sender@example.com", pasted)
                self.assertEqual(admin_auth.sender_problem(), "", pasted)
        with patch.dict(os.environ, {"FROM_EMAIL": "", "BREVO_API_KEY": "key"}):
            sent, detail = admin_auth.send_test_email("person@example.com")
            self.assertFalse(sent)
            self.assertIn("FROM_EMAIL is not set", detail)
            self.assertIn("FROM_EMAIL is not set", admin_auth.mail_status()["sender_problem"])
        self.assertFalse(self.post.called)
        with patch.dict(os.environ, {"FROM_EMAIL": "someone at example", "BREVO_API_KEY": "key"}):
            self.assertIn("not a valid email address", admin_auth.send_test_email("person@example.com")[1])
        with patch.dict(os.environ, {"FROM_EMAIL": "PharmaSearch <sender@example.com>", "BREVO_API_KEY": "key"}):
            self.assertTrue(admin_auth.send_test_email("person@example.com")[0])
        self.assertEqual(self.post.call_args.kwargs["json"]["sender"]["email"], "sender@example.com")


if __name__ == "__main__":
    unittest.main()
