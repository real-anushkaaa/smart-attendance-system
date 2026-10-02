import csv
import io
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, make_response, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, log_audit_event
from .security import login_required, role_required

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
LOCKOUT_MAX_ATTEMPTS = 4
LOCKOUT_MINUTES = 15


def _password_requirements_error(password):
    if not password:
        return "Password is required."
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[A-Z]", password):
        return "Password must include at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must include at least one lowercase letter."
    if not re.search(r"\d", password):
        return "Password must include at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must include at least one special character."
    return None


def _get_login_attempt(username):
    db = get_db()
    normalized = (username or "").strip().lower()
    if not normalized:
        return None
    return db.execute(
        "SELECT username, failed_attempts, locked_until FROM login_attempts WHERE username = ?",
        (normalized,),
    ).fetchone()


def _reset_login_attempt(username):
    db = get_db()
    normalized = (username or "").strip().lower()
    if not normalized:
        return
    db.execute(
        "INSERT INTO login_attempts (username, failed_attempts, locked_until, updated_at) VALUES (?, 0, NULL, ?) ON CONFLICT(username) DO UPDATE SET failed_attempts = 0, locked_until = NULL, updated_at = excluded.updated_at",
        (normalized, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def _record_failed_login(username):
    db = get_db()
    normalized = (username or "").strip().lower()
    if not normalized:
        return False

    now = datetime.now(timezone.utc)
    record = _get_login_attempt(normalized)
    failed_attempts = int((record["failed_attempts"] if record else 0)) + 1

    if record and record["locked_until"]:
        locked_until = datetime.fromisoformat(record["locked_until"])
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if now < locked_until:
            return True

    if failed_attempts >= LOCKOUT_MAX_ATTEMPTS:
        lock_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
        db.execute(
            "INSERT INTO login_attempts (username, failed_attempts, locked_until, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET failed_attempts = excluded.failed_attempts, locked_until = excluded.locked_until, updated_at = excluded.updated_at",
            (normalized, failed_attempts, lock_until, now.isoformat()),
        )
        db.commit()
        return True

    db.execute(
        "INSERT INTO login_attempts (username, failed_attempts, locked_until, updated_at) VALUES (?, ?, NULL, ?) ON CONFLICT(username) DO UPDATE SET failed_attempts = excluded.failed_attempts, locked_until = NULL, updated_at = excluded.updated_at",
        (normalized, failed_attempts, now.isoformat()),
    )
    db.commit()
    return False


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    return _login_impl(allowed_roles={"teacher", "admin"}, template_name="login.html")


@auth_bp.route("/student-login", methods=["GET", "POST"])
def student_login():
    return _login_impl(allowed_roles={"student"}, template_name="student_login.html")


@auth_bp.route("/audit-export")
@login_required
@role_required("admin")
def audit_export():
    db = get_db()
    rows = db.execute(
        "SELECT action, user_id, entity_type, entity_id, details, created_at FROM audit_log ORDER BY id DESC LIMIT 500"
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["action", "user_id", "entity_type", "entity_id", "details", "created_at"])
    for row in rows:
        writer.writerow([
            row["action"],
            row["user_id"],
            row["entity_type"],
            row["entity_id"],
            row["details"],
            row["created_at"],
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=audit_log.csv"
    return response


def _login_impl(allowed_roles, template_name):
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        normalized_username = (username or "").strip().lower()

        db = get_db()
        attempt_record = _get_login_attempt(normalized_username)
        if attempt_record and attempt_record["locked_until"]:
            locked_until = datetime.fromisoformat(attempt_record["locked_until"])
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < locked_until:
                flash(f"This account is temporarily locked until {locked_until.strftime('%Y-%m-%d %H:%M')}. Please try again later.", "error")
                return render_template(template_name)

        user = db.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            locked = _record_failed_login(normalized_username)
            flash("Account temporarily locked for 15 minutes due to repeated failed attempts." if locked else "Invalid credentials", "error")
            if username:
                log_audit_event("login_failed", details=f"Failed login attempt for {username}")
            return render_template(template_name)

        if user["role"] not in allowed_roles:
            flash("Access denied for this login portal", "error")
            return render_template(template_name)

        _reset_login_attempt(normalized_username)
        session.clear()
        session["user_id"] = user["id"]
        session["name"] = user["name"]
        session["role"] = user["role"]
        session["student_id"] = user["student_id"]
        session["last_activity"] = datetime.now(timezone.utc).isoformat()
        log_audit_event("login", user_id=user["id"], details=f"{user['role']} login for {user['username']}")
        return redirect(url_for("attendance.dashboard"))

    return render_template(template_name)


@auth_bp.route("/logout")
def logout():
    user_id = session.get("user_id")
    if user_id:
        log_audit_event("logout", user_id=user_id, details="User logged out")
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/users", methods=["GET", "POST"])
@login_required
@role_required("admin")
def users():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "teacher").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if role not in {"teacher", "admin"}:
            flash("Invalid role selected", "error")
            return redirect(url_for("auth.users"))

        if not all([name, role, username, password]):
            flash("All fields are required", "error")
        else:
            password_issue = _password_requirements_error(password)
            if password_issue:
                flash(f"Password policy violation: {password_issue}", "error")
            else:
                existing = db.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone()
                if existing:
                    flash("Username already exists. Please choose a unique login name.", "error")
                else:
                    try:
                        cursor = db.execute(
                            "INSERT INTO users (name, role, username, password_hash) VALUES (?, ?, ?, ?)",
                            (name, role, username, generate_password_hash(password)),
                        )
                        db.commit()
                        log_audit_event("user_created", user_id=session.get("user_id"), details=f"Created {role} user {username}", entity_type="user", entity_id=cursor.lastrowid)
                        flash("User created", "success")
                    except Exception:
                        flash("Username already exists", "error")

    users_list = db.execute(
        """
        SELECT id, name, role, username, created_at
        FROM users
        WHERE role IN ('admin', 'teacher')
        ORDER BY id DESC
        """
    ).fetchall()
    return render_template("users.html", users=users_list)
