import os
import re
import sqlite3
import mimetypes
import urllib.parse
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for, make_response,
    session, send_file, send_from_directory, flash, jsonify, Response
)
app = Flask(__name__, template_folder="templates")
app.secret_key = "gaja_yonsei_secret_key"

# -------------------------------
# 업로드 폴더 및 DB 경로 설정 (Persistent Disk)
# -------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Render Persistent Disk 경로
DATA_DIR = "/var/data"
DB_PATH = os.path.join(DATA_DIR, "reports.db")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")

# 폴더 생성 (없으면 자동 생성)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 부서별 하위 폴더도 자동 생성
DEPT_LIST = ["외래", "병동", "수술실", "상담실"]
for dept in ["관리자", *DEPT_LIST]:
    os.makedirs(os.path.join(UPLOAD_FOLDER, dept), exist_ok=True)

# Flask 설정 등록
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# =========================
# 🔹 파일명 정제 함수 (한글·특수문자 허용 버전)
# =========================
def clean_filename(filename: str) -> str:
    """
    Render/Windows/Linux 모두 안전한 ASCII 파일명 생성
    - 한글, 공백, 특수문자 → 언더바(_) 또는 영어로 변환
    - 서버 저장용으로만 사용, DB에는 original_name 그대로 저장
    """
    return secure_filename(filename)
# =========================
# DB 연결 및 초기화
# =========================
def get_db():
    """DB 연결 및 자동 생성"""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH):
        print("⚙️ reports.db not found. Creating new persistent database...")
        init_db()
        print("✅ reports.db created successfully at /var/data")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = sqlite3.connect(DB_PATH)
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
            filename TEXT,
            original_name TEXT
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
    "gajakjh":   {"password": "1234", "department": "관리자"},
    "gajaopd":   {"password": "1234", "department": "외래"},
    "gajaward":  {"password": "1234", "department": "병동"},
    "gajaor":    {"password": "1234", "department": "수술실"},
    "gajacoordi":{"password": "1234", "department": "상담실"},
}

# =========================
# 로그인 / 로그아웃
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
        title = request.form.get("title", "").strip() or "일일보고서"
        date_input = request.form.get("date", "").strip()
        categories = request.form.getlist("category[]")
        contents = request.form.getlist("content[]")

        conn = get_db()
        cur = conn.cursor()

        last_local = cur.execute(
            "SELECT MAX(local_id) FROM reports WHERE department = ?", (dept,)
        ).fetchone()[0]
        next_local_id = (last_local or 0) + 1

        report_date = date_input or datetime.now().strftime("%Y-%m-%d")
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ✅ DB 저장
        cur.execute(
            """
            INSERT INTO reports (local_id, title, date, department, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (next_local_id, title, report_date, dept, created_at)
        )
        report_id = cur.lastrowid


        # ✅ 카테고리/내용 저장
        for cat, cont in zip(categories, contents):
            if cont.strip():
                cur.execute(
                    "INSERT INTO report_contents (report_id, category, content) VALUES (?, ?, ?)",
                    (report_id, cat, cont)
                )

        # ✅ 첨부파일 저장 (uploads/부서명/파일명)
        files = request.files.getlist("files")
        if files:
            dept_path = os.path.join(app.config["UPLOAD_FOLDER"], dept)
            os.makedirs(dept_path, exist_ok=True)

            for f in files:
                if f.filename:
                    original_name = f.filename
                    safe_name = clean_filename(original_name)
                    save_path = os.path.join(dept_path, safe_name)

                    # 같은 이름 존재 시 숫자 붙이기
                    counter = 1
                    while os.path.exists(save_path):
                        name, ext = os.path.splitext(safe_name)
                        new_name = f"{name}_{counter}{ext}"
                        save_path = os.path.join(dept_path, new_name)
                        counter += 1

                    # 파일 저장
                    f.save(save_path)

                    # DB에 실제 저장된 파일명과 원본명 함께 기록
                    cur.execute(
                        """
                        INSERT INTO report_files (report_id, department, filename, original_name)
                        VALUES (?, ?, ?, ?)
                        """,
                        (report_id, dept, os.path.basename(save_path), original_name)
                    )

        conn.commit()
        conn.close()
        return redirect("/list")

    today = datetime.now().date().isoformat()
    return render_template("form.html", today=today, user=user)

# =========================
# 보고서 목록 (검색 + 날짜 필터 유지)
# =========================
@app.route("/list")
@login_required
def report_list():
    user = session["user"]
    dept = user["department"]
    selected_dept = request.args.get("dept")
    today = datetime.now().date()
    two_weeks_ago = today - timedelta(days=14)

    start_date = request.args.get("start_date", two_weeks_ago.isoformat())
    end_date = request.args.get("end_date", today.isoformat())
    search_query = request.args.get("search", "").strip()
    search_filter = request.args.get("filter", "title_content")

    conn = get_db()
    sql = "SELECT * FROM reports WHERE date BETWEEN ? AND ?"
    params = [start_date, end_date]

    # ✅ 관리자 외 부서는 본인 부서만 표시
    if dept != "관리자":
        sql += " AND department = ?"
        params.append(dept)
    elif selected_dept:
        sql += " AND department = ?"
        params.append(selected_dept)

    sql += " ORDER BY id DESC"
    base_reports = conn.execute(sql, tuple(params)).fetchall()

    enriched = []

    # ✅ 1. 카테고리 검색
    if search_query and search_filter == "category":
        q = f"%{search_query.lower()}%"
        for r in base_reports:
            matches = conn.execute("""
                SELECT category, content 
                FROM report_contents
                WHERE report_id = ? AND LOWER(category) LIKE ?
            """, (r["id"], q)).fetchall()

            if matches:  # 카테고리 일치하는 보고서만
                item = dict(r)
                item["match_details"] = [
                    {"category": m["category"], "content": m["content"]}
                    for m in matches
                ]
                files = conn.execute(
                    "SELECT filename, department FROM report_files WHERE report_id = ?",
                    (r["id"],)
                ).fetchall()
                item["has_files"] = len(files) > 0
                item["files"] = [f["filename"] for f in files]
                enriched.append(item)

    # ✅ 2. 제목 + 내용 검색
    elif search_query and search_filter == "title_content":
        q = f"%{search_query.lower()}%"
        for r in base_reports:
            # 제목 또는 내용 중 하나라도 검색어 포함 시 포함
            match_title = conn.execute("""
                SELECT 1 FROM reports 
                WHERE id = ? AND LOWER(title) LIKE ?
            """, (r["id"], q)).fetchone()

            match_content = conn.execute("""
                SELECT 1 FROM report_contents 
                WHERE report_id = ? AND LOWER(content) LIKE ?
            """, (r["id"], q)).fetchone()

            if match_title or match_content:
                item = dict(r)
                item["files"] = [
                    {
                        "name": f["filename"],
                        "original_name": f["original_name"] or f["filename"],
                        "dept": f["department"]
                    }
                    for f in files
                ]
                item["has_files"] = len(files) > 0
                item["files"] = [f["filename"] for f in files]
                item["match_details"] = []
                enriched.append(item)

    # ✅ 3. 검색어 없음 → 전체 목록
    else:
        for r in base_reports:
            item = dict(r)
            files = conn.execute(
                "SELECT filename, department FROM report_files WHERE report_id = ?",
                (r["id"],)
            ).fetchall()
            item["has_files"] = len(files) > 0
            item["files"] = [f["filename"] for f in files]
            item["match_details"] = []
            enriched.append(item)

    conn.close()

    return render_template(
        "list.html",
        reports=enriched,
        user=user,
        departments=DEPT_LIST if dept == "관리자" else None,
        selected_dept=selected_dept,
        start_date=start_date,
        end_date=end_date,
        search_query=search_query,
        search_filter=search_filter
    )

# =========================
# 보고서 보기
# =========================
@app.route('/view/<int:report_id>')
@login_required
def view_report(report_id):
    conn = get_db()
    report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    contents = conn.execute("SELECT * FROM report_contents WHERE report_id = ?", (report_id,)).fetchall()
    files = conn.execute("SELECT * FROM report_files WHERE report_id = ?", (report_id,)).fetchall()
    conn.close()
    return render_template("view.html", report=report, contents=contents, files=files)

# =========================
# 보고서 수정 (edit.html)
# =========================
@app.route("/edit/<int:report_id>", methods=["GET", "POST"])
@login_required
def edit_report(report_id):
    conn = get_db()
    cur = conn.cursor()

    if request.method == "GET":
        report = cur.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        contents = cur.execute("SELECT * FROM report_contents WHERE report_id = ?", (report_id,)).fetchall()
        files = cur.execute("SELECT * FROM report_files WHERE report_id = ?", (report_id,)).fetchall()
        conn.close()
        return render_template("edit.html", report=report, contents=contents, files=files)

    # ------------------------------
    # POST - 수정 저장
    # ------------------------------
    title = request.form.get("title", "").strip() or "일일보고서"
    date_input = request.form.get("date", "").strip()
    categories = request.form.getlist("categories[]")
    contents = request.form.getlist("contents[]")
    new_files = request.files.getlist("new_files")
    dept = session["user"]["department"]

    # ✅ 날짜 입력 없으면 기존 날짜 유지
    if date_input:
        report_date = date_input
    else:
        old_date = cur.execute("SELECT date FROM reports WHERE id = ?", (report_id,)).fetchone()
        report_date = old_date["date"] if old_date else datetime.now().strftime("%Y-%m-%d")

    # ✅ 수정 시각 갱신 (created_at 컬럼에 반영)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ✅ 보고서 기본 정보 수정
    cur.execute(
        """
        UPDATE reports
        SET title = ?, date = ?, created_at = ?
        WHERE id = ?
        """,
        (title, report_date, updated_at, report_id)
    )

    # ✅ 기존 내용 삭제 후 다시 저장
    cur.execute("DELETE FROM report_contents WHERE report_id = ?", (report_id,))
    for cat, text in zip(categories, contents):
        if text.strip():
            cur.execute(
                "INSERT INTO report_contents (report_id, category, content) VALUES (?, ?, ?)",
                (report_id, cat, text.strip())
            )

    # ✅ 첨부파일 업로드 처리
    upload_folder = os.path.join(app.config["UPLOAD_FOLDER"], dept)
    os.makedirs(upload_folder, exist_ok=True)

    for file in new_files:
        if file and file.filename:
            original_name = file.filename
            safe_name = clean_filename(original_name)
            save_path = os.path.join(upload_folder, safe_name)

            counter = 1
            while os.path.exists(save_path):
                name, ext = os.path.splitext(safe_name)
                new_name = f"{name}_{counter}{ext}"
                save_path = os.path.join(upload_folder, new_name)
                counter += 1

            # 파일 저장
            file.save(save_path)

            # DB에 저장된 파일명 + 원본 이름 기록
            cur.execute(
                """
                INSERT INTO report_files (report_id, department, filename, original_name)
                VALUES (?, ?, ?, ?)
                """,
                (report_id, dept, os.path.basename(save_path), original_name)
            )

    conn.commit()
    conn.close()
    flash("✅ 보고서가 수정되었습니다.")
    return redirect(url_for("view_report", report_id=report_id))


# =========================
# 보고서 삭제
# =========================
@app.route("/delete/<int:report_id>", methods=["POST"])
@login_required
def delete_report(report_id):
    conn = get_db()
    cur = conn.cursor()

    # 보고서 및 첨부파일 조회
    report = cur.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    files = cur.execute("SELECT * FROM report_files WHERE report_id = ?", (report_id,)).fetchall()

    if not report:
        conn.close()
        flash("❌ 존재하지 않는 보고서입니다.")
        return redirect("/list")

    # 첨부파일 실제 파일 삭제
    for f in files:
        dept = f["department"]
        filename = f["filename"]
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], dept, filename)
        if os.path.exists(file_path):
            os.remove(file_path)

    # DB에서 삭제
    cur.execute("DELETE FROM report_files WHERE report_id = ?", (report_id,))
    cur.execute("DELETE FROM report_contents WHERE report_id = ?", (report_id,))
    cur.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    conn.commit()
    conn.close()

    flash("🗑️ 보고서가 삭제되었습니다.")
    return redirect("/list")

# =========================
# 첨부파일 삭제
# =========================
@app.route('/delete_file/<int:report_id>/<filename>', methods=['POST'])
@login_required
def delete_file(report_id, filename):
    conn = get_db()
    cur = conn.cursor()
    file_row = cur.execute(
        "SELECT department FROM report_files WHERE report_id = ? AND filename = ?",
        (report_id, filename)
    ).fetchone()

    if not file_row:
        conn.close()
        return jsonify({"status": "error", "message": "파일 정보가 없습니다."}), 404

    dept = file_row["department"]
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], dept, filename)

    if os.path.exists(file_path):
        os.remove(file_path)
        cur.execute('DELETE FROM report_files WHERE report_id = ? AND filename = ?', (report_id, filename))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"{filename} 삭제됨"}), 200
    else:
        conn.close()
        return jsonify({"status": "error", "message": "파일이 존재하지 않습니다."}), 404

# =========================
# 파일 미리보기/다운로드
# =========================
@app.route("/uploads/<department>/<path:filename>")
@login_required
def uploaded_file(department, filename):
    """첨부파일 다운로드 및 미리보기 (한글 이름 + 캐시 무시 + 이미지 호환)"""
    upload_path = os.path.join(app.config["UPLOAD_FOLDER"], department)
    full_path = os.path.join(upload_path, filename)

    if not os.path.exists(full_path):
        return jsonify({"status": "error", "message": "파일이 존재하지 않습니다."}), 404

    try:
        # DB에서 원래 파일 이름 조회
        conn = get_db()
        cur = conn.cursor()
        file_info = cur.execute(
            "SELECT original_name FROM report_files WHERE department = ? AND filename = ?",
            (department, filename)
        ).fetchone()
        conn.close()

        download_name = file_info["original_name"] if file_info and file_info["original_name"] else filename

        # MIME 타입 자동 추정
        mime_type, _ = mimetypes.guess_type(full_path)
        mime_type = mime_type or "application/octet-stream"

        # ✅ send_file 사용 (브라우저에서 직접 처리 가능)
        response = send_file(
            full_path,
            as_attachment=True,
            download_name=download_name,
            mimetype=mime_type
        )

        # ✅ 캐시 방지 헤더
        quoted_name = urllib.parse.quote(download_name)
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quoted_name}"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Render-Bypass"] = "true"

        return response

    except Exception as e:
        print(f"❌ File serving error: {e}")
        return jsonify({"status": "error", "message": "파일 전송 중 오류가 발생했습니다."}), 500

# =========================
# 실행
# =========================
if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print("⚙️ reports.db not found. Creating new persistent database...")
        init_db()
        print("✅ reports.db created successfully at /var/data")
    app.run(host="0.0.0.0", port=5000, debug=False)


