from flask import Flask, render_template, request, redirect, url_for, send_file
import psycopg2
from datetime import datetime, timedelta, date
import pandas as pd
import io
from flask import session
from collections import defaultdict
import os





app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key")
# 🔌 Supabase PostgreSQL Connection
def get_db_connection():
    return psycopg2.connect(
        os.environ["DATABASE_URL"],
        sslmode="require"
    )



# 🔁 Helper: generate week → day → date mapping
def generate_week_dates(start_date, total_weeks=20):
    days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
    week_map = {}
    for w in range(1, total_weeks + 1):
        week_start = start_date + timedelta(weeks=w - 1)
        week_map[w] = {
            d: (week_start + timedelta(days=i)).strftime("%d/%m/%Y")
            for i, d in enumerate(days)
        }
    return week_map


# ================= HOME =================
@app.route('/')
def index():
    conn = get_db_connection()
    cur = conn.cursor()


    # ✅ include role
    cur.execute("""
        SELECT faculty_id, name, role
        FROM faculty
        ORDER BY name
    """)
    faculty = cur.fetchall()

    cur.execute("""
        SELECT section_id, section_name
        FROM sections
        ORDER BY section_name
    """)
    sections = cur.fetchall()

    return render_template(
        "index.html",
        faculty=faculty,
        sections=sections
    )



# ================= FACULTY FLOW =================
@app.route('/faculty-login', methods=['POST'])
def faculty_login():
    login_type = request.form['login_type']   # faculty | admin
    faculty_id = int(request.form['faculty_id'])
    password = request.form['password']
    section_id = request.form['section_id']

    conn = get_db_connection()
    cur = conn.cursor()

    # 🔐 Authenticate using plain password (TESTING ONLY)
    cur.execute("""
        SELECT faculty_id, role
        FROM faculty
        WHERE faculty_id = %s
          AND password = %s
    """, (faculty_id, password))

    row = cur.fetchone()

    if not row:
        return "Invalid credentials", 403

    logged_in_id, role = row

    # 🚫 Role misuse protection
    if login_type == 'faculty' and role != 'faculty':
        return "Use HOD/AHOD login", 403

    if login_type == 'admin' and role not in ('hod', 'ahod'):
        return "Unauthorized admin access", 403

    # 🔐 Create session
    session.clear()
    session['faculty_id'] = logged_in_id
    session['role'] = role
    session['section_id'] = section_id

    # 🔎 Check if faculty actually teaches this section
    cur.execute("""
    SELECT 1
    FROM class_schedule
    WHERE faculty_id=%s AND section_id=%s
""", (logged_in_id, section_id))

    teaches = cur.fetchone()

    if role == 'faculty' and not teaches:
      cur.close()
      conn.close()
      return "You are not assigned to this section.", 403

    # 🔀 Redirect properly
    if role in ('hod','ahod'):
      return redirect(url_for('admin_dashboard'))
    else:
      return redirect(url_for('faculty_dashboard'))



@app.route('/admin-dashboard')
def admin_dashboard():
    if 'faculty_id' not in session or session['role'] not in ('hod','ahod'):
        return "Access Denied", 403

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT name FROM faculty WHERE faculty_id=%s",
                (session['faculty_id'],))
    admin_name = cur.fetchone()[0]

    cur.execute("SELECT section_name FROM sections WHERE section_id=%s",
                (session['section_id'],))
    section_name = cur.fetchone()[0]

    return render_template(
        "admin_dashboard.html",
        admin_name=admin_name,
        faculty_id=session['faculty_id'],
        section_id=session['section_id'],
        section_name=section_name
    )
@app.route('/faculty-audit')
def faculty_audit():
    # 🔐 Access Control
    if 'faculty_id' not in session or session['role'] not in ('hod', 'ahod'):
        return "Access Denied", 403

    conn = get_db_connection()
    cur = conn.cursor()

    selected_date = request.args.get('date')
    rows = []
    no_class = False

    if selected_date:
        report_date = datetime.strptime(selected_date, "%Y-%m-%d").date()

        # 🔎 Check if ANY class scheduled that day
        cur.execute("""
            SELECT COUNT(*)
            FROM class_schedule
            WHERE day_of_week = %s
        """, (report_date.strftime("%A"),))

        class_count = cur.fetchone()[0]

        if class_count == 0:
            no_class = True
        else:
            # 🔍 Fetch audit records for selected date
            cur.execute("""
                SELECT
                    a.date,
                    f_marker.name AS marked_by,
                    f_marker.role AS marker_role,
                    f_class.name  AS class_faculty,
                    s.section_name
                FROM attendance a
                JOIN faculty f_marker 
                    ON f_marker.faculty_id = a.marked_by
                JOIN faculty f_class  
                    ON f_class.faculty_id = a.faculty_id
                JOIN sections s       
                    ON s.section_id = a.section_id
                WHERE a.date = %s
                GROUP BY a.date, f_marker.name, f_marker.role, 
                         f_class.name, s.section_name
                ORDER BY f_class.name
            """, (report_date,))

            rows = cur.fetchall()

    else:
        # 🔍 Default → Show latest records
        cur.execute("""
            SELECT
                a.date,
                f_marker.name AS marked_by,
                f_marker.role AS marker_role,
                f_class.name  AS class_faculty,
                s.section_name
            FROM attendance a
            JOIN faculty f_marker 
                ON f_marker.faculty_id = a.marked_by
            JOIN faculty f_class  
                ON f_class.faculty_id = a.faculty_id
            JOIN sections s       
                ON s.section_id = a.section_id
            GROUP BY a.date, f_marker.name, f_marker.role, 
                     f_class.name, s.section_name
            ORDER BY a.date DESC
            LIMIT 50
        """)

        rows = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "faculty_audit.html",
        rows=rows,
        selected_date=selected_date,
        no_class=no_class
    )

@app.route('/admin-attendance', methods=['GET'])
def admin_attendance():
    if 'faculty_id' not in session or session['role'] not in ('hod','ahod'):
        return "Access Denied", 403

    conn = get_db_connection()
    cur = conn.cursor()

    selected_faculty = request.args.get('faculty_id', type=int)
    selected_subject = request.args.get('subject')
    selected_date = request.args.get('date')

    # Load faculty dropdown
    cur.execute("""
        SELECT faculty_id, name
        FROM faculty
        WHERE role='faculty'
        ORDER BY name
    """)
    faculty_list = cur.fetchall()

    subjects = []
    report = []
    present_count = absent_count = None
    no_class = False
    not_marked = False

    if selected_faculty:
        cur.execute("""
            SELECT DISTINCT subject
            FROM class_schedule
            WHERE faculty_id=%s
            ORDER BY subject
        """, (selected_faculty,))
        subjects = [r[0] for r in cur.fetchall()]

    if selected_faculty and selected_subject and selected_date:

        report_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
        day_name = report_date.strftime("%A")  # Monday, Tuesday etc

        # 🔍 Check if class scheduled that day
        cur.execute("""
            SELECT 1
            FROM class_schedule
            WHERE faculty_id=%s
              AND subject=%s
              AND day_of_week=%s
            LIMIT 1
        """, (selected_faculty, selected_subject, day_name))

        class_exists = cur.fetchone()

        if not class_exists:
            no_class = True
        else:
            # 🔍 Fetch attendance
            cur.execute("""
                SELECT s.roll_no, a.status
                FROM attendance a
                JOIN students s ON s.student_id = a.student_id
                WHERE a.faculty_id=%s
                  AND a.date=%s
                ORDER BY s.roll_no
            """, (selected_faculty, report_date))

            rows = cur.fetchall()

            if not rows:
                not_marked = True
            else:
                report = [{"roll": r[0], "status": r[1]} for r in rows]

                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE status='Present'),
                        COUNT(*) FILTER (WHERE status='Absent')
                    FROM attendance
                    WHERE faculty_id=%s
                      AND date=%s
                """, (selected_faculty, report_date))

                present_count, absent_count = cur.fetchone()

    cur.close()
    conn.close()

    return render_template(
        "admin_readonly.html",
        faculty_list=faculty_list,
        subjects=subjects,
        report=report,
        selected_faculty=selected_faculty,
        selected_subject=selected_subject,
        selected_date=selected_date,
        present_count=present_count,
        absent_count=absent_count,
        no_class=no_class,
        not_marked=not_marked
    )




@app.route('/select', methods=['POST'])
def select():
    faculty_id = request.form['faculty_id']
    section_id = request.form['section_id']
    return redirect(url_for('attendance', faculty_id=faculty_id, section_id=section_id))


@app.route('/attendance/<faculty_id>/<section_id>')
def attendance(faculty_id, section_id):
    if 'faculty_id' not in session:
     return redirect(url_for('index'))

# Normal faculty restriction
    if session['role'] == 'faculty':
     if int(faculty_id) != session['faculty_id']:
        return "Access Denied", 403

    conn = get_db_connection()
    cur = conn.cursor()


    # Detect elective group (GT / DF)
    cur.execute("""
        SELECT DISTINCT group_id
        FROM class_schedule
        WHERE faculty_id=%s AND section_id=%s AND group_id IS NOT NULL
    """, (faculty_id, section_id))
    row = cur.fetchone()
    faculty_group_id = row[0] if row else None

    # Load students
    if faculty_group_id:
        cur.execute("""
            SELECT student_id, roll_no, name
            FROM students
            WHERE section_id=%s AND group_id=%s
            ORDER BY roll_no
        """, (section_id, faculty_group_id))
    else:
        cur.execute("""
            SELECT student_id, roll_no, name
            FROM students
            WHERE section_id=%s
            ORDER BY roll_no
        """, (section_id,))

    students = cur.fetchall()

    # Faculty class days
    cur.execute("""
        SELECT DISTINCT day_of_week
        FROM class_schedule
        WHERE faculty_id=%s AND section_id=%s
    """, (faculty_id, section_id))
    class_days = [r[0] for r in cur.fetchall()]

    semester_start = date(2026, 1, 19)
    week_dates = generate_week_dates(semester_start)

    return render_template(
        "attendance.html",
        students=students,
        faculty_id=faculty_id,
        section_id=section_id,
        class_days=class_days,
        week_dates=week_dates
    )


@app.route('/save', methods=['POST'])
def save():

    # 🔐 Admins cannot mark attendance
    if session.get('role') in ('hod', 'ahod'):
        return "Admins cannot mark attendance", 403

    conn = get_db_connection()
    cur = conn.cursor()

    faculty_id = int(request.form['faculty_id'])
    section_id = int(request.form['section_id'])
    week_id = int(request.form['week_id'])
    attendance_date = request.form['attendance_date']

    # Convert DD/MM/YYYY → date
    class_date = datetime.strptime(attendance_date, "%d/%m/%Y").date()

    # 🔎 Detect if faculty teaches elective group (GT/DF type)
    cur.execute("""
        SELECT DISTINCT group_id
        FROM class_schedule
        WHERE faculty_id=%s AND section_id=%s AND group_id IS NOT NULL
    """, (faculty_id, section_id))

    row = cur.fetchone()
    faculty_group_id = row[0] if row else None

    # 📚 Load correct students
    if faculty_group_id:
        # Only students of that group
        cur.execute("""
            SELECT student_id
            FROM students
            WHERE section_id=%s AND group_id=%s
        """, (section_id, faculty_group_id))
    else:
        # Whole section
        cur.execute("""
            SELECT student_id
            FROM students
            WHERE section_id=%s
        """, (section_id,))

    students = cur.fetchall()

    # 📝 Insert / Update attendance
    for (student_id,) in students:

        status = "Absent" if f"att_{student_id}" in request.form else "Present"

        cur.execute("""
            INSERT INTO attendance
            (student_id, faculty_id, section_id, week_id, date, status, marked_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (student_id, date, faculty_id, section_id)
            DO UPDATE SET
                status = EXCLUDED.status,
                marked_by = EXCLUDED.marked_by
        """, (
            student_id,
            faculty_id,
            section_id,
            week_id,
            class_date,
            status,
            session['faculty_id']   # Who actually marked
        ))

    conn.commit()
    cur.close()
    conn.close()

    # 🔁 Redirect to report and auto-show that date
    return redirect(url_for(
        'week_report',
        date=class_date.strftime("%Y-%m-%d")
    ))





# ================= WEEK REPORT ================= 
@app.route('/week-report')
def week_report():

    if 'faculty_id' not in session:
        return redirect(url_for('index'))

    conn = get_db_connection()
    cur = conn.cursor()

    selected_date = request.args.get('date')
    requested_faculty_id = request.args.get('faculty_id', type=int)

    if not selected_date:
        return render_template("week_report.html", report=None)

    report_date = datetime.strptime(selected_date, "%Y-%m-%d").date()

    # 🔐 Decide whose attendance to show

    if session['role'] == 'faculty':
        faculty_filter = session['faculty_id']

    elif session['role'] in ('hod', 'ahod'):

        # If HOD clicked "My Daily Report"
        if requested_faculty_id == session['faculty_id'] or not requested_faculty_id:
            faculty_filter = session['faculty_id']
        else:
            # If HOD is using admin filter to view someone else
            faculty_filter = requested_faculty_id

    else:
        return "Unauthorized", 403

    # 🔎 Fetch attendance
    query = """
        SELECT s.roll_no, s.name, a.status
        FROM attendance a
        JOIN students s ON s.student_id = a.student_id
        WHERE a.date = %s
          AND a.faculty_id = %s
        ORDER BY s.roll_no
    """

    cur.execute(query, (report_date, faculty_filter))
    rows = cur.fetchall()

    report = [
        {"roll": r[0], "name": r[1], "status": r[2]}
        for r in rows
    ]

    # ✅ Correct Count (filtered by faculty)
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE status='Present'),
            COUNT(*) FILTER (WHERE status='Absent')
        FROM attendance
        WHERE date = %s
          AND faculty_id = %s
    """, (report_date, faculty_filter))

    present_count, absent_count = cur.fetchone()

    cur.close()
    conn.close()

    return render_template(
        "week_report.html",
        report=report,
        selected_date=selected_date,
        present_count=present_count,
        absent_count=absent_count
    )








# ================= STUDENT REPORT =================
@app.route('/student-report')
def student_report():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT section_id, section_name FROM sections")
    sections = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("student_report.html", sections=sections)


@app.route('/get-students/<int:section_id>')
def get_students(section_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT student_id, roll_no, name
        FROM students
        WHERE section_id=%s
        ORDER BY roll_no
    """, (section_id,))
    students = cur.fetchall()

    return {
        "students": [
            {"id": s[0], "roll": s[1], "name": s[2]}
            for s in students
        ]
    }


@app.route('/get-subjects')
def get_subjects():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT subject FROM class_schedule ORDER BY subject")
    return {"subjects": [r[0] for r in cur.fetchall()]}

@app.route('/get-student-attendance/<int:student_id>')
def get_student_attendance(student_id):
    conn = get_db_connection()
    cur = conn.cursor()

    selected_date = request.args.get('date')

    if not selected_date:
        return {"attendance": []}

    report_date = datetime.strptime(selected_date, "%Y-%m-%d").date()

    query = """
        SELECT 
            cs.period_no,
            cs.subject,
            f.name AS faculty_name,
            a.status
        FROM attendance a
        JOIN class_schedule cs
          ON cs.section_id = a.section_id
         AND cs.faculty_id = a.faculty_id
         AND cs.day_of_week = TO_CHAR(a.date, 'FMDay')
        JOIN faculty f
          ON f.faculty_id = cs.faculty_id
        WHERE a.student_id = %s
          AND a.date = %s
        ORDER BY cs.period_no
    """

    cur.execute(query, (student_id, report_date))
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return {
        "attendance": [
            {
                "period": r[0],
                "subject": r[1],
                "faculty": r[2],
                "status": r[3]
            }
            for r in rows
        ]
    }



# ================= EXPORT =================
@app.route('/download-excel')
def download_excel():
    conn = get_db_connection()
    cur = conn.cursor()

    selected_date = request.args.get('date')
    class_date = datetime.strptime(selected_date, "%Y-%m-%d").date()

    cur.execute("""
        SELECT s.roll_no, s.name, a.status
        FROM attendance a
        JOIN students s ON s.student_id=a.student_id
        WHERE a.date=%s
        ORDER BY s.roll_no
    """, (class_date,))
    rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=["Roll No", "Name", "Status"])
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=f"Attendance_{selected_date}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
@app.route('/download-faculty-report')
def download_faculty_report():
    if 'faculty_id' not in session or session['role'] not in ('hod','ahod'):
        return "Access Denied", 403

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            f_marker.name AS marked_by,
            f_class.name  AS class_faculty,
            s.section_name,
            a.date
        FROM attendance a
        JOIN faculty f_marker ON f_marker.faculty_id = a.marked_by
        JOIN faculty f_class  ON f_class.faculty_id  = a.faculty_id
        JOIN sections s       ON s.section_id = a.section_id
        GROUP BY f_marker.name, f_class.name, s.section_name, a.date
        ORDER BY a.date DESC
    """)

    rows = cur.fetchall()

    df = pd.DataFrame(
        rows,
        columns=["Marked By", "Class Faculty", "Section", "Date"]
    )

    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="Faculty_Attendance_Audit.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
@app.route('/download-student-excel')
def download_student_excel():

    conn = get_db_connection()
    cur = conn.cursor()

    student_id = request.args.get('student_id', type=int)
    selected_date = request.args.get('date')

    if not student_id or not selected_date:
        return "Missing parameters", 400

    report_date = datetime.strptime(selected_date, "%Y-%m-%d").date()

    query = """
        SELECT 
            cs.period_no,
            cs.subject,
            f.name,
            a.status
        FROM attendance a
        JOIN class_schedule cs
          ON cs.section_id = a.section_id
         AND cs.faculty_id = a.faculty_id
         AND cs.day_of_week = TO_CHAR(a.date, 'FMDay')
        JOIN faculty f
          ON f.faculty_id = cs.faculty_id
        WHERE a.student_id = %s
          AND a.date = %s
        ORDER BY cs.period_no
    """

    cur.execute(query, (student_id, report_date))
    rows = cur.fetchall()

    df = pd.DataFrame(
        rows,
        columns=["Period", "Subject", "Faculty", "Status"]
    )

    # ✅ Add Date column
    df.insert(0, "Date", report_date.strftime("%d-%m-%Y"))

    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    cur.close()
    conn.close()

    return send_file(
        output,
        as_attachment=True,
        download_name=f"Student_Attendance_{selected_date}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )



@app.route('/faculty-dashboard')
def faculty_dashboard():
    if 'faculty_id' not in session or session['role'] != 'faculty':
        return "Access Denied", 403

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT name FROM faculty WHERE faculty_id=%s",
                (session['faculty_id'],))
    faculty_name = cur.fetchone()[0]

    cur.execute("SELECT section_name FROM sections WHERE section_id=%s",
                (session['section_id'],))
    section_name = cur.fetchone()[0]

    return render_template(
        "faculty_dashboard.html",
        faculty_name=faculty_name,
        faculty_id=session['faculty_id'],
        section_id=session['section_id'],
        section_name=section_name
    )
@app.route('/daily-summary')
def daily_summary():
    if 'faculty_id' not in session or session['role'] not in ('hod','ahod'):
        return "Access Denied", 403

    conn = get_db_connection()
    cur = conn.cursor()

    selected_date = request.args.get('date')

    summary = []

    if selected_date:
        report_date = datetime.strptime(selected_date, "%Y-%m-%d").date()

        cur.execute("""
            SELECT 
                f.name AS faculty_name,
                cs.subject,
                s.section_name,
                COUNT(*) FILTER (WHERE a.status='Present') AS present_count,
                COUNT(*) FILTER (WHERE a.status='Absent') AS absent_count
            FROM attendance a
            JOIN faculty f ON f.faculty_id = a.faculty_id
            JOIN sections s ON s.section_id = a.section_id
            JOIN class_schedule cs
              ON cs.faculty_id = a.faculty_id
             AND cs.section_id = a.section_id
             AND cs.day_of_week = TO_CHAR(a.date, 'FMDay')
            WHERE a.date = %s
            GROUP BY f.name, cs.subject, s.section_name
            ORDER BY s.section_name, f.name
        """, (report_date,))

        rows = cur.fetchall()

        summary = [
            {
                "faculty": r[0],
                "subject": r[1],
                "section": r[2],
                "present": r[3],
                "absent": r[4]
            }
            for r in rows
        ]

    cur.close()
    conn.close()

    return render_template(
        "daily_summary.html",
        summary=summary,
        selected_date=selected_date
    )

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)














