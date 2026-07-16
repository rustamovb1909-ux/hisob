# -*- coding: utf-8 -*-
"""
Hisobchi — Flask backend
- Telegram WebApp initData tekshiruvi (xavfsizlik)
- Faqat botda ro'yxatdan o'tgan (raqam yuborgan) userlar API dan foydalana oladi
- PostgreSQL (Render Postgres) bilan ishlaydi
"""
import os
import hmac
import hashlib
import json
from urllib.parse import parse_qsl

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, render_template

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")  # masalan: mening_hisobchi_bot (@ belgisisiz)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Ma'lumotlar bazasi
# ---------------------------------------------------------------------------
def get_conn():
    """Har chaqiriqda yangi ulanish (Render'da connection pool shart emas,
    lekin xohlasangiz keyinchalik psycopg2.pool ga o'tkazish oson)."""
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            phone TEXT NOT NULL,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
            amount NUMERIC NOT NULL,
            category TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(telegram_id)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            direction TEXT NOT NULL CHECK (direction IN ('given', 'taken')),
            person_name TEXT NOT NULL,
            amount NUMERIC NOT NULL,
            note TEXT DEFAULT '',
            is_paid BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            paid_at TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_debts_user ON debts(telegram_id)")
    conn.commit()
    cur.close()
    conn.close()


def get_user(telegram_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def upsert_user(telegram_id, phone, first_name, last_name, username):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (telegram_id, phone, first_name, last_name, username)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (telegram_id) DO UPDATE
        SET phone = EXCLUDED.phone,
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            username = EXCLUDED.username
    """, (telegram_id, phone, first_name, last_name, username))
    conn.commit()
    cur.close()
    conn.close()


def get_transactions(telegram_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM transactions WHERE telegram_id = %s ORDER BY created_at DESC",
        (telegram_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def add_transaction(telegram_id, type_, amount, category, note):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        INSERT INTO transactions (telegram_id, type, amount, category, note)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
    """, (telegram_id, type_, amount, category, note))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row


def get_transaction(telegram_id, tx_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM transactions WHERE id = %s AND telegram_id = %s",
        (tx_id, telegram_id)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def delete_transaction(telegram_id, tx_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM transactions WHERE id = %s AND telegram_id = %s",
        (tx_id, telegram_id)
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return deleted > 0


def update_transaction(telegram_id, tx_id, type_, amount, category, note):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        UPDATE transactions
        SET type = %s, amount = %s, category = %s, note = %s
        WHERE id = %s AND telegram_id = %s
        RETURNING *
    """, (type_, amount, category, note, tx_id, telegram_id))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row


def get_monthly_summary(telegram_id):
    """Joriy oy (UTC bo'yicha) uchun kirim/xarajat yig'indisi va xarajat
    kategoriyalari bo'yicha taqsimotni qaytaradi. Bot orqali "Oylik hisobot"
    tugmasi bosilganda ishlatiladi."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT type, category, SUM(amount) AS total
        FROM transactions
        WHERE telegram_id = %s
          AND date_trunc('month', created_at) = date_trunc('month', NOW())
        GROUP BY type, category
        ORDER BY total DESC
    """, (telegram_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    income = 0.0
    expense = 0.0
    categories = []
    for r in rows:
        total = float(r["total"])
        if r["type"] == "income":
            income += total
        else:
            expense += total
            categories.append((r["category"], total))

    return {
        "income": income,
        "expense": expense,
        "balance": income - expense,
        "categories": categories,
    }


# ---------------------------------------------------------------------------
# Qarz daftari
# ---------------------------------------------------------------------------
def get_debts(telegram_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM debts WHERE telegram_id = %s ORDER BY is_paid ASC, created_at DESC",
        (telegram_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def add_debt(telegram_id, direction, person_name, amount, note):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        INSERT INTO debts (telegram_id, direction, person_name, amount, note)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
    """, (telegram_id, direction, person_name, amount, note))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row


def get_debt(telegram_id, debt_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM debts WHERE id = %s AND telegram_id = %s",
        (debt_id, telegram_id)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def update_debt(telegram_id, debt_id, person_name, amount, note):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        UPDATE debts
        SET person_name = %s, amount = %s, note = %s
        WHERE id = %s AND telegram_id = %s
        RETURNING *
    """, (person_name, amount, note, debt_id, telegram_id))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row


def set_debt_paid(telegram_id, debt_id, is_paid):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        UPDATE debts
        SET is_paid = %s, paid_at = CASE WHEN %s THEN NOW() ELSE NULL END
        WHERE id = %s AND telegram_id = %s
        RETURNING *
    """, (is_paid, is_paid, debt_id, telegram_id))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row


def delete_debt(telegram_id, debt_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM debts WHERE id = %s AND telegram_id = %s",
        (debt_id, telegram_id)
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return deleted > 0


# ---------------------------------------------------------------------------
# Telegram WebApp initData tekshiruvi
# https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
# ---------------------------------------------------------------------------
def verify_init_data(init_data: str):
    """initData imzosini tekshiradi. To'g'ri bo'lsa (dict user, ...) qaytaradi,
    noto'g'ri yoki bo'sh bo'lsa None qaytaradi."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(pairs.items())
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None

    return user


def require_auth():
    """Har bir API so'rovi uchun: initData -> telegram user.
    Qaytaradi: (user_dict, None) yoki (None, (response, status))"""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = verify_init_data(init_data)
    if user is None:
        return None, (jsonify({"error": "Telegram orqali kiring"}), 401)
    return user, None


def require_registered():
    """initData to'g'ri VA foydalanuvchi botda ro'yxatdan o'tgan bo'lishi kerak."""
    user, err = require_auth()
    if err:
        return None, err
    db_user = get_user(user["id"])
    if not db_user:
        return None, (jsonify({
            "error": "not_registered",
            "message": "Avval botda ro'yxatdan o'ting"
        }), 403)
    return user, None


# ---------------------------------------------------------------------------
# Sahifalar
# ---------------------------------------------------------------------------
def to_utc_iso(dt):
    """PostgreSQL NOW() qiymati UTC bo'yicha saqlanadi, lekin "timezone
    yo'q" (naive) holatda qaytadi. Bu funksiya ISO satr oxiriga aniq "Z"
    (UTC) belgisini qo'shadi — shunda brauzer sanani noto'g'ri (o'zining
    mahalliy vaqti deb) talqin qilib, kunni siljitib yubormaydi."""
    return dt.isoformat() + "Z"


@app.route("/")
def index():
    return render_template("index.html", bot_username=BOT_USERNAME)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.route("/api/me")
def api_me():
    user, err = require_auth()
    if err:
        return err
    db_user = get_user(user["id"])
    if not db_user:
        return jsonify({"registered": False}), 200
    return jsonify({
        "registered": True,
        "first_name": db_user["first_name"] or user.get("first_name", ""),
        "phone": db_user["phone"],
    }), 200


@app.route("/api/transactions", methods=["GET"])
def api_list_transactions():
    user, err = require_registered()
    if err:
        return err
    rows = get_transactions(user["id"])
    result = [{
        "id": r["id"],
        "type": r["type"],
        "amount": float(r["amount"]),
        "category": r["category"],
        "note": r["note"],
        "created_at": to_utc_iso(r["created_at"]),
    } for r in rows]
    return jsonify(result)


@app.route("/api/transactions", methods=["POST"])
def api_add_transaction():
    user, err = require_registered()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    type_ = data.get("type")
    amount = data.get("amount")
    category = (data.get("category") or "").strip()[:100]
    note = (data.get("note") or "").strip()[:200]

    if type_ not in ("income", "expense"):
        return jsonify({"error": "noto'g'ri turi"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "noto'g'ri summa"}), 400
    if amount <= 0:
        return jsonify({"error": "summa 0 dan katta bo'lishi kerak"}), 400
    if type_ == "expense" and not category:
        return jsonify({"error": "kategoriya kerak"}), 400

    row = add_transaction(user["id"], type_, amount, category or "kirim", note)
    return jsonify({
        "id": row["id"],
        "type": row["type"],
        "amount": float(row["amount"]),
        "category": row["category"],
        "note": row["note"],
        "created_at": to_utc_iso(row["created_at"]),
    }), 201


@app.route("/api/transactions/<int:tx_id>", methods=["PUT"])
def api_update_transaction(tx_id):
    user, err = require_registered()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    type_ = data.get("type")
    amount = data.get("amount")
    category = (data.get("category") or "").strip()[:100]
    note = (data.get("note") or "").strip()[:200]

    if type_ not in ("income", "expense"):
        return jsonify({"error": "noto'g'ri turi"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "noto'g'ri summa"}), 400
    if amount <= 0:
        return jsonify({"error": "summa 0 dan katta bo'lishi kerak"}), 400
    if type_ == "expense" and not category:
        return jsonify({"error": "kategoriya kerak"}), 400

    row = update_transaction(user["id"], tx_id, type_, amount, category or "kirim", note)
    if not row:
        return jsonify({"error": "topilmadi"}), 404

    return jsonify({
        "id": row["id"],
        "type": row["type"],
        "amount": float(row["amount"]),
        "category": row["category"],
        "note": row["note"],
        "created_at": to_utc_iso(row["created_at"]),
    }), 200


@app.route("/api/transactions/<int:tx_id>", methods=["DELETE"])
def api_delete_transaction(tx_id):
    user, err = require_registered()
    if err:
        return err
    ok = delete_transaction(user["id"], tx_id)
    if not ok:
        return jsonify({"error": "topilmadi"}), 404
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# API — Qarz daftari
# ---------------------------------------------------------------------------
def serialize_debt(row):
    return {
        "id": row["id"],
        "direction": row["direction"],
        "person_name": row["person_name"],
        "amount": float(row["amount"]),
        "note": row["note"],
        "is_paid": row["is_paid"],
        "created_at": to_utc_iso(row["created_at"]),
        "paid_at": to_utc_iso(row["paid_at"]) if row["paid_at"] else None,
    }


@app.route("/api/debts", methods=["GET"])
def api_list_debts():
    user, err = require_registered()
    if err:
        return err
    rows = get_debts(user["id"])
    return jsonify([serialize_debt(r) for r in rows])


@app.route("/api/debts", methods=["POST"])
def api_add_debt():
    user, err = require_registered()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    direction = data.get("direction")
    person_name = (data.get("person_name") or "").strip()[:100]
    amount = data.get("amount")
    note = (data.get("note") or "").strip()[:200]

    if direction not in ("given", "taken"):
        return jsonify({"error": "noto'g'ri turi"}), 400
    if not person_name:
        return jsonify({"error": "ism kiritilmagan"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "noto'g'ri summa"}), 400
    if amount <= 0:
        return jsonify({"error": "summa 0 dan katta bo'lishi kerak"}), 400

    row = add_debt(user["id"], direction, person_name, amount, note)
    return jsonify(serialize_debt(row)), 201


@app.route("/api/debts/<int:debt_id>", methods=["PUT"])
def api_update_debt(debt_id):
    user, err = require_registered()
    if err:
        return err

    existing = get_debt(user["id"], debt_id)
    if not existing:
        return jsonify({"error": "topilmadi"}), 404

    data = request.get_json(silent=True) or {}

    # Faqat "to'landi" holatini almashtirish so'ralgan bo'lishi mumkin
    if "is_paid" in data and len(data) == 1:
        row = set_debt_paid(user["id"], debt_id, bool(data["is_paid"]))
        return jsonify(serialize_debt(row))

    person_name = (data.get("person_name") or "").strip()[:100]
    amount = data.get("amount")
    note = (data.get("note") or "").strip()[:200]

    if not person_name:
        return jsonify({"error": "ism kiritilmagan"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "noto'g'ri summa"}), 400
    if amount <= 0:
        return jsonify({"error": "summa 0 dan katta bo'lishi kerak"}), 400

    row = update_debt(user["id"], debt_id, person_name, amount, note)
    if not row:
        return jsonify({"error": "topilmadi"}), 404
    return jsonify(serialize_debt(row))


@app.route("/api/debts/<int:debt_id>", methods=["DELETE"])
def api_delete_debt(debt_id):
    user, err = require_registered()
    if err:
        return err
    ok = delete_debt(user["id"], debt_id)
    if not ok:
        return jsonify({"error": "topilmadi"}), 404
    return jsonify({"success": True})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
