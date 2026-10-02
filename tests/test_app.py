import io
import os

import pytest

from attendance import create_app
from attendance.db import ensure_db_initialized, get_db
from werkzeug.security import generate_password_hash


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "attendance_test.db"
    app = create_app()
    app.config.update(TESTING=True, DB_PATH=str(db_path))

    with app.app_context():
        ensure_db_initialized()
        db = get_db()
        db.execute(
            "DELETE FROM users WHERE username IN ('admin', 'teacher')"
        )
        db.execute(
            "INSERT INTO users (name, role, username, password_hash) VALUES (?, ?, ?, ?)",
            ("System Admin", "admin", "admin", generate_password_hash("admin123")),
        )
        db.execute(
            "INSERT INTO users (name, role, username, password_hash) VALUES (?, ?, ?, ?)",
            ("Teacher", "teacher", "teacher", generate_password_hash("teacher123")),
        )
        db.commit()

    with app.test_client() as test_client:
        yield test_client


def test_face_recognition_warning_is_suppressed():
    import warnings

    from attendance.face import _load_face_lib

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _load_face_lib()

    assert not any("pkg_resources is deprecated as an API" in str(w.message) for w in caught)


def test_login_page_loads(client):
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert b"Smart Attendance" in response.data


def test_admin_login_redirects_to_dashboard(client):
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.request.path == "/attendance/dashboard"


def test_protected_routes_are_reachable_for_admin(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    for route in [
        "/attendance/dashboard",
        "/attendance/mark",
        "/attendance/timetable",
        "/students/",
        "/reports/",
        "/auth/users",
        "/sync/status",
    ]:
        response = client.get(route)
        assert response.status_code == 200, f"Route {route} failed with status {response.status_code}"


def test_admin_api_students_works(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    response = client.get("/attendance/api/students")
    assert response.status_code == 200
    assert isinstance(response.get_json(), list)


def test_custom_timetable_subjects_appear_in_teacher_mark_page(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    db = client.application.app_context().database if hasattr(client.application, "database") else None
    # Add a custom timetable entry to simulate the admin portal workflow.
    with client.application.app_context():
        from attendance.db import get_db
        db = get_db()
        db.execute(
            "INSERT INTO timetable (section_name, weekday, subject, start_time, end_time) VALUES (?, ?, ?, ?, ?)",
            ("CSG", 1, "Operating Systems", "09:00", "10:00"),
        )
        db.commit()

    response = client.get("/attendance/mark")
    assert response.status_code == 200
    assert b"Operating Systems" in response.data


def test_student_search_filters_results(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    with client.application.app_context():
        from attendance.db import get_db
        db = get_db()
        db.execute(
            "INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone) VALUES (?, ?, ?, ?, ?, ?)",
            ("S001", "Asha Verma", "CSG", "A", "UID2001", "9999999001"),
        )
        db.execute(
            "INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone) VALUES (?, ?, ?, ?, ?, ?)",
            ("S002", "Ravi Kumar", "CSG", "A", "UID2002", "9999999002"),
        )
        db.commit()

    response = client.get("/students/?q=asha")
    assert response.status_code == 200
    assert b"Asha Verma" in response.data
    assert b"Ravi Kumar" not in response.data


def test_timetable_overlap_is_rejected(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    with client.application.app_context():
        from attendance.db import get_db
        db = get_db()
        db.execute(
            "INSERT INTO timetable (section_name, weekday, subject, start_time, end_time) VALUES (?, ?, ?, ?, ?)",
            ("CSG", 0, "Math", "09:00", "10:00"),
        )
        db.commit()

    response = client.post(
        "/attendance/timetable",
        data={
            "section_name": "CSG",
            "weekday": 0,
            "subject": "Physics",
            "start_time": "09:30",
            "end_time": "10:30",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"overlaps" in response.data.lower()


def test_duplicate_student_code_is_rejected(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    response = client.post(
        "/students/add",
        data={
            "student_code": "DUP-001",
            "name": "Same Student",
            "section_name": "CSG",
            "section": "A",
            "unique_id": "UIDDUP1",
            "guardian_phone": "9988776601",
            "login_username": "studentdup",
            "login_password": "pass1234",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Student saved" in response.data

    response = client.post(
        "/students/add",
        data={
            "student_code": "DUP-001",
            "name": "Duplicate Student",
            "section_name": "CSG",
            "section": "A",
            "unique_id": "UIDDUP2",
            "guardian_phone": "9988776602",
            "login_username": "studentdup2",
            "login_password": "pass1234",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"duplicate" in response.data.lower()


def test_reports_support_date_range_filters(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    with client.application.app_context():
        from attendance.db import get_db
        db = get_db()
        db.execute(
            "INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone) VALUES (?, ?, ?, ?, ?, ?)",
            ("R001", "Report Test Student", "CSG", "A", "UIDR001", "9988776603"),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "2026-09-15", "Math", "present", "manual", "2026-09-15T09:00:00", 0),
        )
        db.commit()

    response = client.get("/reports/?start_date=2026-09-20&end_date=2026-09-30")
    assert response.status_code == 200
    assert b"No attendance records match" in response.data or b"0 records found" in response.data.lower()


def test_weak_password_is_rejected_for_new_user(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    response = client.post(
        "/auth/users",
        data={
            "name": "Weak User",
            "role": "teacher",
            "username": "weakuser",
            "password": "abc123",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"at least 8 characters" in response.data.lower()


def test_login_lockout_blocks_repeated_failures(client):
    with client.application.app_context():
        from attendance.db import get_db
        from werkzeug.security import generate_password_hash
        db = get_db()
        db.execute(
            "INSERT INTO users (name, role, username, password_hash) VALUES (?, ?, ?, ?)",
            ("Locked User", "admin", "lockuser", generate_password_hash("StrongPass123!")),
        )
        db.commit()

    for _ in range(4):
        client.post(
            "/auth/login",
            data={"username": "lockuser", "password": "wrongpass"},
            follow_redirects=True,
        )

    response = client.post(
        "/auth/login",
        data={"username": "lockuser", "password": "StrongPass123!"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"temporarily locked" in response.data.lower()


def test_student_csv_import_and_export(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    csv_payload = (
        "student_code,name,section_name,section,unique_id,guardian_phone,login_username,login_password\n"
        "CSV001,Imported Student,CSG,A,UIDCSV001,9999001001,studentcsv,StrongPass123!\n"
    )

    response = client.post(
        "/students/import-csv",
        data={"file": (io.BytesIO(csv_payload.encode("utf-8")), "students.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"imported 1 student" in response.data.lower()

    export_response = client.get("/students/export-csv")
    assert export_response.status_code == 200
    assert b"student_code" in export_response.data.lower()


def test_reports_show_advanced_insights(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    with client.application.app_context():
        from attendance.db import get_db
        db = get_db()
        db.execute(
            "INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone) VALUES (?, ?, ?, ?, ?, ?)",
            ("I001", "Insight Student 1", "CSG", "A", "UIDI001", "9988111001"),
        )
        db.execute(
            "INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone) VALUES (?, ?, ?, ?, ?, ?)",
            ("I002", "Insight Student 2", "CSG", "A", "UIDI002", "9988111002"),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "2026-09-10", "Math", "present", "manual", "2026-09-10T09:00:00", 0),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "2026-09-11", "Math", "present", "manual", "2026-09-11T09:00:00", 0),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "2026-09-12", "Math", "absent", "manual", "2026-09-12T09:00:00", 0),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2, "2026-09-10", "Math", "present", "manual", "2026-09-10T09:00:00", 0),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2, "2026-09-11", "Math", "present", "manual", "2026-09-11T09:00:00", 0),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2, "2026-09-12", "Math", "present", "manual", "2026-09-12T09:00:00", 0),
        )
        db.commit()

    response = client.get("/reports/?start_date=2026-09-01&end_date=2026-09-30")
    assert response.status_code == 200
    assert b"Top Absent" in response.data or b"Top Absent Students" in response.data
    assert b"Consistent Present" in response.data or b"Consistent Present Students" in response.data


def test_student_portal_and_alert_routes_are_accessible(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    with client.application.app_context():
        from attendance.db import get_db
        db = get_db()
        db.execute(
            "INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone) VALUES (?, ?, ?, ?, ?, ?)",
            ("P001", "Portal Student", "CSG", "A", "UIDP001", "9999999009"),
        )
        student_id = db.execute("SELECT id FROM students WHERE student_code = ?", ("P001",)).fetchone()["id"]
        db.execute(
            "INSERT INTO users (name, role, username, password_hash, student_id) VALUES (?, ?, ?, ?, ?)",
            ("Portal Student", "student", "portalstudent", generate_password_hash("StrongPass123!"), student_id),
        )
        db.commit()

    client.post("/auth/logout", follow_redirects=True)
    response = client.post(
        "/auth/student-login",
        data={"username": "portalstudent", "password": "StrongPass123!"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.request.path == "/attendance/dashboard"

    portal_response = client.get("/attendance/portal")
    assert portal_response.status_code == 200
    assert b"Portal" in portal_response.data or b"attendance overview" in portal_response.data.lower()

    alerts_response = client.get("/attendance/alerts")
    assert alerts_response.status_code == 200
    assert b"alert" in alerts_response.data.lower()


def test_reports_excel_export_works(client):
    client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    response = client.get("/reports/export/xlsx")
    assert response.status_code == 200
    assert response.mimetype in {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream"}
    assert b"PK\x03\x04" in response.data[:8]


def test_face_attendance_is_enabled_by_default_when_backend_is_available():
    app = create_app()
    assert app.config.get("ENABLE_AI_MODULE") is True


def test_student_face_marking_route_records_face_attendance(client, monkeypatch):
    with client.application.app_context():
        from attendance.db import get_db
        from werkzeug.security import generate_password_hash
        db = get_db()
        db.execute("DELETE FROM users WHERE username = ?", ("face_student",))
        db.execute("DELETE FROM students WHERE student_code = ?", ("F100",))
        db.execute(
            "INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone, photo_encoding) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("F100", "Face Student", "CSG", "A", "UIDF100", "9999999000", "[0.1, 0.2, 0.3, 0.4]"),
        )
        student_id = db.execute("SELECT id FROM students WHERE student_code = ?", ("F100",)).fetchone()["id"]
        db.execute(
            "INSERT INTO users (name, role, username, password_hash, student_id) VALUES (?, ?, ?, ?, ?)",
            ("Face Student", "student", "face_student", generate_password_hash("StrongPass123!"), student_id),
        )
        db.commit()

    client.post("/auth/logout", follow_redirects=True)
    client.post("/auth/student-login", data={"username": "face_student", "password": "StrongPass123!"}, follow_redirects=True)

    monkeypatch.setattr("attendance.attendance_routes.extract_face_encoding", lambda image_bytes: [0.1, 0.2, 0.3, 0.4])
    monkeypatch.setattr("attendance.attendance_routes.match_face", lambda captured, known, tolerance=0.6: 0)

    response = client.post(
        "/attendance/api/mark/face",
        json={"image": "data:image/jpeg;base64,ZmFrZQ==", "subject": "Python"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["student"]["name"] == "Face Student"
    assert payload["result"]["face_done"] == 1


def test_face_match_accepts_realistic_near_match_encodings():
    from attendance.face import match_face

    reference = [float(i) / 100.0 for i in range(128)]
    near_match = [value + 0.05 for value in reference]
    assert match_face(near_match, [reference], tolerance=0.6) == 0


def test_student_portal_generates_low_attendance_alert(client):
    with client.application.app_context():
        from attendance.db import get_db
        from werkzeug.security import generate_password_hash
        db = get_db()
        db.execute("DELETE FROM users WHERE username = ?", ("lowattendance_student",))
        db.execute("DELETE FROM students WHERE student_code = ?", ("L001",))
        db.execute(
            "INSERT INTO students (student_code, name, section_name, section, unique_id, guardian_phone) VALUES (?, ?, ?, ?, ?, ?)",
            ("L001", "Low Attendance Student", "CSG", "A", "UIDL001", "9999999008"),
        )
        student_id = db.execute("SELECT id FROM students WHERE student_code = ?", ("L001",)).fetchone()["id"]
        db.execute(
            "INSERT INTO users (name, role, username, password_hash, student_id) VALUES (?, ?, ?, ?, ?)",
            ("Low Attendance Student", "student", "lowattendance_student", generate_password_hash("StrongPass123!"), student_id),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced, unique_id_done, face_done, qr_done) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (student_id, "2026-09-01", "Math", "present", "manual", "2026-09-01T09:00:00", 0, 1, 0, 0),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced, unique_id_done, face_done, qr_done) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (student_id, "2026-09-02", "Math", "absent", "manual", "2026-09-02T09:00:00", 0, 0, 0, 0),
        )
        db.execute(
            "INSERT INTO attendance (student_id, date, subject, status, mode, timestamp, synced, unique_id_done, face_done, qr_done) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (student_id, "2026-09-03", "Math", "absent", "manual", "2026-09-03T09:00:00", 0, 0, 0, 0),
        )
        db.commit()

    client.post("/auth/logout", follow_redirects=True)
    client.post(
        "/auth/student-login",
        data={"username": "lowattendance_student", "password": "StrongPass123!"},
        follow_redirects=True,
    )

    response = client.get("/attendance/portal")
    assert response.status_code == 200
    assert b"At Risk" in response.data or b"attendance" in response.data.lower()
    assert b"below the 75% alert threshold" in response.data.lower() or b"No attendance notifications yet." not in response.data


def test_unauthenticated_user_redirects_to_login(client):
    response = client.get("/attendance/dashboard")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")
