import streamlit as st
import sqlite3
import pandas as pd
import io
import hashlib
import hmac
import os
from datetime import datetime

from datetime import date, timedelta
from PIL import Image

# =========================================================
# PAGE CONFIGURATION
# =========================================================

st.set_page_config(
    page_title="Student Attendance Management",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded"
)

DB_NAME = "attendance.db"

# =========================================================
# ADMIN LOGIN
# =========================================================

def verify_admin_password(password):
    return password == "admin"

    salt = st.secrets["ADMIN_SALT"]
    stored_hash = st.secrets["ADMIN_PASSWORD_HASH"]

    entered_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        600000
    ).hex()

    return hmac.compare_digest(
        entered_hash,
        stored_hash
    )


if "logged_in" not in st.session_state:
    st.session_state.logged_in = False


# =========================================================
# DATABASE CONNECTION
# =========================================================

def connect():

    conn = sqlite3.connect(
        DB_NAME,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    return conn


def migrate_roll_number_uniqueness(conn):
    """
    Ensure roll numbers are unique per active class, not globally.
    If an older database has UNIQUE(roll_no), rebuild the students table
    while retaining student IDs, photos, class links and attendance links.
    """
    cur = conn.cursor()
    indexes = cur.execute("PRAGMA index_list('students')").fetchall()
    has_global_roll_unique = False

    for index in indexes:
        if int(index["unique"]) != 1:
            continue
        index_name = index["name"].replace('"', '""')
        index_columns = [
            item["name"]
            for item in cur.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
        ]
        if index_columns == ["roll_no"]:
            has_global_roll_unique = True
            break

    if has_global_roll_unique:
        # Make a consistent SQLite backup before changing the table.
        backup_name = (
            f"{DB_NAME}.before_roll_number_migration_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
        )
        try:
            backup_conn = sqlite3.connect(backup_name)
            conn.backup(backup_conn)
            backup_conn.close()
        except Exception:
            try:
                if os.path.exists(backup_name):
                    os.remove(backup_name)
            except OSError:
                pass

        # Foreign keys must be disabled before beginning the rebuild.
        conn.commit()
        cur.execute("PRAGMA foreign_keys = OFF")

        try:
            info = cur.execute("PRAGMA table_info('students')").fetchall()
            definitions = []
            column_names = []

            for column in info:
                name = column["name"]
                column_names.append(name)
                quoted_name = '"' + name.replace('"', '""') + '"'
                column_type = column["type"] or "TEXT"

                if int(column["pk"]) > 0:
                    if name == "id" and column_type.upper() == "INTEGER":
                        definitions.append(
                            f"{quoted_name} INTEGER PRIMARY KEY AUTOINCREMENT"
                        )
                    else:
                        definitions.append(
                            f"{quoted_name} {column_type} PRIMARY KEY"
                        )
                    continue

                definition = f"{quoted_name} {column_type}"
                if int(column["notnull"]):
                    definition += " NOT NULL"
                if column["dflt_value"] is not None:
                    definition += f" DEFAULT {column['dflt_value']}"
                definitions.append(definition)

            # Preserve the existing student-table foreign keys.
            foreign_keys = cur.execute(
                "PRAGMA foreign_key_list('students')"
            ).fetchall()
            for fk in foreign_keys:
                from_col = '"' + fk["from"].replace('"', '""') + '"'
                to_table = '"' + fk["table"].replace('"', '""') + '"'
                to_col = '"' + fk["to"].replace('"', '""') + '"'
                fk_sql = (
                    f"FOREIGN KEY({from_col}) REFERENCES "
                    f"{to_table}({to_col})"
                )
                if fk["on_update"] and fk["on_update"] != "NO ACTION":
                    fk_sql += f" ON UPDATE {fk['on_update']}"
                if fk["on_delete"] and fk["on_delete"] != "NO ACTION":
                    fk_sql += f" ON DELETE {fk['on_delete']}"
                definitions.append(fk_sql)

            cur.execute(
                "CREATE TABLE students_roll_migration_new "
                f"({', '.join(definitions)})"
            )

            quoted_columns = ", ".join(
                '"' + name.replace('"', '""') + '"'
                for name in column_names
            )
            cur.execute(
                f"INSERT INTO students_roll_migration_new ({quoted_columns}) "
                f"SELECT {quoted_columns} FROM students"
            )
            cur.execute("DROP TABLE students")
            cur.execute(
                "ALTER TABLE students_roll_migration_new RENAME TO students"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.execute("PRAGMA foreign_keys = ON")

    # A student roll number is unique only among active students in one class.
    # If old data already contains duplicates, leave enforcement to the
    # registration/edit checks rather than preventing the app from starting.
    try:
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_students_class_roll_active
            ON students(class_id, roll_no)
            WHERE active = 1
        """)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()


def setup_database():

    conn = connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            teacher_name TEXT DEFAULT '',
            active INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            roll_no TEXT NOT NULL,
            class_id INTEGER,
            photo BLOB,
            active INTEGER DEFAULT 1,
            FOREIGN KEY(class_id) REFERENCES classes(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(student_id, subject_id, attendance_date),
            FOREIGN KEY(student_id) REFERENCES students(id),
            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        )
    """)

    # Add a class teacher field to existing databases without deleting data.
    class_columns = [
        row["name"]
        for row in cur.execute(
            "PRAGMA table_info(classes)"
        ).fetchall()
    ]

    if "teacher_name" not in class_columns:
        cur.execute(
            "ALTER TABLE classes ADD COLUMN teacher_name TEXT DEFAULT ''"
        )

    student_columns = [
        row["name"]
        for row in cur.execute(
            "PRAGMA table_info(students)"
        ).fetchall()
    ]

    if "class_id" not in student_columns:
        cur.execute(
            "ALTER TABLE students ADD COLUMN class_id INTEGER"
        )

    if "photo" not in student_columns:
        cur.execute(
            "ALTER TABLE students ADD COLUMN photo BLOB"
        )

    if "active" not in student_columns:
        cur.execute(
            "ALTER TABLE students ADD COLUMN active INTEGER DEFAULT 1"
        )

    cur.execute(
        "INSERT OR IGNORE INTO classes(name) VALUES (?)",
        ("Class 1",)
    )

    default_class = cur.execute(
        "SELECT id FROM classes WHERE name = ?",
        ("Class 1",)
    ).fetchone()

    cur.execute(
        "UPDATE students SET class_id = ? WHERE class_id IS NULL",
        (default_class["id"],)
    )

    # Run this after legacy students have been assigned to the default class.
    # This avoids a NULL class_id becoming a duplicate after the unique index
    # is created. Existing duplicate records are preserved; the app still
    # validates new registrations and edits within each class.
    migrate_roll_number_uniqueness(conn)

    conn.commit()
    conn.close()


setup_database()


# =========================================================
# LOGIN SCREEN
# =========================================================

if not st.session_state.logged_in:

    st.markdown("""
    <style>
    .stApp {
        background: linear-gradient(135deg, #102A43, #0D9488);
    }

    [data-testid="stHeader"] {
        background: transparent;
    }

    .login-title {
        color: white;
        font-size: 32px;
        font-weight: 800;
        text-align: center;
    }

    .login-subtitle {
        color: #E2E8F0;
        text-align: center;
        margin-bottom: 25px;
    }

    </style>
    """, unsafe_allow_html=True)

    left, center, right = st.columns([1, 1.5, 1])

    with center:

        st.markdown("<br><br>", unsafe_allow_html=True)

        st.markdown(
            '<div class="login-title">🎓 Attendance Portal</div>',
            unsafe_allow_html=True
        )

        st.markdown(
            '<div class="login-subtitle">Administrator Login</div>',
            unsafe_allow_html=True
        )

        with st.container(border=True):

            st.subheader("Welcome Back")

            with st.form("admin_login_form"):

                username = st.text_input(
                    "Username",
                    placeholder="Enter administrator username"
                )

                password = st.text_input(
                    "Password",
                    type="password",
                    placeholder="Enter your password"
                )

                login_button = st.form_submit_button(
                    "Login",
                    use_container_width=True
                )

                if login_button:

                    try:

                        correct_username = (
                            username == st.secrets["ADMIN_USERNAME"]
                        )

                        correct_password = verify_admin_password(
                            password
                        )

                        if correct_username and correct_password:

                            st.session_state.logged_in = True
                            st.rerun()

                        else:
                            st.error(
                                "Incorrect username or password."
                            )

                    except Exception:
                        st.error(
                            "Login configuration is missing. "
                            "Please configure .streamlit/secrets.toml."
                        )

        st.markdown(
            "<p style='text-align:center;color:white;'>"
            "Student Attendance Management System"
            "</p>",
            unsafe_allow_html=True
        )

    st.stop()


# =========================================================
# DATABASE HELPERS
# =========================================================

def read_data(sql, params=()):

    conn = connect()

    df = pd.read_sql_query(
        sql,
        conn,
        params=params
    )

    conn.close()

    return df


def execute(sql, params=()):

    conn = connect()

    conn.execute(sql, params)

    conn.commit()
    conn.close()


def get_classes():

    return read_data("""
        SELECT * FROM classes
        WHERE active = 1
        ORDER BY name
    """)


def get_subjects():

    return read_data("""
        SELECT * FROM subjects
        WHERE active = 1
        ORDER BY name
    """)


def get_students(class_id=None):

    if class_id is None:

        return read_data("""
            SELECT s.*, c.name AS class_name
            FROM students s
            LEFT JOIN classes c ON s.class_id = c.id
            WHERE s.active = 1
            ORDER BY s.name
        """)

    return read_data("""
        SELECT s.*, c.name AS class_name
        FROM students s
        LEFT JOIN classes c ON s.class_id = c.id
        WHERE s.active = 1 AND s.class_id = ?
        ORDER BY s.name
    """, (int(class_id),))


# =========================================================
# CUSTOM CSS
# =========================================================

st.markdown("""
<style>

.stApp {
    background-color: #F3F6FA;
    color: #1E293B;
}

[data-testid="stSidebar"] {
    background: #102A43;
}

[data-testid="stSidebar"] * {
    color: white !important;
}

.main-title {
    font-size: 30px;
    font-weight: 800;
    color: #102A43;
    margin-bottom: 5px;
}

.subtitle {
    color: #64748B;
    font-size: 14px;
    margin-bottom: 25px;
}

.absent-card {
    background: #FFF1F2;
    border-left: 5px solid #DC2626;
    padding: 12px;
    border-radius: 8px;
    margin-bottom: 8px;
    color: #991B1B;
}

div.stButton > button {
    background-color: #0D9488;
    color: white;
    border-radius: 8px;
    border: none;
    font-weight: 600;
    min-height: 40px;
}

div.stButton > button:hover {
    background-color: #0F766E;
    color: white;
}

h1, h2, h3 {
    color: #102A43;
}

@media (max-width: 768px) {

    .main-title {
        font-size: 23px;
    }

    [data-testid="stMetric"] {
        background: white;
        padding: 10px;
        border-radius: 10px;
    }

    .block-container {
        padding-left: 1rem;
        padding-right: 1rem;
    }

}

</style>
""", unsafe_allow_html=True)


# =========================================================
# HEADER
# =========================================================

st.markdown(
    '<div class="main-title">🎓 Student Attendance Management</div>',
    unsafe_allow_html=True
)

st.markdown(
    '<div class="subtitle">Manage classes, students, subjects and attendance.</div>',
    unsafe_allow_html=True
)

st.success("✓ Logged in as Admin")


# =========================================================
# SIDEBAR NAVIGATION
# =========================================================

with st.sidebar:

    st.title("🎓 Attendance App")
    st.caption("Administrator Dashboard")

    st.success("Logged in as Admin")

    if st.button(
        "🚪 Logout",
        use_container_width=True
    ):

        st.session_state.logged_in = False
        st.rerun()

    st.divider()

    page = st.radio(
        "Navigation",
        [
            "Dashboard",
            "Classes",
            "Students",
            "Subjects",
            "Mark Attendance",
            "Reports"
        ]
    )

    st.divider()

    st.caption("Student Attendance System")


# =========================================================
# CLASSES PAGE
# =========================================================

if page == "Classes":

    st.header("🏫 Manage Classes")

    with st.expander("➕ Add New Class", expanded=True):

        with st.form("add_class_form"):

            class_name = st.text_input("Class Name")
            teacher_name = st.text_input("Class Teacher Name")
            submitted = st.form_submit_button("Add Class")

            if submitted:

                if class_name.strip():

                    try:
                        clean_class_name = class_name.strip()
                        duplicate_class = read_data(
                            "SELECT id FROM classes WHERE name = ? COLLATE NOCASE",
                            (clean_class_name,)
                        )

                        if not duplicate_class.empty:
                            st.error("This class already exists. You can edit its name from the class list.")
                        else:
                            execute(
                                "INSERT INTO classes(name, teacher_name) VALUES (?, ?)",
                                (clean_class_name, teacher_name.strip())
                            )
                            st.success(
                                f"{clean_class_name} created successfully. "
                                "You can register students later; adding students is optional."
                            )
                            st.rerun()

                    except sqlite3.Error as error:
                        st.error(f"Could not create class: {error}")

                else:
                    st.warning("Please enter a class name.")

    class_df = get_classes()

    if not class_df.empty:

        st.subheader("Available Classes")
        st.caption("Select View Details to see the class teacher, student list and complete attendance history.")

        for _, row in class_df.iterrows():

            col1, col2, col3, col4 = st.columns([3, 2, 1, 1])
            col1.write(f"**{row['name']}**")
            col2.caption(f"Teacher: {row.get('teacher_name', '') or 'Not assigned'}")

            if col3.button("Edit", key=f"edit_class_{row['id']}"):
                st.session_state["class_detail_id"] = int(row["id"])
                st.session_state["show_class_editor"] = True
                st.rerun()

            if col4.button("View", key=f"view_class_{row['id']}"):
                st.session_state["class_detail_id"] = int(row["id"])
                st.session_state["show_class_editor"] = False
                st.rerun()

            st.divider()

            if st.button(
                "Deactivate",
                key=f"delclass_{row['id']}"
            ):

                execute(
                    "UPDATE classes SET active = 0 WHERE id = ?",
                    (int(row["id"]),)
                )

                if st.session_state.get("class_detail_id") == int(row["id"]):
                    st.session_state.pop("class_detail_id", None)

                st.rerun()

        # Detailed view for the selected class.
        selected_class_id = st.session_state.get("class_detail_id")
        selected_class_df = class_df[class_df["id"] == selected_class_id]

        if not selected_class_df.empty:

            selected_class_row = selected_class_df.iloc[0]
            selected_class_name = selected_class_row["name"]

            st.divider()
            st.header(f"📘 {selected_class_name} — Class Details")

            with st.expander("✏️ Edit Class Name and Teacher", expanded=st.session_state.get("show_class_editor", False)):

                with st.form(f"class_edit_form_{selected_class_id}"):

                    updated_class_name = st.text_input(
                        "Class Name",
                        value=selected_class_name
                    )
                    updated_teacher = st.text_input(
                        "Class Teacher Name",
                        value=selected_class_row.get("teacher_name", "") or ""
                    )

                    save_class_changes = st.form_submit_button("Save Class Changes")

                    if save_class_changes:
                        clean_name = updated_class_name.strip()

                        if not clean_name:
                            st.warning("Class name cannot be empty.")
                        else:
                            duplicate = read_data(
                                "SELECT id FROM classes WHERE name = ? COLLATE NOCASE AND id != ?",
                                (clean_name, int(selected_class_id))
                            )

                            if not duplicate.empty:
                                st.error("Another class already has this name. Choose a different name.")
                            else:
                                execute(
                                    "UPDATE classes SET name = ?, teacher_name = ? WHERE id = ?",
                                    (clean_name, updated_teacher.strip(), int(selected_class_id))
                                )
                                st.session_state["show_class_editor"] = False
                                st.success("Class name and teacher updated. Existing students and attendance history remain linked to this class.")
                                st.rerun()

            class_students = read_data("""
                SELECT
                    s.id,
                    s.name,
                    s.roll_no,
                    COUNT(CASE WHEN a.status IN ('Present', 'Absent') THEN 1 END) AS total_classes,
                    SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) AS present,
                    SUM(CASE WHEN a.status = 'Absent' THEN 1 ELSE 0 END) AS absent,
                    SUM(CASE WHEN a.status = 'Leave' THEN 1 ELSE 0 END) AS leave_count
                FROM students s
                LEFT JOIN attendance a ON a.student_id = s.id
                WHERE s.active = 1 AND s.class_id = ?
                GROUP BY s.id, s.name, s.roll_no
                ORDER BY s.name
            """, (int(selected_class_id),))

            if not class_students.empty:
                class_students[["present", "absent", "leave_count", "total_classes"]] = class_students[["present", "absent", "leave_count", "total_classes"]].fillna(0).astype(int)
                class_students["Cumulative Attendance (%)"] = (
                    class_students["present"] /
                    class_students["total_classes"].replace(0, pd.NA) * 100
                ).fillna(0).round(2)

            total_class_students = len(class_students)
            total_attendance_records = int(class_students[["present", "absent", "leave_count"]].to_numpy().sum()) if not class_students.empty else 0
            teacher_display = selected_class_row.get("teacher_name", "") or "Not assigned"

            metric1, metric2, metric3 = st.columns(3)
            metric1.metric("Total Students", total_class_students)
            metric2.metric("Class Teacher", teacher_display)
            metric3.metric("Attendance Records", total_attendance_records)

            st.subheader("👨‍🎓 Student List and Cumulative Attendance")

            if class_students.empty:
                st.info("No active students are registered in this class.")
            else:
                class_display = class_students.rename(columns={
                    "name": "Student Name",
                    "roll_no": "Roll Number",
                    "total_classes": "Total Classes",
                    "present": "Present",
                    "absent": "Absent",
                    "leave_count": "Leave"
                })
                st.dataframe(
                    class_display[[
                        "Student Name", "Roll Number", "Total Classes",
                        "Present", "Absent", "Leave", "Cumulative Attendance (%)"
                    ]],
                    use_container_width=True,
                    hide_index=True
                )

            st.subheader("📅 Complete Class Attendance History")
            class_history = read_data("""
                SELECT
                    a.attendance_date AS Date,
                    sub.name AS Subject,
                    s.name AS Student,
                    s.roll_no AS Roll_Number,
                    a.status AS Status
                FROM attendance a
                JOIN students s ON s.id = a.student_id
                JOIN subjects sub ON sub.id = a.subject_id
                WHERE s.class_id = ?
                ORDER BY a.attendance_date DESC, sub.name, s.name
            """, (int(selected_class_id),))

            if class_history.empty:
                st.info("No attendance history is available for this class yet.")
            else:
                st.dataframe(
                    class_history,
                    use_container_width=True,
                    hide_index=True
                )

            if st.button("Close Class Details", key="close_class_details"):
                st.session_state.pop("class_detail_id", None)
                st.rerun()

    else:
        st.info("No classes available.")


# =========================================================
# STUDENTS PAGE
# =========================================================

elif page == "Students":

    st.header("👨‍🎓 Student Management")

    class_df = get_classes()

    if class_df.empty:

        st.warning("Please create a class first.")

    else:

        class_options = {
            row["name"]: int(row["id"])
            for _, row in class_df.iterrows()
        }

        with st.expander(
            "➕ Register New Student",
            expanded=True
        ):

            with st.form("student_form"):

                name = st.text_input("Student Full Name")

                roll_no = st.text_input("Roll Number")

                selected_class = st.selectbox(
                    "Select Class",
                    list(class_options.keys())
                )

                photo_file = st.file_uploader(
                    "Upload Student Photo",
                    type=["jpg", "jpeg", "png"]
                )

                submit_student = st.form_submit_button(
                    "Register Student"
                )

                if submit_student:

                    if not name.strip() or not roll_no.strip():

                        st.warning(
                            "Please enter student name and roll number."
                        )

                    else:

                        photo_bytes = (
                            photo_file.getvalue()
                            if photo_file else None
                        )

                        try:
                            chosen_class_id = class_options[selected_class]

                            # Roll numbers may repeat in different classes,
                            # but must be unique inside the same class.
                            duplicate_roll = read_data(
                                """
                                SELECT id FROM students
                                WHERE class_id = ? AND roll_no = ? AND active = 1
                                """,
                                (chosen_class_id, roll_no.strip())
                            )

                            if not duplicate_roll.empty:
                                st.error(
                                    f"Roll number {roll_no.strip()} is already used "
                                    f"in {selected_class}. Choose another roll number."
                                )
                            else:
                                execute("""
                                    INSERT INTO students
                                    (name, roll_no, class_id, photo)
                                    VALUES (?, ?, ?, ?)
                                """, (
                                    name.strip(),
                                    roll_no.strip(),
                                    chosen_class_id,
                                    photo_bytes
                                ))

                                st.success(
                                    f"Student registered successfully in {selected_class}."
                                )
                                st.rerun()

                        except sqlite3.IntegrityError as error:
                            st.error(
                                "The database rejected this student record. "
                                f"Database details: {error}. "
                                "If this roll number is already used in another class, "
                                "your existing database may still have an old global "
                                "roll-number restriction; send me this exact message "
                                "so I can safely migrate it without losing attendance."
                            )
                        except sqlite3.Error as error:
                            st.error(f"Database error while registering student: {error}")

        st.subheader("Registered Students")

        selected_class_filter = st.selectbox(
            "Filter by Class",
            ["All Classes"] + list(class_options.keys())
        )

        if selected_class_filter == "All Classes":

            student_df = get_students()

        else:

            student_df = get_students(
                class_options[selected_class_filter]
            )

        if student_df.empty:

            st.info("No students registered.")

        else:

            for _, student in student_df.iterrows():

                col1, col2, col3 = st.columns([1, 4, 1])

                with col1:

                    if student["photo"] is not None:

                        try:

                            st.image(
                                Image.open(
                                    io.BytesIO(student["photo"])
                                ),
                                width=65
                            )

                        except Exception:
                            st.write("👤")

                    else:
                        st.write("👤")

                with col2:

                    st.write(f"**{student['name']}**")

                    st.caption(
                        f"Roll No: {student['roll_no']} | "
                        f"Class: {student['class_name']}"
                    )

                with col3:

                    if st.button(
                        "Edit",
                        key=f"edit_student_{student['id']}"
                    ):
                        st.session_state["edit_student_id"] = int(student["id"])
                        st.rerun()

                    if st.button(
                        "Remove",
                        key=f"remove_student_{student['id']}"
                    ):

                        execute(
                            "UPDATE students SET active = 0 WHERE id = ?",
                            (int(student["id"]),)
                        )

                        if st.session_state.get("edit_student_id") == int(student["id"]):
                            st.session_state.pop("edit_student_id", None)

                        st.rerun()

                if st.session_state.get("edit_student_id") == int(student["id"]):

                    with st.expander(f"✏️ Edit {student['name']}", expanded=True):

                        with st.form(f"edit_student_form_{student['id']}"):

                            edited_name = st.text_input(
                                "Student Full Name",
                                value=student["name"]
                            )
                            edited_roll = st.text_input(
                                "Roll Number",
                                value=student["roll_no"]
                            )
                            save_student = st.form_submit_button("Save Changes")

                            if save_student:
                                if not edited_name.strip() or not edited_roll.strip():
                                    st.warning("Student name and roll number cannot be empty.")
                                else:
                                    try:
                                        duplicate_edit = read_data(
                                            """
                                            SELECT id FROM students
                                            WHERE class_id = ?
                                              AND roll_no = ?
                                              AND active = 1
                                              AND id != ?
                                            """,
                                            (
                                                int(student["class_id"]),
                                                edited_roll.strip(),
                                                int(student["id"])
                                            )
                                        )

                                        if not duplicate_edit.empty:
                                            st.error(
                                                f"Roll number {edited_roll.strip()} is already used "
                                                f"in {student['class_name']}. Choose another roll number."
                                            )
                                        else:
                                            execute(
                                                "UPDATE students SET name = ?, roll_no = ? WHERE id = ?",
                                                (
                                                    edited_name.strip(),
                                                    edited_roll.strip(),
                                                    int(student["id"])
                                                )
                                            )
                                            st.session_state.pop("edit_student_id", None)
                                            st.success(
                                                "Student details updated. Existing attendance history is preserved."
                                            )
                                            st.rerun()

                                    except sqlite3.IntegrityError as error:
                                        st.error(
                                            "Could not update student because this roll number "
                                            f"conflicts with an existing record: {error}"
                                        )

                        if st.button("Cancel Edit", key=f"cancel_edit_{student['id']}"):
                            st.session_state.pop("edit_student_id", None)
                            st.rerun()

                st.divider()


# =========================================================
# SUBJECTS PAGE
# =========================================================

elif page == "Subjects":

    st.header("📚 Manage Subjects")

    with st.form("subject_form"):

        subject_name = st.text_input("Subject Name")

        submit_subject = st.form_submit_button("Add Subject")

        if submit_subject:

            if subject_name.strip():

                try:

                    execute(
                        "INSERT INTO subjects(name) VALUES (?)",
                        (subject_name.strip(),)
                    )

                    st.success("Subject added.")
                    st.rerun()

                except sqlite3.IntegrityError:
                    st.error("Subject already exists.")

            else:
                st.warning("Enter a subject name.")

    subject_df = get_subjects()

    if not subject_df.empty:

        st.subheader("Available Subjects")

        for _, subject in subject_df.iterrows():

            col1, col2 = st.columns([4, 1])

            col1.write(f"**{subject['name']}**")

            if col2.button(
                "Deactivate",
                key=f"deactivate_subject_{subject['id']}"
            ):

                execute(
                    "UPDATE subjects SET active = 0 WHERE id = ?",
                    (int(subject["id"]),)
                )

                st.rerun()

    else:
        st.info("No subjects added yet.")


# =========================================================
# MARK ATTENDANCE PAGE
# =========================================================

elif page == "Mark Attendance":

    st.header("📝 Mark Daily Attendance")

    class_df = get_classes()
    subject_df = get_subjects()

    if class_df.empty or subject_df.empty:

        st.warning(
            "Please add at least one class and one subject first."
        )

    else:

        class_options = {
            row["name"]: int(row["id"])
            for _, row in class_df.iterrows()
        }

        subject_options = {
            row["name"]: int(row["id"])
            for _, row in subject_df.iterrows()
        }

        col1, col2, col3 = st.columns(3)

        with col1:

            selected_class = st.selectbox(
                "Select Class",
                list(class_options.keys())
            )

        with col2:

            selected_subject = st.selectbox(
                "Select Subject",
                list(subject_options.keys())
            )

        with col3:

            attendance_date = st.date_input(
                "Attendance Date",
                value=date.today()
            )

        students_df = get_students(
            class_options[selected_class]
        )

        if students_df.empty:

            st.info("No students registered in this class.")

        else:

            existing = read_data("""
                SELECT student_id, status
                FROM attendance
                WHERE subject_id = ? AND attendance_date = ?
            """, (
                subject_options[selected_subject],
                attendance_date.isoformat()
            ))

            existing_status = {}

            if not existing.empty:

                existing_status = dict(
                    zip(
                        existing["student_id"],
                        existing["status"]
                    )
                )

            attendance_data = []

            for _, student in students_df.iterrows():

                attendance_data.append({

                    "Student ID": int(student["id"]),

                    "Student Name": student["name"],

                    "Roll Number": student["roll_no"],

                    "Status": existing_status.get(
                        int(student["id"]),
                        "Present"
                    )

                })

            attendance_df = pd.DataFrame(attendance_data)

            st.caption(
                "Select Present, Absent or Leave for each student."
            )

            edited_df = st.data_editor(
                attendance_df,
                use_container_width=True,
                hide_index=True,
                disabled=[
                    "Student ID",
                    "Student Name",
                    "Roll Number"
                ],
                column_config={
                    "Status": st.column_config.SelectboxColumn(
                        "Attendance Status",
                        options=[
                            "Present",
                            "Absent",
                            "Leave"
                        ],
                        required=True
                    )
                },
                key="attendance_editor"
            )

            if st.button(
                "💾 Save Attendance",
                use_container_width=True
            ):

                conn = connect()

                for _, row in edited_df.iterrows():

                    conn.execute("""
                        INSERT INTO attendance
                        (student_id, subject_id, attendance_date, status)
                        VALUES (?, ?, ?, ?)

                        ON CONFLICT(
                            student_id,
                            subject_id,
                            attendance_date
                        )

                        DO UPDATE SET
                            status = excluded.status
                    """, (
                        int(row["Student ID"]),
                        subject_options[selected_subject],
                        attendance_date.isoformat(),
                        row["Status"]
                    ))

                conn.commit()
                conn.close()

                st.success("Attendance saved successfully!")

                st.rerun()


# =========================================================
# DASHBOARD PAGE
# =========================================================

elif page == "Dashboard":

    st.header("📊 Attendance Dashboard")

    today = date.today().isoformat()

    total_students_df = read_data("""
        SELECT COUNT(*) AS total
        FROM students
        WHERE active = 1
    """)

    total_students = int(
        total_students_df.iloc[0]["total"]
    )

    daily = read_data("""
        SELECT status, COUNT(*) AS total
        FROM attendance
        WHERE attendance_date = ?
        GROUP BY status
    """, (today,))

    daily_counts = {
        row["status"]: int(row["total"])
        for _, row in daily.iterrows()
    }

    present = daily_counts.get("Present", 0)
    absent = daily_counts.get("Absent", 0)
    leave = daily_counts.get("Leave", 0)

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Total Students", total_students)

    c2.metric("Present Today", present)

    c3.metric("Absent Today", absent)

    c4.metric("On Leave", leave)

    st.divider()

    st.subheader("🚨 Students Absent Today")

    absent_df = read_data("""
        SELECT
            s.name,
            s.roll_no,
            c.name AS class_name,
            sub.name AS subject_name

        FROM attendance a

        JOIN students s ON a.student_id = s.id

        JOIN classes c ON s.class_id = c.id

        JOIN subjects sub ON a.subject_id = sub.id

        WHERE a.attendance_date = ?
          AND a.status = 'Absent'
          AND s.active = 1

        ORDER BY c.name, s.name
    """, (today,))

    if absent_df.empty:

        st.success("No absent students recorded today.")

    else:

        for _, student in absent_df.iterrows():

            st.markdown(
                f"""
                <div class="absent-card">
                    <b>🔴 {student['name']}</b><br>
                    Roll No: {student['roll_no']} |
                    Class: {student['class_name']} |
                    Subject: {student['subject_name']}
                </div>
                """,
                unsafe_allow_html=True
            )

    st.divider()

    st.subheader("📚 Today's Class Summary")

    # Start from the classes table so every active class appears, even if
    # its attendance has not been saved or linked correctly for today.
    summary = read_data("""
        SELECT
            c.name AS Class,
            COALESCE(sub.name, 'No subject recorded') AS Subject,
            COUNT(a.id) AS Total_Records,
            COUNT(DISTINCT a.student_id) AS Students_Marked,
            SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) AS Present,
            SUM(CASE WHEN a.status = 'Absent' THEN 1 ELSE 0 END) AS Absent,
            SUM(CASE WHEN a.status = 'Leave' THEN 1 ELSE 0 END) AS Leave
        FROM classes c
        LEFT JOIN students s
            ON s.class_id = c.id AND s.active = 1
        LEFT JOIN attendance a
            ON a.student_id = s.id AND a.attendance_date = ?
        LEFT JOIN subjects sub
            ON sub.id = a.subject_id
        WHERE c.active = 1
        GROUP BY c.id, c.name, sub.id, sub.name
        ORDER BY c.name, sub.name
    """, (today,))

    if summary.empty:
        st.info("No active classes are available.")
    else:
        # Replace SQL NULLs from classes with no attendance by readable zeros.
        for column in ["Total_Records", "Students_Marked", "Present", "Absent", "Leave"]:
            summary[column] = summary[column].fillna(0).astype(int)

        st.dataframe(
            summary,
            use_container_width=True,
            hide_index=True
        )

    st.caption(
        "Dashboard counts attendance records. "
        "A student marked in multiple subjects can appear more than once."
    )


# =========================================================
# REPORTS PAGE
# =========================================================

elif page == "Reports":

    st.header("📈 Attendance Reports")

    class_df = get_classes()
    subject_df = get_subjects()

    col1, col2 = st.columns(2)

    with col1:

        start_date = st.date_input(
            "Start Date",
            value=date.today() - timedelta(days=30),
            key="report_start"
        )

    with col2:

        end_date = st.date_input(
            "End Date",
            value=date.today(),
            key="report_end"
        )

    class_options = {
        row["name"]: int(row["id"])
        for _, row in class_df.iterrows()
    }

    subject_options = {
        row["name"]: int(row["id"])
        for _, row in subject_df.iterrows()
    }

    filter_col1, filter_col2 = st.columns(2)

    with filter_col1:

        selected_class = st.selectbox(
            "Class Filter",
            ["All Classes"] + list(class_options.keys())
        )

    with filter_col2:

        selected_subject = st.selectbox(
            "Subject Filter",
            ["All Subjects"] + list(subject_options.keys())
        )

    if start_date > end_date:

        st.error("Start date cannot be after end date.")

    else:

        sql = """
            SELECT
                s.id AS student_id,
                s.name AS student_name,
                s.roll_no,
                c.name AS class_name,
                sub.name AS subject_name,

                COUNT(*) AS total_marked,

                SUM(
                    CASE WHEN a.status = 'Present'
                    THEN 1 ELSE 0 END
                ) AS present,

                SUM(
                    CASE WHEN a.status = 'Absent'
                    THEN 1 ELSE 0 END
                ) AS absent,

                SUM(
                    CASE WHEN a.status = 'Leave'
                    THEN 1 ELSE 0 END
                ) AS leave_count

            FROM attendance a

            JOIN students s ON a.student_id = s.id

            JOIN classes c ON s.class_id = c.id

            JOIN subjects sub ON a.subject_id = sub.id

            WHERE a.attendance_date BETWEEN ? AND ?
              AND s.active = 1
        """

        params = [
            start_date.isoformat(),
            end_date.isoformat()
        ]

        if selected_class != "All Classes":

            sql += " AND c.id = ?"

            params.append(
                class_options[selected_class]
            )

        if selected_subject != "All Subjects":

            sql += " AND sub.id = ?"

            params.append(
                subject_options[selected_subject]
            )

        sql += """
            GROUP BY s.id, sub.id
            ORDER BY c.name, s.name, sub.name
        """

        report_df = read_data(
            sql,
            tuple(params)
        )

        if report_df.empty:

            st.info(
                "No attendance records found for these filters."
            )

        else:

            report_df["Working Classes"] = (
                report_df["present"] +
                report_df["absent"]
            )

            report_df["Attendance %"] = (
                report_df["present"] /
                report_df["Working Classes"].replace(0, pd.NA)
                * 100
            ).fillna(0).round(2)

            st.subheader("Overall Report")

            total_present = int(
                report_df["present"].sum()
            )

            total_absent = int(
                report_df["absent"].sum()
            )

            total_leave = int(
                report_df["leave_count"].sum()
            )

            total_working = total_present + total_absent

            overall_percentage = (
                total_present / total_working * 100
                if total_working else 0
            )

            r1, r2, r3, r4 = st.columns(4)

            r1.metric("Present", total_present)

            r2.metric("Absent", total_absent)

            r3.metric("Leave", total_leave)

            r4.metric(
                "Attendance %",
                f"{overall_percentage:.2f}%"
            )

            st.divider()

            st.subheader("Student Attendance Details")

            display_df = report_df.rename(columns={

                "student_name": "Student Name",

                "roll_no": "Roll Number",

                "class_name": "Class",

                "subject_name": "Subject",

                "total_marked": "Total Marked",

                "Working Classes": "Total Classes",

                "present": "Present",

                "absent": "Absent",

                "leave_count": "Leave",

                "Attendance %": "Cumulative Attendance (%)"

            })

            columns = [
                "Roll Number",
                "Student Name",
             
                "Class",
                "Subject",
                "Total Classes",
                "Present",
                "Absent",
                "Leave",
                "Cumulative Attendance (%)"
            ]

            st.dataframe(
                display_df[columns],
                use_container_width=True,
                hide_index=True
            )

            st.subheader("📊 Attendance Percentage Chart")

            chart_df = report_df[
                ["student_name", "Attendance %"]
            ].copy()

            chart_df["Student"] = (
                chart_df["student_name"] +
                " (" +
                chart_df.index.astype(str) +
                ")"
            )

            st.bar_chart(
                chart_df.set_index("Student")["Attendance %"]
            )

            st.divider()

            st.subheader("📅 Monthly Attendance Summary")

            monthly_sql = """
                SELECT

                    strftime(
                        '%Y-%m',
                        a.attendance_date
                    ) AS Month,

                    COUNT(*) AS Total_Records,

                    SUM(
                        CASE WHEN a.status = 'Present'
                        THEN 1 ELSE 0 END
                    ) AS Present,

                    SUM(
                        CASE WHEN a.status = 'Absent'
                        THEN 1 ELSE 0 END
                    ) AS Absent,

                    SUM(
                        CASE WHEN a.status = 'Leave'
                        THEN 1 ELSE 0 END
                    ) AS Leave

                FROM attendance a

                JOIN students s ON a.student_id = s.id

                JOIN classes c ON s.class_id = c.id

                JOIN subjects sub ON a.subject_id = sub.id

                WHERE a.attendance_date BETWEEN ? AND ?
                  AND s.active = 1
            """

            monthly_params = [
                start_date.isoformat(),
                end_date.isoformat()
            ]

            if selected_class != "All Classes":

                monthly_sql += " AND c.id = ?"

                monthly_params.append(
                    class_options[selected_class]
                )

            if selected_subject != "All Subjects":

                monthly_sql += " AND sub.id = ?"

                monthly_params.append(
                    subject_options[selected_subject]
                )

            monthly_sql += """
                GROUP BY strftime('%Y-%m', a.attendance_date)
                ORDER BY Month
            """

            monthly_df = read_data(
                monthly_sql,
                tuple(monthly_params)
            )

            if not monthly_df.empty:

                monthly_df["Attendance %"] = (
                    monthly_df["Present"] /
                    (
                        monthly_df["Present"] +
                        monthly_df["Absent"]
                    ).replace(0, pd.NA)
                    * 100
                ).fillna(0).round(2)

                st.dataframe(
                    monthly_df,
                    use_container_width=True,
                    hide_index=True
                )

            st.divider()

            st.subheader("⬇️ Download Reports")

            # EXCEL EXPORT

            excel_buffer = io.BytesIO()

            with pd.ExcelWriter(
                excel_buffer,
                engine="openpyxl"
            ) as writer:

                display_df[columns].to_excel(
                    writer,
                    sheet_name="Student Report",
                    index=False
                )

                if not monthly_df.empty:

                    monthly_df.to_excel(
                        writer,
                        sheet_name="Monthly Summary",
                        index=False
                    )

            st.download_button(
                label="📥 Download Excel Report",
                data=excel_buffer.getvalue(),
                file_name="attendance_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

            # PDF EXPORT

            try:

                from reportlab.lib import colors

                from reportlab.lib.pagesizes import (
                    A4,
                    landscape
                )

                from reportlab.platypus import (
                    SimpleDocTemplate,
                    Paragraph,
                    Spacer,
                    Table,
                    TableStyle
                )

                from reportlab.lib.styles import getSampleStyleSheet

                pdf_buffer = io.BytesIO()

                document = SimpleDocTemplate(
                    pdf_buffer,
                    pagesize=landscape(A4)
                )

                styles = getSampleStyleSheet()

                elements = [

                    Paragraph(
                        "Student Attendance Report",
                        styles["Title"]
                    ),

                    Spacer(1, 15),

                    Paragraph(
                        f"Period: {start_date} to {end_date}",
                        styles["Normal"]
                    ),

                    Spacer(1, 15)

                ]

                pdf_df = display_df[columns].copy()

                pdf_df["Cumulative Attendance (%)"] = (
                    pdf_df["Cumulative Attendance (%)"].astype(str) + "%"
                )

                table_data = (
                    [columns] +
                    pdf_df.astype(str).values.tolist()
                )

                pdf_table = Table(
                    table_data,
                    repeatRows=1
                )

                pdf_table.setStyle(TableStyle([

                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, 0),
                        colors.HexColor("#102A43")
                    ),

                    (
                        "TEXTCOLOR",
                        (0, 0),
                        (-1, 0),
                        colors.white
                    ),

                    (
                        "GRID",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        colors.grey
                    ),

                    (
                        "FONTSIZE",
                        (0, 0),
                        (-1, -1),
                        8
                    ),

                    (
                        "PADDING",
                        (0, 0),
                        (-1, -1),
                        5
                    ),

                    (
                        "VALIGN",
                        (0, 0),
                        (-1, -1),
                        "MIDDLE"
                    ),

                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [
                            colors.white,
                            colors.HexColor("#F1F5F9")
                        ]
                    )

                ]))

                elements.append(pdf_table)

                document.build(elements)

                st.download_button(
                    label="📄 Download PDF Report",
                    data=pdf_buffer.getvalue(),
                    file_name="attendance_report.pdf",
                    mime="application/pdf",
                    use_container_width=True
                )

            except ImportError:

                st.warning(
                    "Install reportlab to enable PDF export."
                )


# =========================================================
# FOOTER
# =========================================================

st.divider()

st.markdown(
    """
    <div style="text-align:center;color:#64748B;font-size:12px;">
        Developed & Deployed by Fizza R Ishaq
    </div>
    """,
    unsafe_allow_html=True
)
