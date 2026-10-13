"""Who may reach what: registered accounts are users, and the admin area is not theirs.

Runs against a temporary database with both ways of sending mail replaced,
so nothing here can send a message or touch the real accounts.
"""
import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services import admin_auth

PASSWORD = "a-long-test-password"
ADMIN_PASSWORD = "the-admin-test-password"
EMAIL = "person@example.com"


class _Accounts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        settings = {
            "SMTP_HOST": "", "SMTP_USER": "", "SMTP_PASSWORD": "", "BREVO_API_KEY": "",
            "PHARMASEARCH_BASE_URL": "https://search.example.com", "FROM_EMAIL": "sender@example.com",
            "PHARMASEARCH_ADMIN_USERNAME": "admin", "PHARMASEARCH_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "PHARMASEARCH_ADMIN_PASSWORD_HASH": "", "PHARMASEARCH_SESSION_SECRET": "x" * 48,
            "PHARMASEARCH_REQUIRE_EMAIL_CONFIRMATION": "1",
        }
        for patcher in (
            patch.dict(os.environ, settings, clear=False),
            patch("repository.DB_PATH", Path(folder.name) / "accounts.db", create=True),
            patch.dict(admin_auth._form_uses, {}, clear=True),
            patch("smtplib.SMTP"),
            patch("requests.post", return_value=Mock(status_code=201, text="{}")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def register(self, email=EMAIL, password=PASSWORD, confirm=True):
        """Sign up and return the link's token, as the email would carry it."""
        with patch("services.admin_auth.send_verification_email", return_value=True) as sent:
            self.assertEqual(admin_auth.register_user(email, password), (True, ""))
        token = sent.call_args.args[1] if sent.called else ""
        if confirm:
            self.assertTrue(admin_auth.verify_email(token)[0])
        return token


class AccountRuleTests(_Accounts):
    def test_a_registered_account_is_a_user_and_the_configured_admin_is_an_admin(self):
        self.register()
        self.assertEqual(admin_auth.user_role(EMAIL), "user")
        self.assertFalse(admin_auth.is_admin(EMAIL))
        self.assertEqual(admin_auth.user_role("admin"), "admin")
        self.assertEqual(admin_auth.user_role("nobody@example.com"), "")
        self.assertEqual(admin_auth.user_role(""), "")
        self.assertEqual(admin_auth.user_role(None), "")

    def test_an_unconfirmed_account_has_no_role_and_no_session(self):
        self.register(confirm=False)
        self.assertEqual(admin_auth.user_role(EMAIL), "")
        self.assertIsNone(admin_auth.read_session(admin_auth.issue_session(EMAIL)))

    def test_only_an_admin_decision_changes_a_role(self):
        self.register()
        self.assertTrue(admin_auth.set_role(EMAIL, "admin"))
        self.assertTrue(admin_auth.is_admin(EMAIL))
        self.assertEqual([(a["username"], a["role"]) for a in admin_auth.list_accounts()], [(EMAIL, "admin")])
        self.assertTrue(admin_auth.set_role(EMAIL, "user"))
        self.assertFalse(admin_auth.is_admin(EMAIL))
        self.assertFalse(admin_auth.set_role(EMAIL, "owner"))
        self.assertFalse(admin_auth.set_role("nobody@example.com", "admin"))

    def test_an_emailed_link_is_stored_as_a_digest_and_works_once(self):
        from repository import get_connection

        token = self.register(confirm=False)
        with get_connection() as conn:
            stored = conn.execute("SELECT verification_token FROM admin_users").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual(len(stored), 64)
        self.assertFalse(admin_auth.verify_email(stored)[0])  # the stored value is not a link
        self.assertTrue(admin_auth.verify_email(token)[0])
        self.assertFalse(admin_auth.verify_email(token)[0])

    def test_a_reset_link_is_stored_as_a_digest_and_works_once(self):
        from repository import get_connection

        self.register()
        with patch("services.admin_auth.send_password_reset_email", return_value=True) as sent:
            admin_auth.request_password_reset(EMAIL)
        token = sent.call_args.args[1]
        with get_connection() as conn:
            stored = conn.execute("SELECT reset_token FROM admin_users").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertFalse(admin_auth.reset_password(stored, "another-long-password")[0])
        self.assertTrue(admin_auth.reset_password(token, "another-long-password")[0])
        self.assertFalse(admin_auth.reset_password(token, "yet-another-password")[0])
        self.assertTrue(admin_auth.authenticate_db_user(EMAIL, "another-long-password")[0])

    def test_signing_up_again_does_not_say_the_address_is_taken_or_change_its_password(self):
        self.register()
        with patch("services.admin_auth.send_verification_email") as sent:
            self.assertEqual(admin_auth.register_user(EMAIL, "an-attackers-password"), (True, ""))
        self.assertFalse(sent.called)
        self.assertTrue(admin_auth.authenticate_db_user(EMAIL, PASSWORD)[0])
        self.assertFalse(admin_auth.authenticate_db_user(EMAIL, "an-attackers-password")[0])

    def test_signing_up_again_before_confirming_sends_a_fresh_link(self):
        first = self.register(confirm=False)
        second = self.register(password="the-second-password", confirm=False)
        self.assertNotEqual(first, second)
        self.assertFalse(admin_auth.verify_email(first)[0])
        self.assertTrue(admin_auth.verify_email(second)[0])
        self.assertTrue(admin_auth.authenticate_db_user(EMAIL, "the-second-password")[0])

    def test_a_form_stops_answering_after_its_limit(self):
        self.assertEqual([admin_auth.allow_form_use("signup", "10.0.0.1", 3) for _ in range(5)],
                         [True, True, True, False, False])
        self.assertTrue(admin_auth.allow_form_use("signup", "10.0.0.2", 3))
        self.assertTrue(admin_auth.allow_form_use("reset", "10.0.0.1", 3))
        with patch("time.time", return_value=__import__("time").time() + 3601):
            self.assertTrue(admin_auth.allow_form_use("signup", "10.0.0.1", 3))


class FreshDeploymentTests(_Accounts):
    def test_the_seeded_rows_carry_a_molecule_key_and_are_counted(self):
        from repository import get_connection
        from services import admin_metrics

        overview = admin_metrics.overview()
        with get_connection() as conn:
            without_key = conn.execute(
                "SELECT COUNT(*) FROM product_details WHERE COALESCE(substance_key, '') = ''"
            ).fetchone()[0]
        self.assertGreater(overview["products"], 1000)
        self.assertEqual(without_key, 0)
        self.assertGreater(overview["substances"], 0)


class AdminSignInDiagnosisTests(_Accounts):
    def test_the_admin_name_is_accepted_in_any_case(self):
        self.assertTrue(admin_auth.authenticate("Admin", ADMIN_PASSWORD))
        self.assertTrue(admin_auth.authenticate(" ADMIN ", ADMIN_PASSWORD))
        self.assertFalse(admin_auth.authenticate("admin", ADMIN_PASSWORD.upper()))

    def test_a_refused_admin_sign_in_logs_lengths_and_never_the_password(self):
        with self.assertLogs(admin_auth.logger, level="WARNING") as logged:
            self.assertFalse(admin_auth.authenticate("admin", "close-but-wrong"))
        line = "\n".join(logged.output)
        self.assertIn("(15 characters)", line)
        self.assertIn(f"({len(ADMIN_PASSWORD)} characters, PHARMASEARCH_ADMIN_PASSWORD)", line)
        self.assertNotIn("close-but-wrong", line)
        self.assertNotIn(ADMIN_PASSWORD, line)

    def test_the_start_up_line_describes_the_admin_without_the_password(self):
        line = admin_auth.describe_admin_configuration()
        self.assertIn("user name 'admin'", line)
        self.assertIn(f"{len(ADMIN_PASSWORD)} characters", line)
        self.assertNotIn(ADMIN_PASSWORD, line)
        with patch.dict(os.environ, {"PHARMASEARCH_ADMIN_PASSWORD": "short@1"}):
            self.assertIn("too short", admin_auth.describe_admin_configuration())
        with patch.dict(os.environ, {"PHARMASEARCH_ADMIN_USERNAME": ""}):
            self.assertIn("not set", admin_auth.describe_admin_configuration())


class AccessTests(_Accounts):
    """The application itself, asked for pages under each kind of session."""

    def setUp(self):
        super().setUp()
        with patch("sources.open_registers.warm_open_registers"):
            import main
        self.app = main.app

    def get(self, path, user=None, json=False, method="GET"):
        headers = [(b"host", b"localhost")]
        if json:
            headers.append((b"accept", b"application/json"))
        if user:
            cookie = f"{admin_auth.SESSION_COOKIE_NAME}={admin_auth.issue_session(user)}"
            headers.append((b"cookie", cookie.encode()))
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
            "client": ("10.0.0.1", 1), "server": ("localhost", 80), "root_path": "",
        }
        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode("utf-8", "replace")
        return start["status"], dict(start["headers"]).get(b"location", b"").decode(), body

    def test_without_a_session_everything_leads_to_the_sign_in_page(self):
        for path in ("/", "/admin", "/catalogue", "/crawl/metformin"):
            status, location, _ = self.get(path)
            self.assertEqual(status, 303, path)
            self.assertTrue(location.startswith("/admin/login"), path)
        self.assertEqual(self.get("/api/sources", json=True)[0], 401)

    def test_a_user_is_refused_the_admin_area_and_the_maintenance_routes(self):
        self.register()
        for path in ("/admin", "/admin/operations", "/admin/data", "/admin/email", "/admin/records",
                     "/crawl/metformin", "/crawl_all_products/metformin", "/mhra_full/metformin", "/reset_db"):
            status, _, body = self.get(path, user=EMAIL)
            self.assertEqual(status, 403, path)
            self.assertIn("for administrators", body, path)
        for path in ("/api/data_health", "/api/export_directory", "/admin/api/dashboard"):
            self.assertEqual(self.get(path, user=EMAIL, json=True)[0], 403, path)
        for path in ("/admin/reset-database", "/admin/email/role", "/admin/email/activate", "/crawl_url"):
            self.assertEqual(self.get(path, user=EMAIL, method="POST")[0], 403, path)

    def test_a_user_keeps_the_search_and_can_sign_out(self):
        self.register()
        status, _, body = self.get("/", user=EMAIL)
        self.assertEqual(status, 200)
        self.assertNotIn('href="/admin"', body)
        self.assertNotIn('href="/admin/operations"', body)
        self.assertIn(EMAIL, body)
        self.assertEqual(self.get("/api/sources", user=EMAIL, json=True)[0], 200)
        status, location, _ = self.get("/admin/logout", user=EMAIL, method="POST")
        self.assertEqual((status, location), (303, "/admin/login"))

    def test_a_user_has_the_search_and_nothing_else(self):
        self.register()
        body = self.get("/", user=EMAIL)[2]
        for link in ('href="/catalogue"', 'href="/connector_health_page"', 'href="/connector_status_page"', 'href="/admin"'):
            self.assertNotIn(link, body)
        for path in ("/catalogue", "/products", "/connector_status_page", "/connector_health_page",
                     "/harvest_coverage", "/search_jobs", "/search_jobs/", "/deep_export/metformin"):
            self.assertEqual(self.get(path, user=EMAIL)[0], 403, path)
        for path in ("/connector_status", "/connector_health", "/ai_status"):
            self.assertEqual(self.get(path, user=EMAIL, json=True)[0], 403, path)
        # One's own search stays one's own: not refused, only not found here.
        self.assertNotEqual(self.get("/search_jobs/no-such-job", user=EMAIL)[0], 403)
        self.assertNotEqual(self.get("/api/search_jobs/no-such-job", user=EMAIL, json=True)[0], 403)
        admin_home = self.get("/", user="admin")[2]
        self.assertIn('href="/connector_health_page"', admin_home)
        self.assertEqual(self.get("/connector_health_page", user="admin")[0], 200)

    def test_an_admin_reaches_the_admin_area_and_a_promoted_user_does_too(self):
        self.register()
        self.assertEqual(self.get("/admin/email", user="admin")[0], 200)
        self.assertIn('href="/admin"', self.get("/", user="admin")[2])
        admin_auth.set_role(EMAIL, "admin")
        self.assertEqual(self.get("/admin/email", user=EMAIL)[0], 200)
        admin_auth.set_role(EMAIL, "user")
        self.assertEqual(self.get("/admin/email", user=EMAIL)[0], 403)

    def test_a_removed_account_loses_its_session(self):
        from repository import get_connection

        self.register()
        self.assertEqual(self.get("/", user=EMAIL)[0], 200)
        with get_connection() as conn:
            conn.execute("UPDATE admin_users SET is_active = 0")
        status, location, _ = self.get("/", user=EMAIL)
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/admin/login"))


class OpenSignUpTests(_Accounts):
    """The default: create an account and you are in, with no email involved."""

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"PHARMASEARCH_REQUIRE_EMAIL_CONFIRMATION": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_new_account_signs_in_at_once_as_a_user_and_no_email_is_sent(self):
        with patch("services.admin_auth.send_verification_email") as sent:
            self.assertEqual(admin_auth.register_user(EMAIL, PASSWORD), (True, ""))
        self.assertFalse(sent.called)
        self.assertEqual(admin_auth.authenticate_db_user(EMAIL, PASSWORD), (True, ""))
        self.assertEqual(admin_auth.user_role(EMAIL), "user")
        self.assertEqual(admin_auth.read_session(admin_auth.issue_session(EMAIL)), EMAIL)
        self.assertEqual(admin_auth.pending_users(), [])
        self.assertEqual([a["username"] for a in admin_auth.list_accounts()], [EMAIL])

    def test_an_address_with_an_account_cannot_be_registered_again(self):
        admin_auth.register_user(EMAIL, PASSWORD)
        self.assertEqual(admin_auth.register_user(EMAIL, "someone-elses-password"), (False, admin_auth.ALREADY_REGISTERED))
        self.assertTrue(admin_auth.authenticate_db_user(EMAIL, PASSWORD)[0])
        self.assertFalse(admin_auth.authenticate_db_user(EMAIL, "someone-elses-password")[0])

    def test_an_account_that_never_confirmed_can_sign_in_once_confirmation_is_off(self):
        with patch.dict(os.environ, {"PHARMASEARCH_REQUIRE_EMAIL_CONFIRMATION": "1"}):
            with patch("services.admin_auth.send_verification_email", return_value=True):
                admin_auth.register_user(EMAIL, PASSWORD)
            self.assertFalse(admin_auth.authenticate_db_user(EMAIL, PASSWORD)[0])
        self.assertTrue(admin_auth.authenticate_db_user(EMAIL, PASSWORD)[0])

    def test_the_sign_up_page_ends_signed_in_on_the_search(self):
        import asyncio
        from urllib.parse import urlencode

        with patch("sources.open_registers.warm_open_registers"):
            import main
        body = urlencode({"username": EMAIL, "password": PASSWORD, "confirm_password": PASSWORD}).encode()
        sent = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/admin/register", "raw_path": b"/admin/register", "query_string": b"", "root_path": "",
            "headers": [(b"host", b"localhost"), (b"content-type", b"application/x-www-form-urlencoded")],
            "client": ("10.0.0.9", 1), "server": ("localhost", 80),
        }
        asyncio.run(main.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        headers = dict(start["headers"])
        self.assertEqual(start["status"], 303)
        self.assertEqual(headers[b"location"], b"/")
        cookie = headers[b"set-cookie"].decode()
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertEqual(admin_auth.read_session(token), EMAIL)
        self.assertFalse(admin_auth.is_admin(EMAIL))


class RegisterChildProcessTests(unittest.TestCase):
    """A register built in its own process: a failure stays there, and pauses."""

    def setUp(self):
        from sources import open_registers

        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.registers = open_registers
        for patcher in (
            patch.object(open_registers, "INDEX_DIR", Path(folder.name)),
            patch.object(open_registers, "REGISTER_PROCESS", "child"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.register = next(iter(open_registers.REGISTERS.values()))

    def run_child(self, returncode):
        done = Mock(returncode=returncode, stderr="Killed", stdout="")
        with patch("subprocess.run", return_value=done) as run:
            result = self.registers.refresh_register(self.register)
        return result, run

    def test_a_killed_build_leaves_the_site_up_and_waits_before_retrying(self):
        with self.assertLogs(self.registers.logger, level="WARNING") as logged:
            result, run = self.run_child(-9)
        self.assertIsNone(result)
        self.assertIn("ran out of memory", " ".join(logged.output))
        self.assertEqual(run.call_args.args[0][-1], self.register.source)
        self.assertEqual(run.call_args.kwargs["env"]["PHARMASEARCH_REGISTER_PROCESS"], "inline")
        _, again = self.run_child(0)
        self.assertFalse(again.called)

    def test_a_successful_build_clears_an_earlier_failure(self):
        self.run_child(1)
        self.registers._failure_marker(self.register).touch()
        os.utime(self.registers._failure_marker(self.register), (0, 0))
        _, run = self.run_child(0)
        self.assertTrue(run.called)
        self.assertFalse(self.registers._failure_marker(self.register).exists())


if __name__ == "__main__":
    unittest.main()
