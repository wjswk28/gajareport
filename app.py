import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_from_directory, flash, jsonify
)

app = Flask(__name__)
app.secret_key = "gaja_yonsei_secret_key"

# -------------------------------
# 업로드 폴더 설정
# -------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 좌측 사이드바에 항상 노출할 부서(관리자 제외)
DEPT_LIST = ["외래", "병동", "수술실", "상담실"]

# 각 부서별 폴더 자동 생성
for dept in ["관리자", *DEPT_LIST]:
    os.makedirs(os.path.join(UPLOAD_FOLDER, dept), exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# KST (표시용)
KST = datetime.utcnow() + timedelta(hours=9)

# =========================
# DB
# =========================
def get_db():
    db_path = os.path.join(BASE_DIR, "reports.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_id INTEGER,
            title TEXT,
            date TEXT,
            department TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER,
            category TEXT,
            content TEXT,
            FOREIGN KEY(report_id) REFERENCES reports(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER,
            department TEXT,
            filename TEXT
        )
    """)
    conn.commit()
    conn.close()

# =========================
# Auth
# =========================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

USERS = {
    "gajakjh":   {"password": "1234", "department": "관리자"},   # ✅ 관리자 PW 1234로 통일
    "gajaopd":   {"password": "1234", "department": "외래"},
    "gajaward":  {"password": "1234", "department": "병동"},
    "gajaor":    {"password": "1234", "department": "수술실"},
    "gajacoordi":{"password": "1234", "department": "상담실"},   # ✅ 새로 추가
}

# =========================
# Routes
# =========================
@app.route("/")
def home_redirect():
    return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = USERS.get(username)
        if user and user["password"] == password:
            session["user"] = {"username": username, "department": user["department"]}
            return redirect("/list")
        return render_template("login.html", error="아이디 또는 비밀번호가 잘못되었습니다.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")

# =========================
# 보고서 작성
# =========================
@app.route("/create", methods=["GET", "POST"])
@login_required
def create_report():
    user = session["user"]
    dept = user["department"]

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        date_input = request.form.get("date", "").strip()
        categories = request.form.getlist("category[]")
        contents   = request.form.getlist("content[]")

        # ✅ DB 연결과 커서 생성
        conn = get_db()
        cur = conn.cursor()

        # ✅ 부서 정보
        dept = session["user"]["department"]

        # ✅ 부서별 local_id 계산
        last_local = cur.execute(
            "SELECT MAX(local_id) FROM reports WHERE department = ?",
            (dept,)
        ).fetchone()[0]
        next_local_id = (last_local or 0) + 1

        # ✅ 사용자가 입력한 날짜가 있으면 그대로, 없으면 오늘 날짜 사용
        if date_input:
            created_at = f"{date_input} 00:00:00"
        else:
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ✅ 보고서 본문 저장
        cur.execute(
            "INSERT INTO reports (local_id, title, date, department, created_at) VALUES (?, ?, ?, ?, ?)",
            (next_local_id, title, date_input, dept, created_at)
        )

        report_id = cur.lastrowid

        # ✅ 카테고리/내용 저장
        for cat, cont in zip(categories, contents):
            if cont and cont.strip():
                cur.execute(
                    "INSERT INTO report_contents (report_id, category, content) VALUES (?, ?, ?)",
                    (report_id, cat, cont)
                )

        # ✅ 첨부파일 저장
        files = request.files.getlist("files")
        if files:
            dept_path = os.path.join(UPLOAD_FOLDER, dept)
            os.makedirs(dept_path, exist_ok=True)
            for file in files:
                if file and file.filename:
                    safe_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
                    save_path = os.path.join(dept_path, safe_name)
                    file.save(save_path)
                    cur.execute(
                        "INSERT INTO report_files (report_id, department, filename) VALUES (?, ?, ?)",
                        (report_id, dept, safe_name)
                    )

        # ✅ 커밋 및 종료
        conn.commit()
        conn.close()
        return redirect("/list")


    today = datetime.now().date().isoformat()
    return render_template("form.html", today=today, user=user)

# =========================
# 보고서 목록 (필터/검색/첨부)
# =========================
@app.route("/list")
@login_required
def report_list():
    user = session["user"]
    dept = user["department"]
    selected_dept = request.args.get("dept")

    # 🔹 날짜 필터 기본값 (오늘 ~ 2주 전)
    today = datetime.now().date()
    two_weeks_ago = today - timedelta(days=14)

    start_date = request.args.get("start_date", two_weeks_ago.isoformat())
    end_date = request.args.get("end_date", today.isoformat())

    sql = "SELECT * FROM reports WHERE date BETWEEN ? AND ?"
    params = [start_date, end_date]

    # 🔹 검색 파라미터
    search_filter = request.args.get("filter", "title_content")
    search_query = (request.args.get("search") or "").strip()

    conn = get_db()

    # 🔹 기본 쿼리 (날짜 조건 추가)
    base_sql = "SELECT * FROM reports WHERE date BETWEEN ? AND ?"
    base_params = [start_date, end_date]

    # 부서 조건
    if dept != "관리자":
        base_sql += " AND department = ?"
        base_params.append(dept)
    elif selected_dept:
        base_sql += " AND department = ?"
        base_params.append(selected_dept)

    base_sql += " ORDER BY id DESC"
    rows = conn.execute(base_sql, tuple(base_params)).fetchall()

    reports_filtered = list(rows)
    match_map = {}  # ✅ 카테고리 검색 결과 저장용

    # 2️⃣ 검색 기능
    if search_query:
        q = f"%{search_query}%"

        if search_filter == "category":
            ids = [r["id"] for r in reports_filtered]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                sql = f"""
                    SELECT rc.report_id, rc.category, rc.content
                    FROM report_contents rc
                    WHERE rc.report_id IN ({placeholders})
                      AND LOWER(rc.category) LIKE LOWER(?)
                """
                cat_rows = conn.execute(sql, (*ids, q)).fetchall()

                # ✅ report_id별 매칭 카테고리+내용 저장
                for cr in cat_rows:
                    match_map.setdefault(cr["report_id"], []).append({
                        "category": cr["category"],
                        "content": cr["content"]
                    })

                reports_filtered = [
                    r for r in reports_filtered if r["id"] in match_map
                ]
            else:
                reports_filtered = []

        else:
            # ✅ 제목 + 내용 검색
            ids = [r["id"] for r in reports_filtered]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                sql = f"""
                    SELECT r.*
                    FROM reports r
                    WHERE r.id IN ({placeholders})
                      AND (
                        LOWER(r.title) LIKE LOWER(?)
                        OR EXISTS (
                          SELECT 1 FROM report_contents c
                          WHERE c.report_id = r.id
                            AND LOWER(c.content) LIKE LOWER(?)
                        )
                      )
                    ORDER BY r.id DESC
                """
                reports_filtered = conn.execute(sql, (*ids, q, q)).fetchall()
            else:
                reports_filtered = []

    # 3️⃣ 첨부파일 + 카테고리 매칭 정보 추가
    enriched = []
    for r in reports_filtered:
        item = dict(r)
        item["match_details"] = match_map.get(r["id"], [])  # ✅ 카테고리+내용 매칭 저장

        # 첨부파일 여부
        files = conn.execute(
            "SELECT filename, department FROM report_files WHERE report_id = ?",
            (r["id"],)
        ).fetchall()
        item["has_files"] = len(files) > 0
        item["files"] = [f["filename"] for f in files]  # ✅ 리스트 페이지 팝업용
        item["file"] = (
            {"filename": files[0]["filename"], "department": files[0]["department"]}
            if files else None
        )
        enriched.append(item)

    # 4️⃣ 관리자용 부서 목록 — DB 의존 X, 항상 표시
    if dept == "관리자":
        conn.close()
        return render_template(
            "list.html",
            reports=enriched,
            user=user,
            departments=DEPT_LIST,           # ✅ 고정 목록 사용
            selected_dept=selected_dept,
            search_filter=search_filter,
            search_query=search_query
        )

    conn.close()
    return render_template(
        "list.html",
        reports=enriched,
        user=user,
        search_filter=search_filter,
        search_query=search_query
    )

# =========================
# 보고서 상세보기
# =========================
@app.route("/view/<int:report_id>")
@login_required
def view_report(report_id):
    conn = get_db()
    report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    contents = conn.execute("SELECT * FROM report_contents WHERE report_id = ?", (report_id,)).fetchall()
    files = conn.execute("SELECT * FROM report_files WHERE report_id = ?", (report_id,)).fetchall()
    conn.close()
    return render_template("view.html", report=report, contents=contents, files=files)

# =========================
# 첨부파일 목록(JSON) — 리스트 모달
# =========================
@app.route("/files/<int:report_id>")
@login_required
def get_files(report_id):
    conn = get_db()
    files = conn.execute(
        "SELECT filename, department FROM report_files WHERE report_id = ?",
        (report_id,)
    ).fetchall()
    conn.close()
    file_list = [
        {
            "filename": f["filename"],
            "department": f["department"],
            "url": url_for("uploaded_file", department=f["department"], filename=f["filename"])
        }
        for f in files
    ]
    return jsonify(file_list)

# =========================
# 보고서 삭제 (첨부 보존 + 로그)
# =========================
@app.route("/delete/<int:report_id>", methods=["POST"])
@login_required
def delete_report(report_id):
    conn = get_db()
    cur = conn.cursor()
    report = cur.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    files = cur.execute("SELECT * FROM report_files WHERE report_id = ?", (report_id,)).fetchall()

    if not report:
        conn.close()
        return "❌ 존재하지 않는 보고서입니다.", 404

    cur.execute("DELETE FROM report_files WHERE report_id = ?", (report_id,))
    cur.execute("DELETE FROM report_contents WHERE report_id = ?", (report_id,))
    cur.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    conn.commit()
    conn.close()

    log_path = os.path.join(BASE_DIR, "delete_log.txt")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n" + "="*80 + "\n")
        f.write(f"[삭제일시] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"[삭제자]   {session['user']['department']} ({session['user']['username']})\n")
        f.write(f"[보고서ID] {report_id}\n")
        f.write(f"[제목]     {report['title']}\n")
        f.write(f"[부서]     {report['department']}\n")
        f.write(f"[작성일]   {report['created_at']}\n")
        if files:
            f.write("[첨부파일 목록]\n")
            for fdata in files:
                f.write(f"  ┗ {fdata['department']}/{fdata['filename']}\n")
        else:
            f.write("[첨부파일] 없음\n")
        f.write("="*80 + "\n")

    return redirect("/list")

# =========================
# 파일 다운로드/미리보기
# =========================
@app.route("/uploads/<department>/<filename>")
def uploaded_file(department, filename):
    upload_path = os.path.join(UPLOAD_FOLDER, department)
    return send_from_directory(upload_path, filename)

# =========================
# 디버그
# =========================
@app.route("/debug_db")
def debug_db():
    conn = get_db()
    reports = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 5").fetchall()
    contents = conn.execute("SELECT * FROM report_contents ORDER BY id DESC LIMIT 5").fetchall()
    conn.close()
    return f"""
    <h2>📋 최근 보고서</h2>
    <pre>{[dict(r) for r in reports]}</pre>
    <h2>📝 최근 내용(report_contents)</h2>
    <pre>{[dict(c) for c in contents]}</pre>
    """

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
