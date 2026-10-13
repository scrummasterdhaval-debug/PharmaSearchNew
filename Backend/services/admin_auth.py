"""Username and password sign-in for the admin section.

Everything here is standard library. Passwords are stored as a scrypt digest
and never in plain text, the session cookie is signed so it cannot be forged
or edited, and both comparisons run in constant time.

Configure with three environment variables:

    PHARMASEARCH_ADMIN_USERNAME       the admin's user name
    PHARMASEARCH_ADMIN_PASSWORD_HASH  the digest printed by tools/make_admin_password.py
    PHARMASEARCH_ADMIN_PASSWORD       or the password itself, at least 12 characters, where the
                                      host keeps its settings secret; used instead of the digest
    PHARMASEARCH_SESSION_SECRET       any long random string

Leaving PHARMASEARCH_SESSION_SECRET unset is allowed for local use: a random
secret is generated per process, which simply signs everyone out on restart.
Without the first two the admin section stays closed and says so, so a missing
configuration can never leave it standing open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import config  # noqa: F401 -- loads Backend/.env before the variables below are read
from core.logging_config import get_logger


logger = get_logger(__name__)

SESSION_COOKIE_NAME = "pharmasearch_admin"
SESSION_TTL_SECONDS = 8 * 60 * 60

# The sign-in guards the whole application, not only /admin, so the cookie has
# to travel with every request rather than just the admin ones.
SESSION_COOKIE_PATH = "/"

# Work factors for scrypt. n is the cost, and raising it makes both a login
# and an offline guess proportionally slower.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32

# A user name and password pair may be tried this many times in this window
# before the account stops answering, so the form cannot be used to guess.
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 15 * 60

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()
_process_secret = secrets.token_urlsafe(48)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return a self-describing digest that verify_password() can read back."""
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_KEY_BYTES,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """True when the password produces the stored digest. Never raises."""
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=_b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(_b64decode(expected)),
        )
    except Exception:
        logger.warning("The stored admin password digest could not be read")
        return False
    return hmac.compare_digest(digest, _b64decode(expected))


def _session_secret() -> str:
    return os.getenv("PHARMASEARCH_SESSION_SECRET") or _process_secret


def admin_username() -> str:
    return (os.getenv("PHARMASEARCH_ADMIN_USERNAME") or "").strip()


def _admin_password_hash() -> str:
    """The stored digest, forgiving the two ways it gets pasted wrong.

    The password tool prints "NAME=scrypt$...", and that whole line, or the
    value in quotes, is what tends to land in a hosting dashboard. Either one
    made every sign-in fail with nothing to say why.
    """
    value = (os.getenv("PHARMASEARCH_ADMIN_PASSWORD_HASH") or "").strip().strip("\"'").strip()
    name, separator, rest = value.partition("=")
    if separator and "$" not in name:
        value = rest.strip().strip("\"'").strip()
    return value


MIN_ADMIN_PASSWORD = 12


def _admin_password() -> str:
    """The admin password given directly, for a host whose settings are already secret.

    A hosting dashboard keeps its environment values the way it keeps API
    keys, so the password can sit there as itself. It spares running the
    digest tool, which is the step that goes wrong. Where the settings are a
    file on disk, the digest remains the better choice. When this is set it is
    the one used, and the digest is ignored.
    """
    return (os.getenv("PHARMASEARCH_ADMIN_PASSWORD") or "").strip()


def admin_is_configured() -> bool:
    """True once a user name and a usable password or digest are both present."""
    if not admin_username():
        return False
    plain = _admin_password()
    if plain:
        return len(plain) >= MIN_ADMIN_PASSWORD
    return bool(_admin_password_hash())


def _sign(payload: str) -> str:
    return _b64encode(
        hmac.new(_session_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    )


def issue_session(username: str) -> str:
    payload = _b64encode(
        json.dumps({"u": username, "exp": int(time.time()) + SESSION_TTL_SECONDS}).encode("utf-8")
    )
    return f"{payload}.{_sign(payload)}"


def read_session(token: str | None) -> str | None:
    """Return the signed-in user name, or None for anything not currently valid."""
    if not token or "." not in token:
        return None
    payload, _, signature = token.partition(".")
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        claims = json.loads(_b64decode(payload))
    except Exception:
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    username = str(claims.get("u") or "")
    if not username:
        return None
    # The configured admin, or a registered account that is still active and
    # confirmed: a session outlives neither a deactivation nor a deletion.
    if username == admin_username():
        return username
    return username if user_exists(username) else None


def _attempt_key(username: str, client_ip: str) -> str:
    return f"{username.lower()}|{client_ip}"


def seconds_until_unlocked(username: str, client_ip: str) -> int:
    """How long this user name and address must wait, or 0 when free to try."""
    key = _attempt_key(username, client_ip)
    now = time.time()
    with _attempts_lock:
        recent = [stamp for stamp in _attempts.get(key, []) if now - stamp < _LOCKOUT_SECONDS]
        _attempts[key] = recent
        if len(recent) < _MAX_ATTEMPTS:
            return 0
        return max(1, int(_LOCKOUT_SECONDS - (now - min(recent))))


def record_failure(username: str, client_ip: str) -> None:
    key = _attempt_key(username, client_ip)
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.time())


def clear_failures(username: str, client_ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(_attempt_key(username, client_ip), None)


def authenticate(username: str, password: str) -> bool:
    """True when the pair matches the configured admin.

    The password is checked even when the user name is already wrong so that
    a wrong name and a wrong password take the same time to answer.
    """
    expected_user = admin_username()
    if not expected_user or not admin_is_configured():
        return False
    plain = _admin_password()
    if plain:
        password_ok = hmac.compare_digest(password.encode("utf-8"), plain.encode("utf-8"))
    else:
        password_ok = verify_password(password, _admin_password_hash())
    # A phone capitalises the first letter of a field; "Admin" is still the admin.
    user_ok = hmac.compare_digest(username.strip().lower(), expected_user.lower())
    if user_ok and not password_ok:
        # Lengths only, never the text: enough for whoever runs the site to see
        # a missing character or an old value, and nothing a reader could use.
        logger.warning(
            "Admin sign-in refused: the password typed (%d characters) is not the one this service "
            "was started with (%d characters, %s).",
            len(password), len(plain) if plain else 0,
            "PHARMASEARCH_ADMIN_PASSWORD" if plain else "PHARMASEARCH_ADMIN_PASSWORD_HASH",
        )
    return password_ok and user_ok


def describe_admin_configuration() -> str:
    """One line for the start-up log: how the admin signs in, never the secret."""
    user = admin_username()
    plain = _admin_password()
    if not user:
        return "Admin sign-in: PHARMASEARCH_ADMIN_USERNAME is not set, so nobody can sign in as admin."
    if plain:
        how = f"password set directly in PHARMASEARCH_ADMIN_PASSWORD, {len(plain)} characters"
        if len(plain) < MIN_ADMIN_PASSWORD:
            how += f" -- too short, at least {MIN_ADMIN_PASSWORD} are needed"
    elif _admin_password_hash():
        how = "password digest in PHARMASEARCH_ADMIN_PASSWORD_HASH"
    else:
        how = "no password set"
    return f"Admin sign-in: user name '{user}', {how}."


def _is_valid_email(email: str) -> bool:
    """Validate email format using RFC 5322 simplified pattern."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def email_confirmation_required() -> bool:
    """Whether a new account must confirm its address before it can sign in.

    Off unless PHARMASEARCH_REQUIRE_EMAIL_CONFIRMATION is set. Then sign-up
    is the usual kind: create an account and you are in. An account is a
    user, never an admin, so what it opens is the search and nothing more.
    Turn it on once mail is dependable, to know each address is real.
    """
    return (os.getenv("PHARMASEARCH_REQUIRE_EMAIL_CONFIRMATION") or "").strip().lower() in {"1", "true", "yes", "on"}


def _usable_account_sql() -> str:
    """The condition that makes an account able to sign in."""
    return "COALESCE(email_verified, 0) = 1" if email_confirmation_required() else "1 = 1"


ALREADY_REGISTERED = "This email already has an account. Sign in instead, or reset the password if it is forgotten."


def public_base_url() -> str:
    """The address the links in emails are built on.

    Set by the operator, or taken from the name the platform gives the
    service. Never read from the request: its Host header is written by
    whoever sends it, and a link built on that would deliver a working token
    to a server of their choosing.
    """
    configured = (os.getenv("PHARMASEARCH_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    host = (os.getenv("RENDER_EXTERNAL_HOSTNAME") or "").strip()
    return f"https://{host}" if host else "http://localhost:8000"


def smtp_is_configured() -> bool:
    host = (os.getenv("SMTP_HOST") or "").strip()
    return bool(host) and host != "localhost"


def sender_address() -> str:
    """FROM_EMAIL as the mail service needs it: just the address.

    A value pasted into a hosting dashboard arrives with a space, quote marks
    or a "Name <address>" form often enough that each made the mail service
    answer "valid sender email required" with nothing to say which.
    """
    value = (os.getenv("FROM_EMAIL") or "").strip().strip("\"'").strip()
    bracketed = re.search(r"<([^<>]+)>", value)
    if bracketed:
        value = bracketed.group(1).strip()
    return value


def sender_problem() -> str:
    """Why the sender address would be refused, or "" when it looks right."""
    value = sender_address()
    if not value:
        return "FROM_EMAIL is not set. Set it to the sender address verified with the mail service."
    if not _is_valid_email(value):
        return f"FROM_EMAIL is not a valid email address (it reads '{value}')."
    return ""


def email_is_configured() -> bool:
    return bool((os.getenv("BREVO_API_KEY") or "").strip()) or smtp_is_configured()


def _deliver(to: str, subject: str, text: str, html: str) -> bool:
    """Send one message; False when there is nothing to send it with.

    Two routes. An HTTPS mail API (Brevo) comes first, because some hosts --
    Render's free plan among them -- block the SMTP ports outright and an API
    call is ordinary web traffic. SMTP is the other, for anywhere that allows
    it. A failure raises, and the caller logs it.
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if sender_problem():
        raise RuntimeError(sender_problem())
    from_email = sender_address()
    api_key = (os.getenv("BREVO_API_KEY") or "").strip()
    if api_key:
        import requests

        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": api_key, "accept": "application/json"},
            json={
                "sender": {"email": from_email, "name": "PharmaSearch"},
                "to": [{"email": to}],
                "subject": subject,
                "textContent": text,
                "htmlContent": html,
            },
            timeout=20,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"mail API answered {response.status_code}: {response.text[:200]}")
        return True
    if not smtp_is_configured():
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    # A timeout, or a blocked port leaves the sign-up page waiting forever.
    with smtplib.SMTP(os.getenv("SMTP_HOST", ""), int(os.getenv("SMTP_PORT", "587")), timeout=20) as server:
        if smtp_user and smtp_password:
            server.starttls()
            server.login(smtp_user, smtp_password)
        server.send_message(msg)
    return True


def mail_status() -> dict[str, object]:
    """How mail would be sent, for the admin's email check. Never the key itself.

    The key's length and first characters are enough to tell a whole API key
    from an SMTP key or a shortened copy, which is what goes wrong in practice.
    """
    key = (os.getenv("BREVO_API_KEY") or "").strip()
    if key:
        route = "Brevo API (HTTPS)"
    elif smtp_is_configured():
        route = f"SMTP ({os.getenv('SMTP_HOST')}:{os.getenv('SMTP_PORT', '587')})"
    else:
        route = ""
    return {
        "route": route,
        "from_email": sender_address(),
        "sender_problem": sender_problem(),
        "base_url": public_base_url(),
        "key_set": bool(key),
        "key_prefix": key[:8],
        "key_length": len(key),
        "key_looks_whole": key.startswith("xkeysib-") and len(key) >= 80 and "*" not in key and " " not in key,
    }


def send_test_email(to: str) -> tuple[bool, str]:
    """Send one plain message and report what the mail service said."""
    to = to.strip()
    if not _is_valid_email(to):
        return False, "That is not a valid email address."
    if not email_is_configured():
        return False, "No mail route is configured: set BREVO_API_KEY and FROM_EMAIL, or the SMTP settings."
    text = "This is a test from PharmaSearch. If you are reading it, sign-up and reset emails will arrive too."
    try:
        _deliver(to, "PharmaSearch email test", text, f"<p>{text}</p>")
    except Exception as exc:  # the admin needs the provider's own words
        logger.error("Test email to %s failed: %s", to, exc)
        return False, str(exc)
    return True, f"The mail service accepted the message for {to}. Check the inbox and the spam folder."


def pending_users() -> list[dict[str, str]]:
    """Accounts that signed up and have not confirmed their address."""
    from repository import get_connection, initialize_database

    if not email_confirmation_required():
        return []

    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT username, created_at FROM admin_users"
            " WHERE is_active = 1 AND COALESCE(email_verified, 0) = 0 ORDER BY created_at DESC"
        ).fetchall()
    return [{"username": row[0], "created_at": str(row[1] or "")[:16].replace("T", " ")} for row in rows]


def activate_user(username: str) -> bool:
    """Confirm an account on the admin's say-so, when no email could reach it."""
    from repository import get_connection, initialize_database

    initialize_database()
    with get_connection() as conn:
        changed = conn.execute(
            "UPDATE admin_users SET email_verified = 1, verification_token = NULL"
            " WHERE username = ? AND is_active = 1 AND COALESCE(email_verified, 0) = 0",
            (username.strip().lower(),),
        ).rowcount
    return bool(changed)


EMAIL_NOT_SENT = (
    "Account created, but the confirmation email could not be sent. "
    "Ask the site administrator to activate your account."
)


def _generate_verification_token() -> str:
    """Generate a secure verification token for email confirmation."""
    return secrets.token_urlsafe(32)


def send_verification_email(email: str, verification_token: str, app_url: str | None = None) -> bool:
    """Send verification email with confirmation link.

    Returns True if sent successfully, False otherwise.
    """
    try:
        # Create verification link
        verification_url = f"{app_url or public_base_url()}/admin/verify-email?token={verification_token}"


        # Plain text version
        text = f"""Welcome to PharmaSearch!

Please confirm your email address by clicking the link below:

{verification_url}

This link will expire in 24 hours.

If you did not create this account, please ignore this email."""

        # HTML version
        html = f"""<html>
  <body>
    <h2>Welcome to PharmaSearch!</h2>
    <p>Please confirm your email address by clicking the button below:</p>
    <p><a href="{verification_url}" style="background-color: #4a90e2; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Confirm Email</a></p>
    <p>Or copy this link:<br>{verification_url}</p>
    <p><small>This link will expire in 24 hours.</small></p>
    <p><small>If you did not create this account, please ignore this email.</small></p>
  </body>
</html>"""

        if _deliver(email, "Confirm Your PharmaSearch Admin Account", text, html):
            logger.info(f"Verification email sent to {email}")
            return True
        # Written to the log so whoever runs the site can pass it on;
        # without a mail route there is no other way to activate anyone.
        logger.warning(
            "No mail route configured. Verification email not sent to %s. Activation link: %s",
            email, verification_url,
        )
        return False
    except Exception as e:
        logger.error("Failed to send verification email to %s: %s. Activation link: %s", email, e, verification_url)
        return False


def register_user(email: str, password: str, app_url: str | None = None) -> tuple[bool, str]:
    """Register an account and send its confirmation link.

    Returns (success, message). The answer is the same whether or not the
    address already has an account, so the form cannot be used to find out
    who is registered. An address that signed up but never confirmed gets a
    fresh link and the password just given: the earlier link stops working.
    """
    from datetime import timedelta

    from repository import get_connection, initialize_database

    email = email.strip()
    if not email:
        return False, "Email cannot be empty."
    if not _is_valid_email(email):
        return False, "Please enter a valid email address."
    if len(password) < 12:
        return False, "Password must be at least 12 characters."

    initialize_database()
    try:
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT id, COALESCE(email_verified, 0) FROM admin_users WHERE username = ?",
                (email.lower(),)
            ).fetchone()
            if not email_confirmation_required():
                if existing:
                    # Saying so is the price of signing in at once: answering
                    # "done" here would have to mean signing a stranger into
                    # someone else's account.
                    return False, ALREADY_REGISTERED
                conn.execute(
                    """INSERT INTO admin_users
                       (username, password_hash, created_at, is_active, email_verified, role)
                       VALUES (?, ?, ?, 1, 1, ?)""",
                    (email.lower(), hash_password(password), datetime.now(timezone.utc).isoformat(), ROLE_USER),
                )
                logger.info("New account registered: %s", email)
                return True, ""

            if existing and existing[1]:
                logger.info("Sign-up for an address that already has an account: %s", email)
                return True, ""

            verification_token = _generate_verification_token()
            verification_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            password_hash = hash_password(password)
            if existing:
                conn.execute(
                    "UPDATE admin_users SET password_hash = ?, verification_token = ?, verification_expires = ?"
                    " WHERE id = ?",
                    (password_hash, _token_digest(verification_token), verification_expires, existing[0]),
                )
            else:
                conn.execute(
                    """INSERT INTO admin_users
                       (username, password_hash, created_at, is_active, email_verified,
                        verification_token, verification_expires, role)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (email.lower(), password_hash, datetime.now(timezone.utc).isoformat(), 1, 0,
                     _token_digest(verification_token), verification_expires, ROLE_USER)
                )
            logger.info(f"New account registered: {email}")

            # Say so when the email did not go: a success message would
            # leave the person waiting for a link that was never sent.
            sent = send_verification_email(email, verification_token, app_url)
            return True, "" if sent else EMAIL_NOT_SENT
    except Exception as e:
        logger.error(f"Registration failed: {e}")
        return False, "Registration failed. Please try again."


def verify_email(token: str) -> tuple[bool, str]:
    """Verify email using the verification token.

    Returns (success, message).
    """
    from repository import get_connection, initialize_database

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                """SELECT id, verification_expires, username
                   FROM admin_users
                   WHERE verification_token = ? AND email_verified = 0""",
                (_token_digest(token),)
            ).fetchone()

            if not user:
                return False, "Invalid or expired verification link."

            # Check if token has expired
            if datetime.fromisoformat(user[1]) < datetime.now(timezone.utc):
                return False, "Verification link has expired. Please register again."

            # Mark email as verified
            conn.execute(
                "UPDATE admin_users SET email_verified = 1, verification_token = NULL WHERE id = ?",
                (user[0],)
            )
            logger.info(f"Email verified for user: {user[2]}")
            return True, "Email verified successfully! You can now sign in."
    except Exception as e:
        logger.error(f"Email verification failed: {e}")
        return False, "Verification failed. Please try again."


def authenticate_db_user(username: str, password: str) -> tuple[bool, str]:
    """Check credentials against the admin_users database.

    Returns (success, message). On success, message is empty.
    On failure, message explains why (email not verified, etc).
    """
    from repository import get_connection, initialize_database

    username = username.strip()
    if not username or not password:
        return False, ""

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                "SELECT password_hash, email_verified FROM admin_users WHERE username = ? AND is_active = 1",
                (username.lower(),)
            ).fetchone()
            if not user:
                return False, ""

            password_hash, email_verified = user
            password_ok = verify_password(password, password_hash)

            if not password_ok:
                return False, ""

            # Check if email is verified
            if not email_verified and email_confirmation_required():
                return False, "Please verify your email address before signing in."

            # Update last_login
            conn.execute(
                "UPDATE admin_users SET last_login = ? WHERE username = ?",
                (datetime.now(timezone.utc).isoformat(), username.lower())
            )
            return True, ""
    except Exception as e:
        logger.error(f"Database authentication failed: {e}")
        return False, ""


def user_exists(username: str) -> bool:
    """True for a registered account that is active and has confirmed its address."""
    return bool(_account_role(username))


ROLE_USER = "user"
ROLE_ADMIN = "admin"


def _account_role(username: str) -> str:
    from repository import get_connection, initialize_database

    initialize_database()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(role, 'user') FROM admin_users"
                f" WHERE username = ? AND is_active = 1 AND {_usable_account_sql()}",
                (username.strip().lower(),),
            ).fetchone()
    except Exception:
        return ""
    return (row[0] or ROLE_USER) if row else ""


def user_role(username: str | None) -> str:
    """The role of a signed-in name: admin, user, or empty for nobody.

    The admin named in the server's settings is always an admin. A registered
    account is a user until an admin promotes it: signing up, by itself,
    opens the search and nothing in the admin area.
    """
    name = (username or "").strip()
    if not name:
        return ""
    if name == admin_username():
        return ROLE_ADMIN
    return _account_role(name)


def is_admin(username: str | None) -> bool:
    return user_role(username) == ROLE_ADMIN


def list_accounts() -> list[dict[str, str]]:
    """Confirmed accounts and their roles, for the admin's accounts page."""
    from repository import get_connection, initialize_database

    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT username, COALESCE(role, 'user'), last_login FROM admin_users"
            f" WHERE is_active = 1 AND {_usable_account_sql()} ORDER BY username"
        ).fetchall()
    return [
        {"username": row[0], "role": row[1] or ROLE_USER, "last_login": str(row[2] or "")[:16].replace("T", " ")}
        for row in rows
    ]


def set_role(username: str, role: str) -> bool:
    """Promote or demote a confirmed account. False when there is no such account."""
    from repository import get_connection, initialize_database

    if role not in (ROLE_USER, ROLE_ADMIN):
        return False
    initialize_database()
    with get_connection() as conn:
        changed = conn.execute(
            f"UPDATE admin_users SET role = ? WHERE username = ? AND is_active = 1 AND {_usable_account_sql()}",
            (role, username.strip().lower()),
        ).rowcount
    return bool(changed)


# --- how often a form may be used -------------------------------------------
#
# Sign-up and reset both send mail to an address the visitor chooses, so
# without a limit either form is a way to flood a stranger's inbox, or to fill
# the accounts table.
SIGNUPS_PER_HOUR = 5
RESETS_PER_HOUR = 3
_form_uses: dict[str, list[float]] = {}
_form_uses_lock = threading.Lock()


def allow_form_use(kind: str, key: str, limit: int, window_seconds: int = 3600) -> bool:
    """Count one use of a form by this key; False once the limit is reached."""
    bucket = f"{kind}|{key.strip().lower()}"
    now = time.time()
    with _form_uses_lock:
        recent = [stamp for stamp in _form_uses.get(bucket, []) if now - stamp < window_seconds]
        if len(recent) >= limit:
            _form_uses[bucket] = recent
            return False
        recent.append(now)
        _form_uses[bucket] = recent
        return True


def _token_digest(token: str) -> str:
    """What is stored for an emailed link: its digest, never the link itself.

    A link is a temporary password. Kept as it is, anyone who can read the
    accounts table -- a backup, an export -- could confirm an address or reset
    a password. A digest can be checked against a link but not turned into one.
    """
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def request_password_reset(email: str, app_url: str | None = None) -> tuple[bool, str]:
    """Generate password reset token and send reset email.

    Returns (success, message).
    """
    from repository import get_connection, initialize_database

    email = email.strip()
    if not email:
        return False, "Email cannot be empty."
    if not _is_valid_email(email):
        return False, "Please enter a valid email address."

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                "SELECT id FROM admin_users WHERE username = ? AND is_active = 1",
                (email.lower(),)
            ).fetchone()
            if not user:
                return True, "If an account exists with this email, a password reset link has been sent."

            # Generate reset token
            reset_token = _generate_verification_token()
            reset_expires = (datetime.now(timezone.utc) + __import__('datetime').timedelta(hours=1)).isoformat()

            # Store reset token
            conn.execute(
                "UPDATE admin_users SET reset_token = ?, reset_expires = ? WHERE id = ?",
                (_token_digest(reset_token), reset_expires, user[0])
            )
            logger.info(f"Password reset requested for {email}")

            # Send reset email
            send_password_reset_email(email, reset_token, app_url)
            return True, "If an account exists with this email, a password reset link has been sent."
    except Exception as e:
        logger.error(f"Password reset request failed: {e}")
        return False, "Password reset request failed. Please try again."


def send_password_reset_email(email: str, reset_token: str, app_url: str | None = None) -> bool:
    """Send password reset email with link.

    Returns True if sent successfully, False otherwise.
    """
    try:
        reset_url = f"{app_url or public_base_url()}/admin/reset-password?token={reset_token}"

        text = f"""Password Reset Request

We received a request to reset the password for your PharmaSearch admin account.

Click the link below to reset your password:

{reset_url}

This link will expire in 1 hour.

If you did not request this password reset, please ignore this email."""

        html = f"""<html>
  <body>
    <h2>Password Reset Request</h2>
    <p>We received a request to reset the password for your PharmaSearch admin account.</p>
    <p><a href="{reset_url}" style="background-color: #4a90e2; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Reset Password</a></p>
    <p>Or copy this link:<br>{reset_url}</p>
    <p><small>This link will expire in 1 hour.</small></p>
    <p><small>If you did not request this password reset, please ignore this email.</small></p>
  </body>
</html>"""

        if _deliver(email, "Reset Your PharmaSearch Admin Password", text, html):
            logger.info(f"Password reset email sent to {email}")
            return True
        logger.warning(
            "No mail route configured. Password reset email not sent to %s. Reset link: %s",
            email, reset_url,
        )
        return False
    except Exception as e:
        logger.error("Failed to send password reset email to %s: %s. Reset link: %s", email, e, reset_url)
        return False


def reset_password(token: str, new_password: str) -> tuple[bool, str]:
    """Reset password using reset token.

    Returns (success, message).
    """
    from repository import get_connection, initialize_database

    new_password = new_password or ""
    if len(new_password) < 12:
        return False, "Password must be at least 12 characters."
    if not new_password:
        return False, "Password cannot be empty."

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                """SELECT id, reset_expires, username
                   FROM admin_users
                   WHERE reset_token = ? AND reset_token IS NOT NULL""",
                (_token_digest(token),)
            ).fetchone()

            if not user:
                return False, "Invalid or expired password reset link."

            # Check if token has expired
            if datetime.fromisoformat(user[1]) < datetime.now(timezone.utc):
                return False, "Password reset link has expired. Please request a new one."

            # Update password
            password_hash = hash_password(new_password)
            conn.execute(
                "UPDATE admin_users SET password_hash = ?, reset_token = NULL, reset_expires = NULL WHERE id = ?",
                (password_hash, user[0])
            )
            logger.info(f"Password reset for user: {user[2]}")
            return True, "Password reset successfully! You can now sign in with your new password."
    except Exception as e:
        logger.error(f"Password reset failed: {e}")
        return False, "Password reset failed. Please try again."
