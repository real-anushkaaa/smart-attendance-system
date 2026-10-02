from pathlib import Path
import os
from datetime import datetime, timedelta, timezone

from flask import Flask, flash, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

from .attendance_routes import attendance_bp
from .auth import auth_bp
from .db import close_db, ensure_db_initialized, get_db
from .reports import reports_bp
from .students import students_bp
from .sync_routes import sync_bp


def create_app():
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )

    session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "change-me-in-production"),
        DB_PATH=str(Path(app.instance_path) / "attendance.db"),
        UPLOAD_DIR=str(Path(app.root_path).parent / "uploads" / "students"),
        REMOTE_SYNC_URL=os.getenv("REMOTE_SYNC_URL", ""),
        SCHOOL_CODE=os.getenv("SCHOOL_CODE", "RURAL-SCHOOL-001"),
        ENABLE_AI_MODULE=os.getenv("ENABLE_AI_MODULE", "true").lower() == "true",
        PUBLIC_BASE_URL=os.getenv("PUBLIC_BASE_URL", ""),
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=session_timeout_minutes),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
        SESSION_REFRESH_EACH_REQUEST=False,
    )

    @app.before_request
    def enforce_session_timeout():
        if request.endpoint in {"static", "auth.login", "auth.student_login", "auth.logout"}:
            if session.get("user_id") and request.endpoint == "auth.logout":
                return None
            return None

        if not session.get("user_id"):
            return None

        last_activity = session.get("last_activity")
        now = datetime.now(timezone.utc)
        timeout_seconds = int(app.config["PERMANENT_SESSION_LIFETIME"].total_seconds())

        if last_activity:
            try:
                last_seen = datetime.fromisoformat(last_activity)
            except ValueError:
                last_seen = now
            if (now - last_seen).total_seconds() > timeout_seconds:
                session.clear()
                flash("Your session expired due to inactivity. Please log in again.", "error")
                return redirect(url_for("auth.login"))

        session["last_activity"] = now.isoformat()

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(close_db)

    with app.app_context():
        ensure_db_initialized()
        _ensure_default_admin()

    app.register_blueprint(auth_bp)
    app.register_blueprint(students_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(sync_bp)

    @app.route("/")
    def index():
        if session.get("user_id"):
            return redirect(url_for("attendance.dashboard"))
        return redirect(url_for("auth.login"))

    return app


def _ensure_default_admin():
    db = get_db()
    existing = {
        row["username"]: row
        for row in db.execute("SELECT id, username, role FROM users WHERE username IN (?, ?)", ("admin", "teacher")).fetchall()
    }

    if "admin" not in existing:
        db.execute(
            "INSERT INTO users (name, role, username, password_hash) VALUES (?, ?, ?, ?)",
            ("System Admin", "admin", "admin", generate_password_hash("admin123")),
        )

    if "teacher" not in existing:
        db.execute(
            "INSERT INTO users (name, role, username, password_hash) VALUES (?, ?, ?, ?)",
            ("Teacher", "teacher", "teacher", generate_password_hash("teacher123")),
        )

    db.execute(
        "UPDATE users SET name = ? WHERE username = ? AND role = ?",
        ("Teacher", "teacher", "teacher"),
    )
    db.commit()
