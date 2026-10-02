import csv
import io
import math
from pathlib import Path

from flask import Blueprint, current_app, flash, make_response, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from .db import get_db, log_audit_event
from .face import extract_face_encoding
from .security import login_required, role_required
from .utils import to_json

students_bp = Blueprint("students", __name__, url_prefix="/students")


@students_bp.route("/")
@login_required
@role_required("admin", "teacher")
def list_students():
    db = get_db()
    section_filter = request.args.get("section", "").strip()
    search_query = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "name")
    page = max(1, int(request.args.get("page", "1") or 1))
    page_size = min(max(5, int(request.args.get("per_page", "10") or 10)), 50)

    query = "SELECT * FROM students WHERE active = 1"
    params = []

    if section_filter:
        query += " AND section_name = ?"
        params.append(section_filter)

    if search_query:
        like_query = f"%{search_query}%"
        query += (
            " AND (LOWER(student_code) LIKE LOWER(?) OR LOWER(name) LIKE LOWER(?) OR LOWER(unique_id) LIKE LOWER(?) "
            "OR LOWER(guardian_phone) LIKE LOWER(?) OR LOWER(section_name) LIKE LOWER(?))"
        )
        params.extend([like_query, like_query, like_query, like_query, like_query])

    sort_map = {
        "name": "ORDER BY name ASC, student_code ASC",
        "code": "ORDER BY student_code ASC, name ASC",
        "section": "ORDER BY section_name ASC, name ASC",
        "updated": "ORDER BY updated_at DESC, name ASC",
    }
    query += " " + sort_map.get(sort_by, sort_map["name"])
    rows = db.execute(query, params).fetchall()
    total_rows = len(rows)
    total_pages = max(1, math.ceil(total_rows / page_size)) if total_rows else 1
    page = min(page, total_pages)
    start_index = (page - 1) * page_size
    page_rows = rows[start_index:start_index + page_size]

    sections = db.execute("SELECT DISTINCT section_name FROM students WHERE active = 1 ORDER BY section_name").fetchall()
    return render_template(
        "students.html",
        students=page_rows,
        section_filter=section_filter,
        search_query=search_query,
        sections=[row["section_name"] for row in sections],
        sort_by=sort_by,
        page=page,
        per_page=page_size,
        total_pages=total_pages,
        total_rows=total_rows,
    )


@students_bp.route("/import")
@login_required
@role_required("admin")
def import_students_page():
    return render_template("student_import.html")


@students_bp.route("/import-csv", methods=["POST"])
@login_required
@role_required("admin")
def import_students_csv():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please upload a CSV file.", "error")
        return redirect(url_for("students.import_students_page"))

    if not file.filename.lower().endswith(".csv"):
        flash("Only CSV files are supported.", "error")
        return redirect(url_for("students.import_students_page"))

    stream = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(stream))
    if not reader.fieldnames or not {"student_code", "name", "section_name"}.issubset(set(reader.fieldnames)):
        flash("CSV must include student_code, name, and section_name columns.", "error")
        return redirect(url_for("students.import_students_page"))

    imported = 0
    for row in reader:
        student_code = (row.get("student_code") or "").strip()
        name = (row.get("name") or "").strip()
        section_name = (row.get("section_name") or "").strip()
        if not student_code or not name or not section_name:
            continue

        db = get_db()
        if db.execute("SELECT id FROM students WHERE LOWER(student_code) = LOWER(?)", (student_code,)).fetchone():
            continue

        cursor = db.execute(
            """
            INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                student_code,
                name,
                section_name,
                (row.get("section") or "").strip() or section_name,
                (row.get("unique_id") or "").strip() or None,
                (row.get("guardian_phone") or "").strip() or None,
            ),
        )
        student_id = cursor.lastrowid
        login_username = (row.get("login_username") or "").strip()
        login_password = (row.get("login_password") or "").strip()
        if login_username:
            try:
                _upsert_student_user(db, student_id, name, login_username, login_password or "Student@123")
            except ValueError:
                pass
        db.commit()
        imported += 1

    label = "student" if imported == 1 else "students"
    flash(f"Imported {imported} {label}.", "success")
    return redirect(url_for("students.list_students"))


@students_bp.route("/export-csv")
@login_required
@role_required("admin", "teacher")
def export_students_csv():
    db = get_db()
    rows = db.execute(
        "SELECT student_code, name, section_name, section, unique_id, guardian_phone, active, created_at FROM students WHERE active = 1 ORDER BY section_name, name"
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["student_code", "name", "section_name", "section", "unique_id", "guardian_phone", "active", "created_at"])
    for row in rows:
        writer.writerow([
            row["student_code"],
            row["name"],
            row["section_name"],
            row["section"],
            row["unique_id"],
            row["guardian_phone"],
            row["active"],
            row["created_at"],
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=students_export.csv"
    return response


@students_bp.route("/add", methods=["GET", "POST"])
@login_required
@role_required("admin")
def add_student():
    if request.method == "POST":
        return _save_student()
    return render_template("student_form.html", student=None)


@students_bp.route("/<int:student_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def edit_student(student_id):
    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        flash("Student not found", "error")
        return redirect(url_for("students.list_students"))

    if request.method == "POST":
        return _save_student(student_id)

    student_user = db.execute(
        "SELECT id, username FROM users WHERE role = 'student' AND student_id = ?",
        (student_id,),
    ).fetchone()
    return render_template("student_form.html", student=student, student_user=student_user)


@students_bp.route("/<int:student_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_student(student_id):
    db = get_db()
    student = db.execute("SELECT student_code, name FROM students WHERE id = ?", (student_id,)).fetchone()
    db.execute("UPDATE students SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (student_id,))
    db.commit()
    log_audit_event("student_archived", user_id=session.get("user_id"), details=f"Archived student {student['student_code'] if student else student_id}", entity_type="student", entity_id=student_id)
    flash("Student archived", "success")
    return redirect(url_for("students.list_students"))


def _save_student(student_id=None):
    db = get_db()

    student_code = request.form.get("student_code", "").strip()
    name = request.form.get("name", "").strip()
    section_name = request.form.get("section_name", "").strip()
    section = request.form.get("section", "").strip()
    unique_id = request.form.get("unique_id", "").strip().upper()
    guardian_phone = request.form.get("guardian_phone", "").strip()
    login_username = request.form.get("login_username", "").strip()
    login_password = request.form.get("login_password", "")

    if not all([student_code, name, section_name]):
        flash("Student code, name and section are required", "error")
        return redirect(request.url)

    if student_id:
        existing_code = db.execute("SELECT id FROM students WHERE LOWER(student_code) = LOWER(?) AND id != ?", (student_code, student_id)).fetchone()
    else:
        existing_code = db.execute("SELECT id FROM students WHERE LOWER(student_code) = LOWER(?)", (student_code,)).fetchone()
    if existing_code:
        flash("Duplicate student code detected. Please use a unique roll number.", "error")
        return redirect(request.url)

    if unique_id:
        existing_unique = db.execute("SELECT id FROM students WHERE LOWER(unique_id) = LOWER(?) AND id != ?", (unique_id, student_id or -1)).fetchone()
        if existing_unique:
            flash("Duplicate Unique ID detected. Please use a unique student identity value.", "error")
            return redirect(request.url)

    if login_username:
        existing_login = db.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?) AND (student_id != ? OR ? IS NULL)", (login_username, student_id, student_id)).fetchone()
        if existing_login:
            flash("Duplicate student login username detected. Please choose another username.", "error")
            return redirect(request.url)

    photo = request.files.get("photo")
    photo_path = None
    photo_encoding = None

    if photo and photo.filename:
        safe_name = secure_filename(f"{student_code}_{photo.filename}")
        upload_path = Path(current_app.config["UPLOAD_DIR"]) / safe_name
        photo.save(upload_path)
        photo_path = str(upload_path)
        photo_encoding = extract_face_encoding(upload_path.read_bytes())
        if photo_encoding is None:
            flash("No face detected in the uploaded photo. Please upload a clear front-facing face image.", "error")
            return redirect(request.url)

    try:
        if student_id:
            existing = db.execute("SELECT photo_path, photo_encoding FROM students WHERE id = ?", (student_id,)).fetchone()
            db.execute(
                """
                UPDATE students
                SET student_code = ?, name = ?, section_name = ?, section = ?,
                    photo_path = ?, photo_encoding = ?, unique_id = ?, guardian_phone = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    student_code,
                    name,
                    section_name,
                    section,
                    photo_path or existing["photo_path"],
                    to_json(photo_encoding) if photo_encoding else existing["photo_encoding"],
                    unique_id or None,
                    guardian_phone or None,
                    student_id,
                ),
            )
            log_audit_event("student_updated", user_id=session.get("user_id"), details=f"Updated student {student_code}", entity_type="student", entity_id=student_id)
        else:
            cursor = db.execute(
                """
                INSERT INTO students
                (student_code, name, section_name, section, photo_path, photo_encoding, unique_id, guardian_phone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student_code,
                    name,
                    section_name,
                    section,
                    photo_path,
                    to_json(photo_encoding) if photo_encoding else None,
                    unique_id or None,
                    guardian_phone or None,
                ),
            )
            student_id = cursor.lastrowid
            log_audit_event("student_created", user_id=session.get("user_id"), details=f"Created student {student_code}", entity_type="student", entity_id=student_id)

        if login_username:
            _upsert_student_user(db, student_id, name, login_username, login_password)
        db.commit()
        flash("Student saved", "success")
    except Exception as exc:
        if isinstance(exc, ValueError):
            flash(str(exc), "error")
        else:
            flash("Unable to save student. Check duplicate student code, Unique ID, or username.", "error")

    return redirect(url_for("students.list_students"))


def _upsert_student_user(db, student_id, name, username, password):
    existing = db.execute(
        "SELECT id, password_hash FROM users WHERE role = 'student' AND student_id = ?",
        (student_id,),
    ).fetchone()

    if existing:
        password_hash = existing["password_hash"]
        if password:
            password_hash = generate_password_hash(password)
        db.execute(
            "UPDATE users SET name = ?, username = ?, password_hash = ? WHERE id = ?",
            (name, username, password_hash, existing["id"]),
        )
    else:
        if not password:
            raise ValueError("Password required when creating student login")
        db.execute(
            "INSERT INTO users (name, role, username, password_hash, student_id) VALUES (?, 'student', ?, ?, ?)",
            (name, username, generate_password_hash(password), student_id),
        )

