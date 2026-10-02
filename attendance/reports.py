import io
from datetime import datetime, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, send_file, session, url_for

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    PDF_AVAILABLE = True
except Exception:
    PDF_AVAILABLE = False

from .db import get_db
from .security import login_required, role_required
from .utils import rows_to_csv
from .attendance_routes import _apply_auto_absences

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


@reports_bp.route("/clear-attendance", methods=["POST"])
@login_required
@role_required("admin")
def clear_attendance_data():
    db = get_db()
    attendance_deleted = db.execute("DELETE FROM attendance").rowcount or 0
    sync_deleted = db.execute("DELETE FROM sync_queue WHERE entity_type = 'attendance'").rowcount or 0
    db.commit()
    flash(f"Cleared attendance data: {attendance_deleted} attendance rows and {sync_deleted} sync rows.", "success")
    return redirect(url_for("reports.reports_home"))


@reports_bp.route("/")
@login_required
def reports_home():
    _apply_auto_absences()
    db = get_db()
    period = request.args.get("period", "monthly")
    section_name = request.args.get("section", "")
    subject = request.args.get("subject", "")
    defaulters_only = request.args.get("defaulters_only") == "1"
    student_id = session.get("student_id") if session.get("role") == "student" else None

    end_date = datetime.now().date()
    if period == "weekly":
        start_date = end_date - timedelta(days=6)
    else:
        start_date = end_date.replace(day=1)

    where_section = ""
    where_subject = ""
    where_student = ""
    params = [str(start_date), str(end_date)]
    if section_name:
        where_section = "AND s.section_name = ?"
        params.append(section_name)
    if subject:
        where_subject = "AND a.subject = ?"
        params.append(subject)
    if student_id:
        where_student = "AND s.id = ?"
        params.append(student_id)

    rows = db.execute(
        f"""
        SELECT
            s.student_code,
            s.name,
            s.section_name,
            COUNT(a.id) as marked_sessions,
            SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END) as present_sessions,
            SUM(CASE WHEN a.status = 'late' THEN 1 ELSE 0 END) as late_sessions
        FROM students s
        LEFT JOIN attendance a ON a.student_id = s.id AND a.date BETWEEN ? AND ? {where_subject}
        WHERE s.active = 1 {where_section} {where_student}
        GROUP BY s.id
        ORDER BY s.section_name, s.name
        """,
        tuple(params),
    ).fetchall()

    all_report_rows = []
    total_marked_sessions = 0
    total_present_sessions = 0
    total_absent_sessions = 0
    defaulters_count = 0

    for r in rows:
        no_of_classes = int(r["marked_sessions"] or 0)
        present_sessions = int(r["present_sessions"] or 0)
        absent_sessions = no_of_classes - present_sessions
        percent = round((present_sessions / no_of_classes) * 100, 1) if no_of_classes else 0.0
        is_defaulter = percent < 75.0 if no_of_classes > 0 else False
        if is_defaulter:
            defaulters_count += 1

        total_marked_sessions += no_of_classes
        total_present_sessions += present_sessions
        total_absent_sessions += absent_sessions

        all_report_rows.append(
            {
                "student_code": r["student_code"],
                "name": r["name"],
                "section_name": r["section_name"],
                "no_of_classes": no_of_classes,
                "present_sessions": present_sessions,
                "absent_sessions": absent_sessions,
                "attendance_percent": percent,
                "is_defaulter": is_defaulter,
            }
        )

    # Filter for table display if defaulters_only requested
    display_rows = [r for r in all_report_rows if r["is_defaulter"]] if defaulters_only else all_report_rows

    avg_percent = round((total_present_sessions / total_marked_sessions) * 100, 1) if total_marked_sessions else 0.0

    # 1. Subject-wise chart data
    subject_params = [str(start_date), str(end_date)]
    subject_where_sec = ""
    subject_where_stu = ""
    if section_name:
        subject_where_sec = "AND s.section_name = ?"
        subject_params.append(section_name)
    if student_id:
        subject_where_stu = "AND s.id = ?"
        subject_params.append(student_id)

    subject_stats = db.execute(
        f"""
        SELECT
            a.subject,
            SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END) as present_count,
            SUM(CASE WHEN a.status != 'present' THEN 1 ELSE 0 END) as absent_count
        FROM attendance a
        JOIN students s ON s.id = a.student_id
        WHERE s.active = 1 AND a.date BETWEEN ? AND ? {subject_where_sec} {subject_where_stu}
        GROUP BY a.subject
        ORDER BY a.subject
        """,
        tuple(subject_params),
    ).fetchall()

    subject_chart_data = {
        "labels": [s["subject"] for s in subject_stats if s["subject"]],
        "present": [int(s["present_count"] or 0) for s in subject_stats if s["subject"]],
        "absent": [int(s["absent_count"] or 0) for s in subject_stats if s["subject"]],
    }

    # 2. Daily trend chart data
    trend_params = [str(start_date), str(end_date)]
    trend_where_sec = ""
    trend_where_sub = ""
    trend_where_stu = ""
    if section_name:
        trend_where_sec = "AND s.section_name = ?"
        trend_params.append(section_name)
    if subject:
        trend_where_sub = "AND a.subject = ?"
        trend_params.append(subject)
    if student_id:
        trend_where_stu = "AND s.id = ?"
        trend_params.append(student_id)

    trend_stats = db.execute(
        f"""
        SELECT
            a.date,
            SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END) as present_count,
            SUM(CASE WHEN a.status != 'present' THEN 1 ELSE 0 END) as absent_count
        FROM attendance a
        JOIN students s ON s.id = a.student_id
        WHERE s.active = 1 AND a.date BETWEEN ? AND ? {trend_where_sec} {trend_where_sub} {trend_where_stu}
        GROUP BY a.date
        ORDER BY a.date
        """,
        tuple(trend_params),
    ).fetchall()

    trend_chart_data = {
        "labels": [t["date"] for t in trend_stats],
        "present": [int(t["present_count"] or 0) for t in trend_stats],
        "absent": [int(t["absent_count"] or 0) for t in trend_stats],
    }

    sections = db.execute("SELECT DISTINCT section_name FROM students WHERE active = 1 ORDER BY section_name").fetchall()
    subjects = db.execute("SELECT DISTINCT subject FROM attendance ORDER BY subject").fetchall()

    metrics = {
        "total_students": len(all_report_rows),
        "total_sessions": total_marked_sessions,
        "total_present": total_present_sessions,
        "total_absent": total_absent_sessions,
        "avg_percent": avg_percent,
        "defaulters_count": defaulters_count,
    }

    return render_template(
        "reports.html",
        rows=display_rows,
        metrics=metrics,
        subject_chart_data=subject_chart_data,
        trend_chart_data=trend_chart_data,
        period=period,
        section_name=section_name,
        subject=subject,
        defaulters_only=defaulters_only,
        student_mode=bool(student_id),
        sections=[c["section_name"] for c in sections],
        subjects=[s["subject"] for s in subjects if s["subject"]],
        start_date=start_date,
        end_date=end_date,
    )


@reports_bp.route("/export/csv")
@login_required
def export_csv():
    _apply_auto_absences()
    rows = _current_report_rows(request.args.get("subject", ""), session.get("student_id") if session.get("role") == "student" else None)
    headers = [
        "student_code",
        "name",
        "section_name",
        "no_of_classes",
        "present_sessions",
        "absent_sessions",
        "attendance_percent",
    ]
    data = rows_to_csv(rows, headers)
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=attendance_report.csv"},
    )


@reports_bp.route("/export/pdf")
@login_required
def export_pdf():
    _apply_auto_absences()
    if not PDF_AVAILABLE:
        return Response("PDF export dependency missing. Install reportlab.", status=503)

    rows = _current_report_rows(request.args.get("subject", ""), session.get("student_id") if session.get("role") == "student" else None)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(40, height - 40, "Smart Attendance Report")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(40, height - 58, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    y = height - 85
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(40, y, "Code")
    pdf.drawString(100, y, "Name")
    pdf.drawString(230, y, "Class")
    pdf.drawString(280, y, "Present")
    pdf.drawString(330, y, "Absent")
    pdf.drawString(380, y, "No. Classes")
    pdf.drawString(455, y, "Percent")
    y -= 14

    pdf.setFont("Helvetica", 9)
    for row in rows:
        if y < 40:
            pdf.showPage()
            y = height - 40
            pdf.setFont("Helvetica", 9)
        pdf.drawString(40, y, str(row["student_code"]))
        pdf.drawString(100, y, str(row["name"])[:22])
        pdf.drawString(230, y, str(row["section_name"]))
        pdf.drawString(280, y, str(row["present_sessions"]))
        pdf.drawString(330, y, str(row["absent_sessions"]))
        pdf.drawString(380, y, str(row["no_of_classes"]))
        pdf.drawString(455, y, f"{row['attendance_percent']}%")
        y -= 12

    pdf.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="attendance_report.pdf", mimetype="application/pdf")


def _current_report_rows(subject="", student_id=None):
    db = get_db()
    where_subject = ""
    where_student = ""
    params = []
    if subject:
        where_subject = "AND a.subject = ?"
        params.append(subject)
    if student_id:
        where_student = "AND s.id = ?"
        params.append(student_id)

    rows = db.execute(
        f"""
        SELECT
            s.student_code,
            s.name,
            s.section_name,
            COUNT(a.id) as marked_sessions,
            SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END) as present_sessions,
            SUM(CASE WHEN a.status = 'late' THEN 1 ELSE 0 END) as late_sessions
        FROM students s
        LEFT JOIN attendance a ON a.student_id = s.id
        WHERE s.active = 1 {where_subject} {where_student}
        GROUP BY s.id
        ORDER BY s.section_name, s.name
        """
        ,
        tuple(params),
    ).fetchall()

    report_rows = []
    for r in rows:
        no_of_classes = int(r["marked_sessions"] or 0)
        present_sessions = int(r["present_sessions"] or 0)
        absent_sessions = no_of_classes - present_sessions
        percent = round((present_sessions / no_of_classes) * 100, 2) if no_of_classes else 0.0
        report_rows.append(
            {
                "student_code": r["student_code"],
                "name": r["name"],
                "section_name": r["section_name"],
                "no_of_classes": no_of_classes,
                "present_sessions": present_sessions,
                "absent_sessions": absent_sessions,
                "attendance_percent": percent,
            }
        )
    return report_rows
