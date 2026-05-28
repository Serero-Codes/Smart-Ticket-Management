from flask import Flask, render_template, request, redirect
import sqlite3
from datetime import datetime
from classifier import classify_ticket

app = Flask(__name__)

DATABASE = 'database.db'


# Create database table

def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT,
            department TEXT,
            ticket_text TEXT,
            category TEXT,
            confidence REAL,
            created_at TEXT
        )
    ''')

    conn.commit()
    conn.close()


init_db()


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/submit', methods=['POST'])
def submit_ticket():
    employee_name = request.form['employee_name']
    department = request.form['department']
    ticket_text = request.form['ticket_text']

    category, confidence = classify_ticket(ticket_text)

    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO tickets (
            employee_name,
            department,
            ticket_text,
            category,
            confidence,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        employee_name,
        department,
        ticket_text,
        category,
        confidence,
        created_at
    ))

    conn.commit()
    conn.close()

    return redirect('/dashboard')


@app.route('/dashboard')
def dashboard():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM tickets ORDER BY id DESC')
    tickets = cursor.fetchall()

    conn.close()

    return render_template('dashboard.html', tickets=tickets)


if __name__ == '__main__':
    app.run(debug=True)