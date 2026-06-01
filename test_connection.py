from DB.connection import get_connection

try:
    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute("SELECT version();")
        version = cur.fetchone()

    print("✅ Database connection successful")
    print(version)

    conn.close()

except Exception as e:
    print("❌ Database connection failed")
    print(e)