from DB.connection import get_connection

def authenticate_user(email, password):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT employee_id,
                       first_name,
                       last_name,
                       email,
                       password
                FROM public.employees
                WHERE email = %s
            """, (email,))

            user = cur.fetchone()

            print("EMAIL ENTERED:", email)
            print("USER FOUND:", user)

            if not user:
                print("NO USER FOUND")
                return None

            print("DB PASSWORD:", user['password'])
            print("INPUT PASSWORD:", password)

            if user['password'] == password:
                print("LOGIN SUCCESS")
                return user

            print("PASSWORD INCORRECT")
            return None

    finally:
        conn.close()