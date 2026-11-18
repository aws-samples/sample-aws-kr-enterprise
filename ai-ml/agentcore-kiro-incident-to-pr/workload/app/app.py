import os
import socket
from datetime import datetime, timezone

import psycopg2
from flask import Flask, jsonify

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "sampleapp")
DB_USER = os.environ.get("DB_USER", "sampleapp")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")


@app.route("/health")
def health():
    db_status = "connected"
    try:
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD, connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
    except Exception:
        db_status = "disconnected"

    status = "healthy" if db_status == "connected" else "unhealthy"
    code = 200 if status == "healthy" else 503
    return jsonify({
        "status": status,
        "database": db_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
    }), code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
