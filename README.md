# Smart Ticket Management System

A Flask web app where employees submit support tickets, an ML model classifies them by department, and admins/managers can view and manage them.

---

## Why This Was Modified

### Critical Fixes

| Issue | Fix |
|-------|-----|
| Two competing databases (SQLite + PostgreSQL out of sync) | Unified under a single SQLite database |
| Passwords stored as plaintext on Postgres side | All passwords hashed with `check_password_hash` |
| Hardcoded `secret_key` in source code | Loaded from `.env` environment variable |
| Admin role assigned to anyone with username "admin" | Admin seeded in DB at startup with hashed password |
| `login_required` redirected to POST-only `/login` causing 405 | Rebuilt with `functools.wraps`, redirects correctly |
| Login never set `session` — all protected routes broke | Session properly set on successful login |

**Also fixed:** missing packages in `requirements.txt`, bare `except:` replaced with `sqlite3.IntegrityError`, duplicate import removed, `database.db` and `.env` added to `.gitignore`.

---

## How to Run It

### Step 1 — Clone the repo
```bash
git clone https://github.com/Serero-Codes/Smart-Ticket-Management.git
cd Smart-Ticket-Management
```

### Step 2 — Create a virtual environment

**Windows (PowerShell):**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```
> If you get a permissions error: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

**Mac / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3 — Install dependencies
```bash
pip install -r requirements.txt
```

### Step 4 — Create your .env file
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
Create `.env` in the project root:
```
SECRET_KEY=paste-your-generated-key-here
DATABASE_PATH=database.db
```

### Step 5 — Train the ML model
```bash
python train_model.py
```

### Step 6 — Start the app
```bash
python app.py
```
Open: `http://127.0.0.1:5000`

**Default admin login:** `admin@company.com` / `admin123` — change after first login.

---

## Deploying to Render

| Field | Value |
|-------|-------|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app` |
| Env: `SECRET_KEY` | your generated key |
| Env: `DATABASE_PATH` | `database.db` |
