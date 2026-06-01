from flask import Flask, render_template, request, redirect, session, flash, jsonify
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from DB.queries import authenticate_user
from classifier import classify_ticket

from classifier import classify_ticket

app = Flask(__name__)
app.secret_key = "super_secret_key"

DATABASE = "database.db"


# =========================
# DATABASE INITIALIZATION
# =========================

def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # USERS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            department TEXT,
            role TEXT
        )
    """)

    # TICKETS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT,
            department TEXT,
            ticket_text TEXT,
            category TEXT,
            confidence REAL,
            status TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


@app.route('/')
def home():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    print("REQUEST:", data)

    user = authenticate_user(email, password)

    print("AUTH RESULT:", user)

    if user:
        return jsonify({
            "success": True,
            "employee_id": user["employee_id"],
            "name": user["first_name"],
            "redirect": "/index"
        })

    return jsonify({
        "success": False,
        "message": "Invalid email or password"
    }), 401

@app.route('/index')
def index():
    return render_template('index.html')


# =========================
# LOGIN REQUIRED DECORATOR
# =========================

def login_required(route_function):
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        return route_function(*args, **kwargs)

    wrapper.__name__ = route_function.__name__
    return wrapper


# =========================
# REGISTER
# =========================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        username = request.form["username"]
        password = request.form["password"]
        department = request.form["department"]

        hashed_password = generate_password_hash(password)

        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()

        try:

            role = "user"

            if username.lower() == "admin":
                role = "admin"

            cursor.execute("""
                INSERT INTO users (
                    username,
                    password,
                    department,
                    role
                )
                VALUES (?, ?, ?, ?)
            """, (
                username,
                hashed_password,
                department,
                role
            ))

            conn.commit()

            flash("Registration successful!")
            return redirect("/login")

        except:
            flash("Username already exists.")

        finally:
            conn.close()

    return render_template("register.html")


# =========================
# LOGIN
# =========================

# @app.route("/login", methods=["GET", "POST"])
# def login():

#     if request.method == "POST":

#         username = request.form["username"]
#         password = request.form["password"]

#         conn = sqlite3.connect(DATABASE)
#         cursor = conn.cursor()

#         cursor.execute("""
#             SELECT * FROM users
#             WHERE username = ?
#         """, (username,))

#         user = cursor.fetchone()

#         conn.close()

#         if user and check_password_hash(user[2], password):

#             session["user_id"] = user[0]
#             session["username"] = user[1]
#             session["department"] = user[3]
#             session["role"] = user[4]

#             return redirect("/")

#         flash("Invalid credentials.")

#     return render_template("login.html")


# =========================
# LOGOUT
# =========================

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/login")


# =========================
# HOME PAGE
# =========================

# @app.route("/")
# # @login_required
# def home():

#     return render_template(
#         "index.html",
#         username=session["username"],
#         department=session["department"]
#     )


# =========================
# SUBMIT TICKET
# =========================

@app.route("/submit", methods=["POST"])
@login_required
def submit_ticket():

    employee_name = session["username"]
    department = session["department"]

    ticket_text = request.form["ticket_text"]

    category, confidence = classify_ticket(ticket_text)

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO tickets (
            employee_name,
            department,
            ticket_text,
            category,
            confidence,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        employee_name,
        department,
        ticket_text,
        category,
        confidence,
        "Open",
        created_at
    ))

    conn.commit()
    conn.close()

    return redirect("/history")


# =========================
# USER TICKET HISTORY
# =========================

@app.route("/history")
@login_required
def history():

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM tickets
        WHERE employee_name = ?
        ORDER BY id DESC
    """, (session["username"],))

    tickets = cursor.fetchall()

    conn.close()

    return render_template("history.html", tickets=tickets)


# =========================
# DEPARTMENT TICKETS
# =========================

@app.route("/department")
@login_required
def department_tickets():

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    if session["role"] == "admin":

        cursor.execute("""
            SELECT * FROM tickets
            ORDER BY id DESC
        """)

    else:

        cursor.execute("""
            SELECT * FROM tickets
            WHERE category = ?
            ORDER BY id DESC
        """, (session["department"],))

    tickets = cursor.fetchall()

    conn.close()

    return render_template(
        "department.html",
        tickets=tickets
    )


# =========================
# UPDATE TICKET STATUS
# =========================

@app.route("/update_status/<int:ticket_id>", methods=["POST"])
@login_required
def update_status(ticket_id):

    new_status = request.form["status"]

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE tickets
        SET status = ?
        WHERE id = ?
    """, (
        new_status,
        ticket_id
    ))

    conn.commit()
    conn.close()

    return redirect("/department")


# =========================
# ADMIN DASHBOARD
# =========================

@app.route("/dashboard")
@login_required
def dashboard():

    if session["role"] != "admin":
        return redirect("/")

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM tickets
        ORDER BY id DESC
    """)

    tickets = cursor.fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        tickets=tickets
    )


# =========================
# RUN APP
# =========================

if __name__ == "__main__":
    app.run(debug=True)