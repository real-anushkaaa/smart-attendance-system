import io
import zipfile
from datetime import datetime, timedelta, timezone

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
    sort_by = request.args.get("sort", "attendance")
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = min(max(5, int(request.args.get("per_page", "10") or 10)), 50)
    student_id = session.get("student_id") if session.get("role") == "student" else None

    end_date_raw = (request.args.get("end_date") or datetime.now().date().isoformat())
    start_date_raw = (request.args.get("start_date") or (datetime.now().date().replace(day=1).isoformat()))

    try:
        start_date = datetime.strptime(start_date_raw, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_raw, "%Y-%m-%d").date()
    except ValueError:
        end_date = datetime.now().date()
        if period == "weekly":
            start_date = end_date - timedelta(days=6)
        else:
            start_date = end_date.replace(day=1)

    if end_date < start_date:
        start_date, end_date = end_date, start_date

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
    if start_date and end_date:
        rows = [row for row in rows if int(row["marked_sessions"] or 0) > 0]

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

    top_absent = sorted(all_report_rows, key=lambda row: (row["absent_sessions"], -row["attendance_percent"]), reverse=True)[:5]
    consistent_present = sorted(
        all_report_rows,
        key=lambda row: (row["present_sessions"], -row["absent_sessions"], row["attendance_percent"]),
        reverse=True,
    )[:5]
    anomalies = [
        row for row in all_report_rows if row["no_of_classes"] and row["attendance_percent"] < 60
    ]

    trend_points = []
    day_cursor = start_date
    while day_cursor <= end_date:
        day_label = day_cursor.strftime("%Y-%m-%d")
        day_total = db.execute(
            """
            SELECT COUNT(a.id) AS total, SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END) AS present
            FROM attendance a
            JOIN students s ON s.id = a.student_id
            WHERE a.date = ? AND s.active = 1 {where_section} {where_student} {where_subject}
            """.format(where_section=where_section, where_student=where_student, where_subject=where_subject),
            tuple([day_label] + ([section_name] if section_name else []) + ([student_id] if student_id else []) + ([subject] if subject else [])),
        ).fetchone()
        total = int((day_total["total"] or 0)) if day_total else 0
        present = int((day_total["present"] or 0)) if day_total else 0
        rate = round((present / total) * 100, 1) if total else 0.0
        trend_points.append(rate)
        day_cursor += timedelta(days=1)

    if len(trend_points) >= 2:
        x_vals = list(range(len(trend_points)))
        avg_x = sum(x_vals) / len(x_vals)
        avg_y = sum(trend_points) / len(trend_points)
        numerator = sum((x - avg_x) * (y - avg_y) for x, y in zip(x_vals, trend_points))
        denominator = sum((x - avg_x) ** 2 for x in x_vals)
        slope = (numerator / denominator) if denominator else 0
        predicted_next_rate = round(max(0.0, min(100.0, trend_points[-1] + (slope * 7))), 1)
        if predicted_next_rate > trend_points[-1] + 5:
            direction = "up"
            direction_label = "Rising"
        elif predicted_next_rate < trend_points[-1] - 5:
            direction = "down"
            direction_label = "Falling"
        else:
            direction = "steady"
            direction_label = "Stable"
    else:
        predicted_next_rate = round(avg_percent, 1)
        direction = "steady"
        direction_label = "Stable"

    forecast_summary = {
        "next_rate": predicted_next_rate,
        "direction": direction,
        "direction_label": direction_label,
    }

    if sort_by == "attendance":
        display_rows = sorted(display_rows, key=lambda row: row["attendance_percent"], reverse=True)
    elif sort_by == "name":
        display_rows = sorted(display_rows, key=lambda row: row["name"].lower())
    elif sort_by == "section":
        display_rows = sorted(display_rows, key=lambda row: row["section_name"].lower())

    total_rows = len(display_rows)
    total_pages = max(1, (total_rows + per_page - 1) // per_page) if total_rows else 1
    page = min(page, total_pages)
    start_index = (page - 1) * per_page
    paginated_rows = display_rows[start_index:start_index + per_page]

    return render_template(
        "reports.html",
        rows=paginated_rows,
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
        sort_by=sort_by,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_rows=total_rows,
        top_absent=top_absent,
        consistent_present=consistent_present,
        anomalies=anomalies,
        forecast_summary=forecast_summary,
    )


@reports_bp.route("/export/csv")
@login_required
def export_csv():
    _apply_auto_absences()
    rows = _current_report_rows(request.args.get("subject", ""), session.get("student_id") if session.get("role") == "student" else None, request.args.get("start_date"), request.args.get("end_date"))
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


def _xlsx_bytes_from_rows(rows, headers):
    buffer = io.BytesIO()

    try:
        from openpyxl import Workbook
    except ImportError:
        workbook = None
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Attendance Report"
        sheet.append(headers)
        for row in rows:
            sheet.append([
                row.get("student_code", ""),
                row.get("name", ""),
                row.get("section_name", ""),
                row.get("no_of_classes", 0),
                row.get("present_sessions", 0),
                row.get("absent_sessions", 0),
                row.get("attendance_percent", 0),
            ])
        workbook.save(buffer)
        buffer.seek(0)
        return buffer

    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

    def xml_cell(value, row_num, col_num):
        text = str(value)
        return f'<c r="{chr(65 + col_num)}{row_num + 1}" t="inlineStr"><is><t>{text}</t></is></c>'

    row_values = [headers] + [
        [
            row.get("student_code", ""),
            row.get("name", ""),
            row.get("section_name", ""),
            row.get("no_of_classes", 0),
            row.get("present_sessions", 0),
            row.get("absent_sessions", 0),
            row.get("attendance_percent", 0),
        ]
        for row in rows
    ]

    xml_rows = []
    for row_index, values in enumerate(row_values, start=1):
        cells = ''.join(xml_cell(value, row_index, idx) for idx, value in enumerate(values))
        xml_rows.append(f'<row r="{row_index}">{cells}</row>')
    sheet_xml = ''.join(xml_rows)

    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Attendance Report" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    app_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Smart Attendance</Application></Properties>'''
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    core_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>Smart Attendance System</dc:creator><cp:lastModifiedBy>Smart Attendance System</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified></cp:coreProperties>'''
    worksheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{sheet_xml}</sheetData></worksheet>'''

    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml)

    buffer.seek(0)
    return buffer


@reports_bp.route("/export/xlsx")
@login_required
def export_xlsx():
    _apply_auto_absences()
    rows = _current_report_rows(request.args.get("subject", ""), session.get("student_id") if session.get("role") == "student" else None, request.args.get("start_date"), request.args.get("end_date"))
    headers = [
        "student_code",
        "name",
        "section_name",
        "no_of_classes",
        "present_sessions",
        "absent_sessions",
        "attendance_percent",
    ]
    buffer = _xlsx_bytes_from_rows(rows, headers)
    return send_file(buffer, as_attachment=True, download_name="attendance_report.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@reports_bp.route("/export/pdf")
@login_required
def export_pdf():
    _apply_auto_absences()
    if not PDF_AVAILABLE:
        return Response("PDF export dependency missing. Install reportlab.", status=503)

    rows = _current_report_rows(request.args.get("subject", ""), session.get("student_id") if session.get("role") == "student" else None, request.args.get("start_date"), request.args.get("end_date"))
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


def _current_report_rows(subject="", student_id=None, start_date=None, end_date=None):
    db = get_db()
    where_subject = ""
    where_student = ""
    date_filter = ""
    params = []
    if subject:
        where_subject = "AND a.subject = ?"
        params.append(subject)
    if student_id:
        where_student = "AND s.id = ?"
        params.append(student_id)

    if start_date and end_date:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")
            date_filter = "AND a.date BETWEEN ? AND ?"
            params.extend([start_date, end_date])
        except ValueError:
            pass

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
        LEFT JOIN attendance a ON a.student_id = s.id AND a.date BETWEEN ? AND ?
        WHERE s.active = 1 {where_subject} {where_student} {date_filter}
        GROUP BY s.id
        ORDER BY s.section_name, s.name
        """,
        tuple([start_date or "2000-01-01", end_date or "2100-12-31"] + params),
    ).fetchall()
    rows = [row for row in rows if int(row["marked_sessions"] or 0) > 0]

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
