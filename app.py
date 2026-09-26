#!/usr/bin/env python3
"""
Central Táxi Cone Sul — sistema completo em Flask.
App do passageiro, agendamento familiar (até 4 pessoas da casa),
painel do motorista e painel administrativo.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import struct
import time
import traceback
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote

from flask import (
    Flask,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
    jsonify,
    send_from_directory,
    make_response,
)
from werkzeug.security import check_password_hash, generate_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(APP_DIR, "taxi.db"))
JWT_TTL_MIN = int(os.environ.get("JWT_TTL_MIN", "480"))
LOG_DIR = os.path.join(APP_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "erros.log")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cone-sul-taxi-2026-dev")

_REDIS = None
_MEM_CACHE = {}


def redis_client():
    global _REDIS
    if _REDIS is False:
        return None
    if _REDIS is not None:
        return _REDIS
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        _REDIS = False
        return None
    try:
        import redis
        cli = redis.from_url(url, socket_timeout=1.5, decode_responses=True)
        cli.ping()
        _REDIS = cli
        return cli
    except Exception:
        _REDIS = False
        return None


def cache_get(key):
    r = redis_client()
    if r is not None:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    hit = _MEM_CACHE.get(key)
    if not hit:
        return None
    val, exp = hit
    if exp < time.time():
        _MEM_CACHE.pop(key, None)
        return None
    return val


def cache_set(key, value, ttl=25):
    r = redis_client()
    if r is not None:
        r.setex(key, ttl, json.dumps(value))
        return
    _MEM_CACHE[key] = (value, time.time() + ttl)


def cache_bust(prefix="dash"):
    r = redis_client()
    if r is not None:
        for key in r.scan_iter(f"{prefix}*"):
            r.delete(key)
        return
    for key in list(_MEM_CACHE):
        if key.startswith(prefix):
            _MEM_CACHE.pop(key, None)


@app.route("/sw.js")
def pwa_sw():
    resp = make_response(send_from_directory(os.path.join(APP_DIR, "static", "js"), "sw.js"))
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/manifest.webmanifest")
def pwa_manifest():
    resp = make_response(send_from_directory(os.path.join(APP_DIR, "static"), "manifest.webmanifest"))
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.route("/pwa")
def pwa_lab():
    return render_template("pwa_lab.html")


@app.route("/instalar")
def instalar_app():
    return render_template("instalar.html")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(APP_DIR, "static", "img"), "pwa-192.png", mimetype="image/png")


@app.route("/inicio")
def inicio():
    return redirect(url_for("home"))


@app.after_request
def cache_map_assets(resp):
    path = request.path or ""
    if path.startswith("/static/vendor/") or path.startswith("/static/css/") or path.startswith("/static/js/"):
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return resp

os.makedirs(LOG_DIR, exist_ok=True)
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
)
error_logger = logging.getLogger("conesul.erros")
error_logger.setLevel(logging.INFO)
if not error_logger.handlers:
    error_logger.addHandler(_file_handler)
    error_logger.addHandler(logging.StreamHandler())


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql: str, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql: str, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def init_schema():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','motorista','passageiro')),
            address TEXT DEFAULT '',
            house_label TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            label TEXT NOT NULL,
            distance_km REAL NOT NULL,
            price REAL NOT NULL,
            duration_min INTEGER NOT NULL,
            map_from TEXT,
            map_to TEXT
        );

        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
            route_id INTEGER REFERENCES routes(id),
            plate TEXT,
            car_model TEXT,
            color TEXT DEFAULT '#2563eb',
            initials TEXT,
            status TEXT DEFAULT 'ativo' CHECK(status IN ('ativo','bloqueado','suspenso')),
            payment_status TEXT DEFAULT 'pendente' CHECK(payment_status IN ('pago','pendente')),
            monthly_fee REAL DEFAULT 50.00
        );

        CREATE TABLE IF NOT EXISTS driver_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL REFERENCES drivers(id),
            kind TEXT NOT NULL,
            number TEXT DEFAULT '',
            expires_at TEXT DEFAULT '',
            status TEXT DEFAULT 'pendente',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS rides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            passenger_id INTEGER NOT NULL REFERENCES users(id),
            driver_id INTEGER REFERENCES drivers(id),
            route_id INTEGER NOT NULL REFERENCES routes(id),
            kind TEXT NOT NULL CHECK(kind IN ('imediata','agendada')),
            status TEXT NOT NULL DEFAULT 'pendente'
                CHECK(status IN ('pendente','aceita','a_caminho','em_andamento','concluida','cancelada')),
            pickup_address TEXT NOT NULL,
            house_label TEXT DEFAULT '',
            scheduled_at TEXT,
            seats INTEGER DEFAULT 1,
            price REAL NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS ride_passengers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ride_id INTEGER NOT NULL REFERENCES rides(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            document TEXT DEFAULT '',
            is_requester INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL REFERENCES drivers(id),
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pendente',
            paid_at TEXT
        );
        """
    )
    db.commit()
    db.close()
    migrate_security()
    migrate_driver_status()
    ensure_indexes()
    migrate_share()
    migrate_parcels()
    migrate_push()


def ensure_indexes():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_rides_status ON rides(status);
        CREATE INDEX IF NOT EXISTS idx_rides_route ON rides(route_id, status);
        CREATE INDEX IF NOT EXISTS idx_rides_driver ON rides(driver_id, status);
        CREATE INDEX IF NOT EXISTS idx_rides_passenger ON rides(passenger_id);
        CREATE INDEX IF NOT EXISTS idx_drivers_route ON drivers(route_id, status);
        CREATE INDEX IF NOT EXISTS idx_drivers_pay ON drivers(payment_status, status);
        CREATE INDEX IF NOT EXISTS idx_docs_driver ON driver_documents(driver_id, kind);
        CREATE INDEX IF NOT EXISTS idx_notes_user ON notifications(user_id, read_at);
        CREATE INDEX IF NOT EXISTS idx_notes_kind ON notifications(kind, created_at);
        CREATE INDEX IF NOT EXISTS idx_geo_ride ON geofences(ride_id, active);
        CREATE INDEX IF NOT EXISTS idx_geo_events ON geofence_events(ride_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_pay_month ON payments(driver_id, year, month);
        CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action, created_at);
        CREATE INDEX IF NOT EXISTS idx_users_role ON users(role, active);
        """
    )
    db.commit()
    db.close()


def migrate_share():
    db = sqlite3.connect(DB_PATH)
    cols = {r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    if "referral_code" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
    if "referred_by" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    if "ride_credit" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN ride_credit REAL DEFAULT 0")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            inviter_id INTEGER NOT NULL,
            guest_id INTEGER NOT NULL,
            bonus REAL DEFAULT 10,
            status TEXT DEFAULT 'pago'
        );
        CREATE TABLE IF NOT EXISTS traffic_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER,
            path TEXT,
            utm_source TEXT,
            utm_medium TEXT,
            utm_campaign TEXT,
            http_referrer TEXT,
            ip TEXT
        );
        """
    )
    db.commit()
    db.close()


def migrate_parcels():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS parcels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            sender_id INTEGER NOT NULL,
            driver_id INTEGER,
            route_id INTEGER,
            status TEXT DEFAULT 'pendente',
            pickup_address TEXT NOT NULL,
            delivery_address TEXT NOT NULL,
            recipient_name TEXT NOT NULL,
            recipient_phone TEXT DEFAULT '',
            description TEXT DEFAULT '',
            size TEXT DEFAULT 'media',
            price REAL DEFAULT 0,
            notes TEXT DEFAULT ''
        );
        """
    )
    db.commit()
    db.close()


def migrate_push():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT,
            auth TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    db.commit()
    db.close()


def ensure_vapid():
    pub = (setting("vapid_public", "") or os.environ.get("VAPID_PUBLIC_KEY", "")).strip()
    priv = (setting("vapid_private", "") or os.environ.get("VAPID_PRIVATE_KEY", "")).strip()
    if pub and priv:
        return pub, priv
    try:
        from py_vapid import Vapid
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from base64 import urlsafe_b64encode
        v = Vapid()
        v.generate_keys()
        priv = v.private_pem().decode("utf-8") if isinstance(v.private_pem(), bytes) else str(v.private_pem())
        raw = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        pub = urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        set_setting("vapid_private", priv)
        set_setting("vapid_public", pub)
        return pub, priv
    except Exception:
        return "", ""


def vapid_public_b64():
    pub, _ = ensure_vapid()
    return (pub or "").strip()


def send_web_push(user_id, title, body):
    rows = query("SELECT * FROM push_subscriptions WHERE user_id=?", (user_id,))
    if not rows:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        return 0
    pub, priv = ensure_vapid()
    if not priv:
        return 0
    mail = setting("vapid_mail", "mailto:beto@conesul.taxi") or "mailto:beto@conesul.taxi"
    n = 0
    for row in rows:
        sub = {
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body, "url": "/"}),
                vapid_private_key=priv,
                vapid_claims={"sub": mail},
            )
            n += 1
        except Exception:
            try:
                execute("DELETE FROM push_subscriptions WHERE id=?", (row["id"],))
            except Exception:
                pass
    return n


def share_bonus_value():
    try:
        return max(0.0, float(setting("share_bonus", "10") or 10))
    except (TypeError, ValueError):
        return 10.0


def make_referral_code(user_id: int, name: str = "") -> str:
    base = "".join(ch for ch in (name or "").upper() if ch.isalpha())[:4] or "TAXI"
    return f"{base}{user_id:03d}"


def ensure_referral_code(user):
    if not user:
        return ""
    code = (user["referral_code"] or "").strip() if "referral_code" in user.keys() else ""
    if code:
        return code
    code = make_referral_code(user["id"], user["name"])
    execute("UPDATE users SET referral_code=? WHERE id=?", (code, user["id"]))
    return code


def share_daily_limit():
    try:
        return max(1, int(setting("share_daily_limit", "5") or 5))
    except (TypeError, ValueError):
        return 5


def track_traffic(user_id=None, path=""):
    src = (request.cookies.get("utm_source") or request.args.get("utm_source") or "")[:80]
    med = (request.cookies.get("utm_medium") or request.args.get("utm_medium") or "")[:80]
    camp = (request.cookies.get("utm_campaign") or request.args.get("utm_campaign") or "")[:80]
    ref = (request.cookies.get("land_ref") or request.referrer or "")[:250]
    try:
        execute(
            """INSERT INTO traffic_hits (user_id, path, utm_source, utm_medium, utm_campaign, http_referrer, ip)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, path or request.path, src, med, camp, ref, client_ip()),
        )
    except Exception:
        pass


def apply_referral(guest_id: int, code: str):
    code = (code or "").strip().upper()
    if not code:
        return False
    host = query("SELECT * FROM users WHERE upper(referral_code)=?", (code,), one=True)
    if not host or host["id"] == guest_id:
        return False
    already = query("SELECT id FROM referrals WHERE guest_id=?", (guest_id,), one=True)
    if already:
        return False
    used_today = query(
        """SELECT COUNT(*) AS c FROM referrals
           WHERE inviter_id=? AND date(created_at)=date('now','localtime')""",
        (host["id"],),
        one=True,
    )
    if int((used_today or {}).get("c") or 0) >= share_daily_limit():
        return False
    bonus = share_bonus_value()
    execute(
        "INSERT INTO referrals (inviter_id, guest_id, bonus) VALUES (?,?,?)",
        (host["id"], guest_id, bonus),
    )
    execute("UPDATE users SET referred_by=?, ride_credit=COALESCE(ride_credit,0)+? WHERE id=?", (host["id"], bonus, guest_id))
    execute("UPDATE users SET ride_credit=COALESCE(ride_credit,0)+? WHERE id=?", (bonus, host["id"]))
    push_notification(
        host["id"],
        "Os dois ganharam",
        f"Sua indicação entrou. Vocês dois receberam R$ {bonus:.2f} de crédito.",
        kind="indicacao",
        send_email=False,
    )
    push_notification(
        guest_id,
        "Os dois ganharam",
        f"Você usou o convite e ganhou R$ {bonus:.2f}. Quem chamou também ganhou.",
        kind="indicacao",
        send_email=False,
    )
    audit("indicacao", str(host["id"]), f"guest={guest_id} bonus={bonus}")
    return True


def migrate_driver_status():
    db = sqlite3.connect(DB_PATH)
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='drivers'").fetchone()
    create_sql = (sql[0] if sql else "") or ""
    if "suspenso" in create_sql:
        db.close()
        return
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS drivers_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
            route_id INTEGER REFERENCES routes(id),
            plate TEXT,
            car_model TEXT,
            color TEXT DEFAULT '#2563eb',
            initials TEXT,
            status TEXT DEFAULT 'ativo',
            payment_status TEXT DEFAULT 'pendente',
            monthly_fee REAL DEFAULT 50.00
        );
        INSERT INTO drivers_v2 (id,user_id,route_id,plate,car_model,color,initials,status,payment_status,monthly_fee)
        SELECT id,user_id,route_id,plate,car_model,color,initials,status,payment_status,monthly_fee FROM drivers;
        DROP TABLE drivers;
        ALTER TABLE drivers_v2 RENAME TO drivers;
        """
    )
    db.commit()
    db.close()
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS driver_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            number TEXT DEFAULT '',
            expires_at TEXT DEFAULT '',
            status TEXT DEFAULT 'pendente',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        )"""
    )
    db.commit()
    db.close()


def _has_column(db, table, col):
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    names = {r[1] for r in rows}
    return col in names


def migrate_security():
    """Cria tabelas e colunas do módulo de segurança em bases já existentes."""
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    extra_cols = {
        "failed_logins": "INTEGER DEFAULT 0",
        "locked_until": "TEXT",
        "last_login_at": "TEXT",
        "last_login_ip": "TEXT",
        "must_change_password": "INTEGER DEFAULT 0",
        "totp_secret": "TEXT",
        "totp_enabled": "INTEGER DEFAULT 0",
        "totp_backup": "TEXT",
        "email": "TEXT",
        "recovery_email": "TEXT",
    }
    for col, spec in extra_cols.items():
        if not _has_column(db, "users", col):
            db.execute(f"ALTER TABLE users ADD COLUMN {col} {spec}")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS security_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            actor_id INTEGER,
            actor_name TEXT,
            action TEXT NOT NULL,
            target TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            level TEXT DEFAULT 'info'
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            phone TEXT,
            ip TEXT,
            success INTEGER DEFAULT 0,
            reason TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS email_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            to_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            kind TEXT DEFAULT 'geral',
            user_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL,
            destination TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS error_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            level TEXT DEFAULT 'error',
            source TEXT DEFAULT '',
            message TEXT NOT NULL,
            detail TEXT DEFAULT '',
            path TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            user_id INTEGER,
            resolved INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS geofences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ride_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('casa','destino')),
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            radius_m INTEGER NOT NULL DEFAULT 200,
            last_state TEXT DEFAULT 'fora',
            last_event_at TEXT,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS geofence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            fence_id INTEGER,
            ride_id INTEGER,
            driver_id INTEGER,
            event TEXT NOT NULL,
            distance_m INTEGER,
            detail TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER NOT NULL,
            ride_id INTEGER,
            kind TEXT DEFAULT 'geral',
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            read_at TEXT
        );
        CREATE TABLE IF NOT EXISTS error_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            level TEXT DEFAULT 'crit',
            message TEXT NOT NULL,
            source TEXT DEFAULT '',
            destination TEXT DEFAULT '',
            status TEXT DEFAULT 'criado',
            seen INTEGER DEFAULT 0
        );
        """
    )
    defaults = {
        "max_fails": "5",
        "lock_minutes": "15",
        "session_minutes": "45",
        "min_password": "6",
        "alert_new_admin_login": "1",
        "require_2fa_admin": "0",
        "smtp_enabled": "0",
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_user": "",
        "smtp_pass": "",
        "smtp_from": "nao-responda@conesul.taxi",
        "smtp_from_name": "Central Taxi Cone Sul",
        "smtp_security": "starttls",
        "alert_crit_enabled": "1",
        "alert_email": "",
        "alert_cooldown_min": "15",
        "alert_notify_admins": "1",
        "alert_last_at": "",
        "slack_enabled": "0",
        "slack_webhook": "",
        "teams_enabled": "0",
        "teams_webhook": "",
        "geo_enabled": "1",
        "geo_radius_casa": "200",
        "geo_radius_destino": "400",
        "geo_auto_status": "1",
        "driver_push_enabled": "1",
        "driver_push_fila": "1",
        "driver_push_despacho": "1",
        "driver_push_email": "0",
        "antecedentes_api_url": "",
        "last_doc_remind": "",
        "ui_theme": "uber",
        "share_bonus": "10",
        "share_daily_limit": "5",
        "wa_enabled": "1",
        "wa_phone": "",
        "wa_token": "",
        "wa_phone_id": "",
        "wa_template": "",
        "vapid_public": "",
        "vapid_private": "",
        "vapid_mail": "mailto:beto@conesul.taxi",
    }
    for k, v in defaults.items():
        db.execute(
            "INSERT OR IGNORE INTO security_settings (key, value) VALUES (?,?)",
            (k, v),
        )
    n = db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    if n == 0:
        db.execute(
            """INSERT INTO audit_logs (actor_name, action, target, ip, detail, level)
               VALUES (?,?,?,?,?,?)""",
            (
                "Sistema",
                "modulo_seguranca_ativado",
                "central",
                "127.0.0.1",
                "Módulo de segurança iniciado com auditoria, bloqueio e sessão.",
                "info",
            ),
        )
    token_cols = {
        "purpose": "TEXT DEFAULT 'reset'",
        "selector": "TEXT",
        "attempts": "INTEGER DEFAULT 0",
        "used_at": "TEXT",
        "created_ip": "TEXT",
    }
    for col, spec in token_cols.items():
        if not _has_column(db, "reset_tokens", col):
            db.execute(f"ALTER TABLE reset_tokens ADD COLUMN {col} {spec}")
    out_cols = {
        "status": "TEXT DEFAULT 'fila'",
        "error": "TEXT",
    }
    for col, spec in out_cols.items():
        if not _has_column(db, "email_outbox", col):
            db.execute(f"ALTER TABLE email_outbox ADD COLUMN {col} {spec}")
    loc_user = {
        "home_lat": "REAL",
        "home_lng": "REAL",
    }
    for col, spec in loc_user.items():
        if not _has_column(db, "users", col):
            db.execute(f"ALTER TABLE users ADD COLUMN {col} {spec}")
    loc_drv = {
        "lat": "REAL",
        "lng": "REAL",
        "loc_updated_at": "TEXT",
    }
    for col, spec in loc_drv.items():
        if not _has_column(db, "drivers", col):
            db.execute(f"ALTER TABLE drivers ADD COLUMN {col} {spec}")
    loc_ride = {
        "pickup_lat": "REAL",
        "pickup_lng": "REAL",
        "approach_notified": "INTEGER DEFAULT 0",
    }
    for col, spec in loc_ride.items():
        if not _has_column(db, "rides", col):
            db.execute(f"ALTER TABLE rides ADD COLUMN {col} {spec}")
    db.execute(
        """UPDATE users SET email=COALESCE(NULLIF(email,''), ?),
           recovery_email=COALESCE(NULLIF(recovery_email,''), ?)
           WHERE phone='69999990000' AND role='admin'""",
        ("beto@conesul.taxi", "beto.recupera@conesul.taxi"),
    )
    db.commit()
    db.close()


def seed_if_empty():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    n = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if n:
        db.close()
        return

    routes = [
        ("Vilhena", "Cerejeiras", "Vilhena x Cerejeiras", 88, 180.00, 90),
        ("Vilhena", "Colorado do Oeste", "Vilhena x Colorado do Oeste", 72, 160.00, 75),
        ("Vilhena", "Cabixi", "Vilhena x Cabixi", 95, 200.00, 100),
        ("Vilhena", "Pimenteiras", "Vilhena x Pimenteiras", 110, 220.00, 115),
    ]
    for r in routes:
        db.execute(
            "INSERT INTO routes (origin,destination,label,distance_km,price,duration_min) VALUES (?,?,?,?,?,?)",
            r,
        )

    admin_hash = generate_password_hash("admin123")
    db.execute(
        """INSERT INTO users (name, phone, email, recovery_email, password_hash, role, address, house_label)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "Beto",
            "69999990000",
            "beto@conesul.taxi",
            "beto.recupera@conesul.taxi",
            admin_hash,
            "admin",
            "Central Táxi Cone Sul — Vilhena/RO",
            "Sede",
        ),
    )

    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Módulo de segurança
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def norm_email(value: str) -> str:
    return (value or "").strip().lower()


def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(norm_email(value)))


def find_user_by_login(identifier: str):
    raw = (identifier or "").strip()
    if not raw:
        return None
    if "@" in raw:
        mail = norm_email(raw)
        return query("SELECT * FROM users WHERE lower(email)=?", (mail,), one=True)
    phone = "".join(ch for ch in raw if ch.isdigit())
    if len(phone) < 10:
        return None
    return query("SELECT * FROM users WHERE phone=?", (phone,), one=True)


def find_user_for_recovery(identifier: str):
    raw = (identifier or "").strip()
    if not raw:
        return None
    if "@" in raw:
        mail = norm_email(raw)
        return query(
            "SELECT * FROM users WHERE lower(email)=? OR lower(recovery_email)=?",
            (mail, mail),
            one=True,
        )
    return find_user_by_login(raw)


def recovery_destination(user) -> str:
    rec = norm_email(user["recovery_email"] or "")
    if rec:
        return rec
    return norm_email(user["email"] or "")


def smtp_config() -> dict:
    def pick(env_key, setting_key, default=""):
        env = os.environ.get(env_key)
        if env:
            return env
        return setting(setting_key, default)

    enabled_env = os.environ.get("SMTP_HOST")
    return {
        "enabled": bool(enabled_env) or setting("smtp_enabled", "0") == "1",
        "host": pick("SMTP_HOST", "smtp_host"),
        "port": int(pick("SMTP_PORT", "smtp_port", "587") or 587),
        "user": pick("SMTP_USER", "smtp_user"),
        "password": pick("SMTP_PASS", "smtp_pass"),
        "mail_from": pick("SMTP_FROM", "smtp_from", "nao-responda@conesul.taxi"),
        "from_name": pick("SMTP_FROM_NAME", "smtp_from_name", "Central Taxi Cone Sul"),
        "security": (pick("SMTP_SECURITY", "smtp_security", "starttls") or "starttls").lower(),
    }


def send_smtp(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    cfg = smtp_config()
    if not cfg["enabled"] or not cfg["host"]:
        return False, "smtp_desligado"
    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{cfg['from_name']} <{cfg['mail_from']}>"
        msg["To"] = to_email
        msg.set_content(body)
        timeout = 18
        if cfg["security"] == "ssl":
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout)
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout)
        with smtp:
            smtp.ehlo()
            if cfg["security"] == "starttls":
                smtp.starttls()
                smtp.ehlo()
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
        return True, "enviado"
    except Exception as exc:
        return False, str(exc)[:240]


def queue_email(to_email: str, subject: str, body: str, kind="geral", user_id=None):
    oid = execute(
        """INSERT INTO email_outbox (to_email, subject, body, kind, user_id, status)
           VALUES (?,?,?,?,?,'fila')""",
        (to_email, subject, body, kind, user_id),
    )
    sent, info = send_smtp(to_email, subject, body)
    if sent:
        execute("UPDATE email_outbox SET status='enviado', error='' WHERE id=?", (oid,))
    elif info == "smtp_desligado":
        execute(
            "UPDATE email_outbox SET status='demo', error='SMTP desligado — e-mail só na caixa da central' WHERE id=?",
            (oid,),
        )
    else:
        execute("UPDATE email_outbox SET status='erro', error=? WHERE id=?", (info, oid))
        if kind != "alerta_critico":
            log_error(
                f"Falha SMTP para {to_email}",
                source="smtp",
                level="error",
                detail=info,
            )
    return sent, info


def hash_secret(value: str) -> str:
    return hmac.new(app.secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(user_id: int, destination: str, purpose="reset", minutes=30) -> str:
    execute(
        "UPDATE reset_tokens SET used=1, used_at=? WHERE user_id=? AND purpose=? AND used=0",
        (datetime.now().isoformat(timespec="seconds"), user_id, purpose),
    )
    selector = secrets.token_urlsafe(9)
    verifier = secrets.token_urlsafe(24)
    execute(
        """INSERT INTO reset_tokens
           (user_id, token_hash, destination, expires_at, purpose, selector, attempts, created_ip)
           VALUES (?,?,?,?,?,?,0,?)""",
        (
            user_id,
            hash_secret(verifier),
            destination,
            (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
            purpose,
            selector,
            client_ip(),
        ),
    )
    return f"{selector}.{verifier}"


def parse_token(raw: str) -> tuple[str, str] | tuple[None, None]:
    text = (raw or "").strip()
    if "." not in text:
        return None, None
    selector, verifier = text.split(".", 1)
    if len(selector) < 8 or len(verifier) < 16:
        return None, None
    return selector, verifier


def validate_token(raw: str, purpose="reset"):
    """Valida formato, propósito, validade, uso único e o segredo HMAC."""
    selector, verifier = parse_token(raw)
    if not selector:
        return None, "formato_invalido"
    row = query(
        "SELECT * FROM reset_tokens WHERE selector=? AND purpose=?",
        (selector, purpose),
        one=True,
    )
    if not row:
        return None, "inexistente"
    if row["used"]:
        return None, "ja_usado"
    try:
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            execute("UPDATE reset_tokens SET used=1, used_at=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), row["id"]))
            return None, "expirado"
    except ValueError:
        return None, "expirado"
    attempts = int(row["attempts"] or 0)
    if attempts >= 5:
        execute("UPDATE reset_tokens SET used=1, used_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), row["id"]))
        return None, "tentativas"
    if not hmac.compare_digest(row["token_hash"], hash_secret(verifier)):
        execute("UPDATE reset_tokens SET attempts=? WHERE id=?", (attempts + 1, row["id"]))
        return None, "invalido"
    return row, "ok"


def consume_token(row) -> None:
    execute(
        "UPDATE reset_tokens SET used=1, used_at=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), row["id"]),
    )


def client_ip():
    return (
        request.headers.get("X-Forwarded-For", request.remote_addr or "")
        .split(",")[0]
        .strip()
    )


def setting(key, default=""):
    try:
        row = query("SELECT value FROM security_settings WHERE key=?", (key,), one=True)
        return row["value"] if row else default
    except sqlite3.OperationalError:
        return default


def set_setting(key, value):
    execute(
        "INSERT INTO security_settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _b32_encode(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").replace("=", "")


def _b32_decode(text: str) -> bytes:
    clean = "".join(ch for ch in text.upper() if ch.isalnum())
    pad = "=" * ((8 - len(clean) % 8) % 8)
    return base64.b32decode(clean + pad)


def generate_totp_secret() -> str:
    return _b32_encode(secrets.token_bytes(20))


def totp_code(secret: str, for_time: float | None = None, drift: int = 0) -> str:
    counter = int((for_time if for_time is not None else time.time()) // 30) + drift
    digest = hmac.new(_b32_decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def totp_ok(secret: str, code: str) -> bool:
    digits = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(digits) != 6 or not secret:
        return False
    now = time.time()
    for drift in (-1, 0, 1):
        if hmac.compare_digest(totp_code(secret, now, drift), digits):
            return True
    return False


def make_backup_codes(n: int = 8) -> list[str]:
    return [secrets.token_hex(4).upper() for _ in range(n)]


def store_backup_codes(codes: list[str]) -> str:
    return json.dumps([generate_password_hash(c) for c in codes])


def consume_backup_code(stored_json: str, code: str) -> str | None:
    raw = (code or "").strip().replace(" ", "").upper()
    if not stored_json or len(raw) < 6:
        return None
    try:
        hashes = json.loads(stored_json)
    except json.JSONDecodeError:
        return None
    kept = []
    used = False
    for item in hashes:
        if not used and check_password_hash(item, raw):
            used = True
            continue
        kept.append(item)
    return json.dumps(kept) if used else None


def otpauth_uri(user, secret: str) -> str:
    label = quote(f"Cone Sul:{user['phone']}")
    issuer = quote("Central Taxi Cone Sul")
    return f"otpauth://totp/{label}?secret={secret}&issuer={issuer}&digits=6&period=30"


def finish_login(user):
    ip = client_ip()
    execute(
        """UPDATE users SET failed_logins=0, locked_until=NULL,
           last_login_at=?, last_login_ip=? WHERE id=?""",
        (datetime.now().isoformat(timespec="seconds"), ip, user["id"]),
    )
    session.clear()
    session["user_id"] = user["id"]
    session["csrf"] = secrets.token_hex(16)
    session["login_at"] = datetime.now().isoformat(timespec="seconds")
    session["last_seen"] = session["login_at"]
    session["2fa_ok"] = True
    audit("login", user["phone"], f"perfil={user['role']}", "info", actor=user)


DOC_REQUIRED = ("cnh",)
DOC_TRACK = ("cnh", "antecedentes")
DOC_LABEL = {"cnh": "CNH", "antecedentes": "Antecedentes criminais", "crlv": "CRLV"}


def ensure_driver_docs(driver_id):
    existing = {r["kind"] for r in query("SELECT kind FROM driver_documents WHERE driver_id=?", (driver_id,))}
    for kind in DOC_TRACK:
        if kind not in existing:
            execute(
                "INSERT INTO driver_documents (driver_id, kind, status) VALUES (?,?, 'pendente')",
                (driver_id, kind),
            )


def documents_ok(driver_id) -> bool:
    rows = query("SELECT kind, status, expires_at FROM driver_documents WHERE driver_id=?", (driver_id,))
    by_kind = {r["kind"]: r for r in rows}
    cnh = by_kind.get("cnh")
    if not cnh or cnh["status"] != "aprovado":
        return False
    day = parse_doc_date(cnh["expires_at"])
    if day and day < datetime.now().date():
        return False
    return True


def parse_doc_date(value):
    text = str(value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def cnh_expiry_state(driver_id):
    row = query(
        "SELECT expires_at FROM driver_documents WHERE driver_id=? AND kind='cnh'",
        (driver_id,),
        one=True,
    )
    if not row or not row["expires_at"]:
        return "sem_validade", None, None
    day = parse_doc_date(row["expires_at"])
    if not day:
        return "sem_validade", None, None
    left = (day - datetime.now().date()).days
    if left < 0:
        return "vencida", day, left
    if left <= 30:
        return "vence", day, left
    return "ok", day, left


def _remind_once(user_id, kind, title, body):
    already = query(
        """SELECT id FROM notifications
           WHERE user_id=? AND kind=? AND created_at >= datetime('now','-1 day')""",
        (user_id, kind),
        one=True,
    )
    if already:
        return False
    push_notification(user_id, title, body, kind=kind, send_email=False)
    return True


def run_doc_reminders():
    last = setting("last_doc_remind", "")
    try:
        if last and datetime.now() - datetime.fromisoformat(last) < timedelta(minutes=30):
            return
    except ValueError:
        pass
    admins = query("SELECT id FROM users WHERE role='admin' AND active=1")
    rows = query(
        """SELECT driver_documents.*, users.name, drivers.user_id AS driver_user
           FROM driver_documents
           JOIN drivers ON drivers.id = driver_documents.driver_id
           JOIN users ON users.id = drivers.user_id"""
    )
    today = datetime.now().date()
    for doc in rows:
        if doc["kind"] == "cnh" and doc["expires_at"]:
            day = parse_doc_date(doc["expires_at"])
            if day:
                left = (day - today).days
                if left <= 30:
                    kind = "cnh_vencida" if left < 0 else "cnh_vence"
                    title = "CNH vencida" if left < 0 else "Lembrete: CNH vencendo"
                    body = f"{doc['name']}: CNH {doc['number'] or ''} em {doc['expires_at']} ({left} dia(s))."
                    for adm in admins:
                        _remind_once(adm["id"], f"{kind}-{doc['driver_id']}", title, body)
                    if doc["driver_user"]:
                        _remind_once(doc["driver_user"], kind, title, "Renove a CNH e avise a central.")
        if doc["kind"] == "antecedentes" and doc["status"] == "pendente":
            title = "Antecedentes pendentes"
            body = f"{doc['name']}: falta consultar antecedentes online."
            for adm in admins:
                _remind_once(adm["id"], f"antec-{doc['driver_id']}", title, body)
    run_fee_reminders()
    set_setting("last_doc_remind", datetime.now().isoformat(timespec="seconds"))


def roll_monthly_fees():
    now = datetime.now()
    for drv in query("SELECT id FROM drivers"):
        paid = query(
            """SELECT id FROM payments
               WHERE driver_id=? AND month=? AND year=? AND status='pago'""",
            (drv["id"], now.month, now.year),
            one=True,
        )
        execute(
            "UPDATE drivers SET payment_status=? WHERE id=?",
            ("pago" if paid else "pendente", drv["id"]),
        )


def run_fee_reminders():
    roll_monthly_fees()
    now = datetime.now()
    pending = query(
        """SELECT drivers.id, drivers.monthly_fee, drivers.user_id, users.name
           FROM drivers JOIN users ON users.id = drivers.user_id
           WHERE drivers.payment_status='pendente'"""
    )
    if not pending:
        return
    total = sum(float(p["monthly_fee"] or 50) for p in pending)
    fase = "vencida" if now.day >= 10 else "vencendo"
    resumo = (
        f"{len(pending)} de até 40 taxistas com taxa de R$ 50 em aberto "
        f"para o administrador · total R$ {total:.2f} · {now.month:02d}/{now.year}"
    )
    admins = query("SELECT id FROM users WHERE role='admin' AND active=1")
    for adm in admins:
        _remind_once(adm["id"], f"taxa-resumo-{now.year}-{now.month}", f"Taxa da frota {fase}", resumo)
        for p in pending:
            _remind_once(
                adm["id"],
                f"taxa-{p['id']}-{now.year}-{now.month}",
                f"Cobrar R$ 50 · {p['name']}",
                f"Mensalidade {now.month:02d}/{now.year} em aberto na central.",
            )
    for p in pending:
        if p["user_id"]:
            _remind_once(
                p["user_id"],
                f"taxa-mot-{now.year}-{now.month}",
                "Taxa da central R$ 50",
                f"Sua mensalidade de {now.month:02d}/{now.year} está em aberto. Pague R$ 50 ao administrador.",
            )


def notify_cnh_expiry():
    run_doc_reminders()


def audit(action, target="", detail="", level="info", actor=None):
    try:
        user = actor if actor is not None else current_user()
        execute(
            """INSERT INTO audit_logs (actor_id, actor_name, action, target, ip, detail, level)
               VALUES (?,?,?,?,?,?,?)""",
            (
                user["id"] if user else None,
                user["name"] if user else "anonimo",
                action,
                target,
                client_ip(),
                detail,
                level,
            ),
        )
    except Exception:
        pass


def log_error(message, source="app", level="error", detail="", path=""):
    """Registra erro no banco, no arquivo e na auditoria crítica."""
    try:
        uid = session.get("user_id") if session else None
    except Exception:
        uid = None
    try:
        ip = client_ip()
        route = path or (request.path if request else "")
    except Exception:
        ip, route = "", path
    text = (message or "erro")[:400]
    extra = (detail or "")[:4000]
    try:
        execute(
            """INSERT INTO error_logs (level, source, message, detail, path, ip, user_id)
               VALUES (?,?,?,?,?,?,?)""",
            (level, source, text, extra, route, ip, uid),
        )
    except Exception:
        pass
    line = f"{source} | {text} | {route} | {ip}"
    if level == "crit":
        error_logger.critical(line)
    elif level == "warn":
        error_logger.warning(line)
    else:
        error_logger.error(line)
    if extra:
        error_logger.error(extra[:1500])
    if level in ("error", "crit"):
        audit("erro_sistema", source, text, "crit")
    if level == "crit":
        dispatch_crit_alert(text, source, extra, route)


def alert_destinations():
    dests = []
    extra = norm_email(setting("alert_email", ""))
    if extra and valid_email(extra):
        dests.append(extra)
    if setting("alert_notify_admins", "1") == "1":
        try:
            admins = query(
                "SELECT email, recovery_email FROM users WHERE role='admin' AND active=1"
            )
        except sqlite3.OperationalError:
            admins = []
        for row in admins:
            for field in ("email", "recovery_email"):
                mail = norm_email(row[field] or "")
                if valid_email(mail):
                    dests.append(mail)
    seen = set()
    unique = []
    for mail in dests:
        if mail not in seen:
            seen.add(mail)
            unique.append(mail)
    return unique


def safe_webhook(url: str) -> str:
    text = (url or "").strip()
    if text.startswith("https://") and len(text) > 20:
        return text
    return ""


def post_json_webhook(url: str, payload: dict) -> tuple[bool, str]:
    target = safe_webhook(url)
    if not target:
        return False, "webhook_invalido"
    raw = json.dumps(payload).encode("utf-8")
    req = Request(
        target,
        data=raw,
        headers={"Content-Type": "application/json", "User-Agent": "ConeSul-Alertas/1.0"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            code = getattr(resp, "status", 200)
            return 200 <= int(code) < 300, f"http_{code}"
    except HTTPError as exc:
        return False, f"http_{exc.code}"
    except URLError as exc:
        return False, str(exc.reason)[:180]
    except Exception as exc:
        return False, str(exc)[:180]


def notify_slack(message, source, path, detail):
    if setting("slack_enabled", "0") != "1":
        return False, "desligado"
    url = os.environ.get("SLACK_WEBHOOK_URL") or setting("slack_webhook", "")
    text = f"*CRÍTICO — Central Táxi Cone Sul*\n• Origem: `{source}`\n• Rota: `{path or '-'}`\n• {message}"
    payload = {
        "text": text,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Alerta crítico · Cone Sul"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Origem*\n{source}"},
                    {"type": "mrkdwn", "text": f"*Rota*\n{path or '-'}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{message[:300]}```"}},
        ],
    }
    if detail:
        payload["blocks"].append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": detail[:180]}]}
        )
    return post_json_webhook(url, payload)


def notify_teams(message, source, path, detail):
    if setting("teams_enabled", "0") != "1":
        return False, "desligado"
    url = os.environ.get("TEAMS_WEBHOOK_URL") or setting("teams_webhook", "")
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": "Alerta crítico Cone Sul",
        "themeColor": "DC2626",
        "title": "Alerta crítico — Central Táxi Cone Sul",
        "text": f"**Origem:** {source}<br/>**Rota:** {path or '-'}<br/>**Mensagem:** {message}<br/>{(detail or '')[:240]}",
    }
    return post_json_webhook(url, payload)


def dispatch_crit_alert(message, source, detail="", path=""):
    if setting("alert_crit_enabled", "1") != "1":
        return
    try:
        cooldown = int(setting("alert_cooldown_min", "15") or 15)
    except ValueError:
        cooldown = 15
    last = setting("alert_last_at", "")
    if last:
        try:
            if datetime.now() - datetime.fromisoformat(last) < timedelta(minutes=max(1, cooldown)):
                execute(
                    """INSERT INTO error_alerts (level, message, source, destination, status)
                       VALUES ('crit', ?, ?, '', 'suprimido_cooldown')""",
                    (message[:240], source),
                )
                return
        except ValueError:
            pass
    dests = alert_destinations()
    slack_on = setting("slack_enabled", "0") == "1"
    teams_on = setting("teams_enabled", "0") == "1"
    set_setting("alert_last_at", datetime.now().isoformat(timespec="seconds"))
    if not dests and not slack_on and not teams_on:
        execute(
            """INSERT INTO error_alerts (level, message, source, destination, status)
               VALUES ('crit', ?, ?, '', 'sem_destino')""",
            (message[:240], source),
        )
        return
    body = (
        "Alerta crítico — Central Táxi Cone Sul\n\n"
        f"Origem: {source}\n"
        f"Rota: {path or '-'}\n"
        f"Mensagem: {message}\n\n"
        f"{(detail or '')[:800]}\n\n"
        "Abra o monitor: /admin/erros\n"
    )
    for dest in dests:
        sent, info = queue_email(
            dest,
            "[CRÍTICO] Falha na Central Táxi Cone Sul",
            body,
            kind="alerta_critico",
        )
        execute(
            """INSERT INTO error_alerts (level, message, source, destination, status)
               VALUES ('crit', ?, ?, ?, ?)""",
            (message[:240], source, dest, "enviado" if sent else info),
        )
    if slack_on:
        ok, info = notify_slack(message, source, path, detail)
        execute(
            """INSERT INTO error_alerts (level, message, source, destination, status)
               VALUES ('crit', ?, ?, 'slack', ?)""",
            (message[:240], source, "enviado" if ok else info),
        )
    if teams_on:
        ok, info = notify_teams(message, source, path, detail)
        execute(
            """INSERT INTO error_alerts (level, message, source, destination, status)
               VALUES ('crit', ?, ?, 'teams', ?)""",
            (message[:240], source, "enviado" if ok else info),
        )


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return query("SELECT * FROM users WHERE id=?", (uid,), one=True)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def jwt_secret() -> bytes:
    return (os.environ.get("JWT_SECRET") or app.secret_key).encode("utf-8")


def make_jwt(user, minutes=None) -> str:
    now = int(time.time())
    payload = {
        "sub": user["id"],
        "role": user["role"],
        "name": user["name"],
        "phone": user["phone"],
        "iat": now,
        "exp": now + int((minutes or JWT_TTL_MIN) * 60),
    }
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(jwt_secret(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


def read_jwt(token: str):
    try:
        header, body, sig = token.split(".")
        expect = hmac.new(jwt_secret(), f"{header}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expect), sig):
            return None
        payload = json.loads(_b64url_decode(body))
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        return payload
    except (ValueError, json.JSONDecodeError, TypeError):
        return None


def jwt_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrap(*args, **kwargs):
            auth = request.headers.get("Authorization") or ""
            token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
            if not token:
                token = request.args.get("token") or request.form.get("token") or ""
            data = read_jwt(token)
            if not data:
                return jsonify({"ok": False, "erro": "token JWT inválido ou expirado"}), 401
            user = query("SELECT * FROM users WHERE id=?", (data.get("sub"),), one=True)
            if not user or not user["active"]:
                return jsonify({"ok": False, "erro": "conta inválida"}), 401
            if role and user["role"] != role:
                return jsonify({"ok": False, "erro": "perfil sem permissão"}), 403
            request.jwt_user = user
            return fn(*args, **kwargs)
        return wrap
    return deco


def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrap(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Faça login para continuar.", "warn")
                if role == "admin" or request.path.startswith("/admin"):
                    return redirect(url_for("admin_login"))
                return redirect(url_for("login"))
            if not user["active"]:
                session.clear()
                flash("Esta conta foi desativada.", "err")
                return redirect(url_for("login"))
            if role and user["role"] != role:
                audit("acesso_negado", fn.__name__, f"perfil={user['role']} exigia={role}", "warn")
                flash("Acesso não permitido para este perfil.", "warn")
                if role == "admin":
                    session.clear()
                    return redirect(url_for("admin_login"))
                return redirect(url_for("home"))
            if user["must_change_password"] and request.endpoint not in (
                "change_password",
                "logout",
                "totp_setup",
                "totp_disable",
            ):
                flash("Por segurança, defina uma senha nova.", "warn")
                return redirect(url_for("change_password"))
            require_admin = setting("require_2fa_admin", "0") == "1"
            if (
                require_admin
                and user["role"] == "admin"
                and not user["totp_enabled"]
                and request.endpoint not in ("totp_setup", "logout", "change_password")
            ):
                flash("A política da central exige 2FA no perfil administrador.", "warn")
                return redirect(url_for("totp_setup"))
            return fn(*args, **kwargs)

        return wrap

    return deco


@app.context_processor
def inject_globals():
    user = current_user()
    crit_alerts = []
    if user and user["role"] == "admin":
        try:
            crit_alerts = query(
                """SELECT * FROM error_alerts
                   WHERE seen=0 AND status NOT IN ('suprimido_cooldown')
                   ORDER BY id DESC LIMIT 5"""
            )
        except sqlite3.OperationalError:
            crit_alerts = []
    unread_notes = 0
    if user and user["role"] in ("passageiro", "motorista", "admin"):
        try:
            row = query(
                "SELECT COUNT(*) AS c FROM notifications WHERE user_id=? AND read_at IS NULL",
                (user["id"],),
                one=True,
            )
            unread_notes = row["c"] if row else 0
        except sqlite3.OperationalError:
            unread_notes = 0
    return {
        "user": user,
        "now": datetime.now(),
        "app_name": "Central Táxi Cone Sul",
        "csrf_token": session.get("csrf", ""),
        "crit_alerts": crit_alerts,
        "unread_notes": unread_notes,
        "ui_theme": (
            setting("ui_theme", "uber") or "uber"
        ),
        "cities_geo": cities_geo(),
        "routes_geo": routes_geo(),
        "vapid_public": vapid_public_b64(),
    }


@app.after_request
def remember_traffic_origin(resp):
    if request.endpoint in (None, "static"):
        return resp
    for key in ("utm_source", "utm_medium", "utm_campaign"):
        val = (request.args.get(key) or "").strip()
        if val:
            resp.set_cookie(key, val[:80], max_age=60 * 60 * 24 * 30)
    if request.referrer and not request.cookies.get("land_ref"):
        resp.set_cookie("land_ref", request.referrer[:250], max_age=60 * 60 * 24 * 30)
    return resp


@app.before_request
def security_gate():
    if request.endpoint not in (None, "static") and current_user():
        try:
            run_doc_reminders()
        except Exception:
            pass
    if request.endpoint in (None, "static"):
        return
    # sessão expirada
    if session.get("user_id"):
        try:
            minutes = int(setting("session_minutes", "45") or 45)
        except ValueError:
            minutes = 45
        started = session.get("login_at")
        last = session.get("last_seen")
        now = datetime.now()
        if started:
            try:
                t0 = datetime.fromisoformat(last or started)
                if now - t0 > timedelta(minutes=minutes):
                    audit("sessao_expirada", str(session.get("user_id")), "timeout", "warn")
                    session.clear()
                    flash("Sessão encerrada por inatividade.", "warn")
                    return redirect(url_for("login"))
            except ValueError:
                pass
        session["last_seen"] = now.isoformat(timespec="seconds")
    # CSRF em POST autenticado
    if request.method == "POST" and session.get("user_id") and session.get("csrf"):
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if sent != session.get("csrf"):
            audit("csrf_bloqueado", request.path, "token inválido", "crit")
            flash("Requisição bloqueada pelo módulo de segurança.", "err")
            return redirect(request.referrer or url_for("home"))


# ---------------------------------------------------------------------------
# Helpers de negócio
# ---------------------------------------------------------------------------
APPROACH_METERS = 200

CITY_COORDS = {
    "Vilhena": (-12.7404, -60.1458),
    "Cerejeiras": (-13.1870, -60.8167),
    "Colorado do Oeste": (-13.1170, -60.5450),
    "Cabixi": (-13.4950, -60.5520),
    "Pimenteiras do Oeste": (-13.4820, -61.0470),
    "Pimenteiras": (-13.4820, -61.0470),
}


def to_coord(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def push_notification(user_id, title, body, ride_id=None, kind="geral", send_email=True):
    execute(
        """INSERT INTO notifications (user_id, ride_id, kind, title, body)
           VALUES (?,?,?,?,?)""",
        (user_id, ride_id, kind, title, body),
    )
    person = query("SELECT * FROM users WHERE id=?", (user_id,), one=True)
    dest = ""
    if person and send_email:
        dest = norm_email(person["email"] or "") or recovery_destination(person)
    if dest:
        queue_email(dest, title, body, kind=kind, user_id=user_id)
    if person:
        try:
            send_web_push(user_id, title, body)
        except Exception:
            pass


def wa_digits(phone: str) -> str:
    d = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if d and not d.startswith("55"):
        d = "55" + d
    return d


def notify_whatsapp(phone: str, text: str) -> bool:
    if setting("wa_enabled", "1") != "1":
        return False
    dest = wa_digits(phone)
    if len(dest) < 12:
        return False
    token = (os.environ.get("WHATSAPP_TOKEN") or setting("wa_token", "") or "").strip()
    phone_id = (os.environ.get("WHATSAPP_PHONE_ID") or setting("wa_phone_id", "") or "").strip()
    if token and phone_id:
        try:
            import urllib.request
            tmpl = (setting("wa_template", "") or "").strip()
            if tmpl:
                body = {
                    "messaging_product": "whatsapp",
                    "to": dest,
                    "type": "template",
                    "template": {"name": tmpl, "language": {"code": "pt_BR"}},
                }
            else:
                body = {
                    "messaging_product": "whatsapp",
                    "to": dest,
                    "type": "text",
                    "text": {"body": text[:900]},
                }
            payload = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                f"https://graph.facebook.com/v21.0/{phone_id}/messages",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                ok = 200 <= resp.status < 300
            if ok:
                audit("whatsapp_ok", dest, text[:80])
            return ok
        except Exception as exc:
            audit("whatsapp_falha", dest, str(exc)[:160], "warn")
            return False
    audit("whatsapp_link", dest, text[:80])
    return False


def notify_route_drivers(route_id, title, body, ride_id=None, kind="fila", only_user_id=None):
    if setting("driver_push_enabled", "1") != "1":
        return 0
    rows = query(
        """
        SELECT users.id
        FROM drivers
        JOIN users ON users.id = drivers.user_id
        WHERE drivers.route_id=? AND drivers.status='ativo' AND users.active=1
        """,
        (route_id,),
    )
    mail = setting("driver_push_email", "0") == "1"
    n = 0
    for row in rows:
        if only_user_id and row["id"] != only_user_id:
            continue
        push_notification(row["id"], title, body, ride_id, kind=kind, send_email=mail)
        n += 1
    return n


def geo_int(key, default):
    try:
        return max(50, int(setting(key, str(default)) or default))
    except ValueError:
        return default


def cities_geo():
    return {name: [lat, lng] for name, (lat, lng) in CITY_COORDS.items() if name != "Pimenteiras do Oeste"}


def routes_geo():
    rows = query("SELECT id, origin, destination, label, price FROM routes ORDER BY id")
    out = []
    for r in rows:
        a = city_point(r["origin"])
        b = city_point(r["destination"])
        if not a[0] or not b[0]:
            continue
        out.append(
            {
                "id": r["id"],
                "label": r["label"],
                "price": f"{float(r['price'] or 0):.2f}",
                "from": [a[0], a[1]],
                "to": [b[0], b[1]],
            }
        )
    return out


def city_point(name):
    if not name:
        return None, None
    key = name.strip()
    if key in CITY_COORDS:
        return CITY_COORDS[key]
    for label, point in CITY_COORDS.items():
        if label.lower() in key.lower() or key.lower() in label.lower():
            return point
    return None, None


def ensure_ride_fences(ride):
    if not ride:
        return
    existing = {
        r["kind"]: r
        for r in query("SELECT * FROM geofences WHERE ride_id=? AND active=1", (ride["id"],))
    }
    pax = query("SELECT home_lat, home_lng FROM users WHERE id=?", (ride["passenger_id"],), one=True)
    casa_lat = to_coord(ride["pickup_lat"]) or (to_coord(pax["home_lat"]) if pax else None)
    casa_lng = to_coord(ride["pickup_lng"]) or (to_coord(pax["home_lng"]) if pax else None)
    radius_casa = geo_int("geo_radius_casa", APPROACH_METERS)
    if casa_lat is not None and casa_lng is not None and "casa" not in existing:
        execute(
            """INSERT INTO geofences (ride_id, kind, lat, lng, radius_m, last_state)
               VALUES (?,?,?,?,?,'fora')""",
            (ride["id"], "casa", casa_lat, casa_lng, radius_casa),
        )
        execute(
            "UPDATE rides SET pickup_lat=COALESCE(pickup_lat,?), pickup_lng=COALESCE(pickup_lng,?) WHERE id=?",
            (casa_lat, casa_lng, ride["id"]),
        )
    route = query("SELECT * FROM routes WHERE id=?", (ride["route_id"],), one=True)
    dest_lat, dest_lng = city_point(route["destination"] if route else "")
    radius_dest = geo_int("geo_radius_destino", 400)
    if dest_lat is not None and dest_lng is not None and "destino" not in existing:
        execute(
            """INSERT INTO geofences (ride_id, kind, lat, lng, radius_m, last_state)
               VALUES (?,?,?,?,?,'fora')""",
            (ride["id"], "destino", dest_lat, dest_lng, radius_dest),
        )


def record_fence_event(fence, ride, driver_id, event, distance, detail=""):
    execute(
        """INSERT INTO geofence_events (fence_id, ride_id, driver_id, event, distance_m, detail)
           VALUES (?,?,?,?,?,?)""",
        (fence["id"], ride["id"], driver_id, event, int(distance), detail),
    )
    execute(
        "UPDATE geofences SET last_state=?, last_event_at=? WHERE id=?",
        (
            "dentro" if event == "entrou" else "fora",
            datetime.now().isoformat(timespec="seconds"),
            fence["id"],
        ),
    )


def evaluate_geofences(driver_id):
    if setting("geo_enabled", "1") != "1":
        return []
    drv = query("SELECT * FROM drivers WHERE id=?", (driver_id,), one=True)
    if not drv or drv["lat"] is None or drv["lng"] is None:
        return []
    rides = query(
        """
        SELECT rides.*, u.name AS passenger_name, u.home_lat, u.home_lng
        FROM rides
        JOIN users u ON u.id = rides.passenger_id
        WHERE rides.driver_id=? AND rides.status IN ('aceita','a_caminho','em_andamento')
        """,
        (driver_id,),
    )
    fired = []
    auto_status = setting("geo_auto_status", "1") == "1"
    dlat, dlng = float(drv["lat"]), float(drv["lng"])
    ride_ids = [r["id"] for r in rides]
    for ride in rides:
        ensure_ride_fences(ride)
    fences_by = {}
    if ride_ids:
        ph = ",".join("?" * len(ride_ids))
        for fence in query(f"SELECT * FROM geofences WHERE active=1 AND ride_id IN ({ph})", ride_ids):
            fences_by.setdefault(fence["ride_id"], []).append(fence)
    admins = query("SELECT id FROM users WHERE role='admin' AND active=1")
    driver_user = drv.get("user_id")
    for ride in rides:
        fences = fences_by.get(ride["id"], [])
        for fence in fences:
            dist = haversine_m(dlat, dlng, float(fence["lat"]), float(fence["lng"]))
            radius = float(fence["radius_m"] or APPROACH_METERS)
            state = fence["last_state"] or "fora"
            enter = dist <= radius
            exit_ok = dist > radius * 1.25
            event = None
            if state != "dentro" and enter:
                event = "entrou"
            elif state == "dentro" and exit_ok:
                event = "saiu"
            if not event:
                continue
            record_fence_event(fence, ride, driver_id, event, dist, fence["kind"])
            meters = int(round(dist))
            if fence["kind"] == "casa" and event == "entrou":
                execute("UPDATE rides SET approach_notified=1 WHERE id=?", (ride["id"],))
                if auto_status and ride["status"] == "aceita":
                    execute("UPDATE rides SET status='a_caminho' WHERE id=?", (ride["id"],))
                push_notification(
                    ride["passenger_id"],
                    "Seu táxi está chegando",
                    f"O taxista entrou na cerca da sua casa ({meters} m). Fique no portão.",
                    ride["id"],
                    kind="chegando",
                )
                if driver_user:
                    push_notification(
                        driver_user,
                        "Cerca da casa",
                        f"Você está a {meters} m da casa de {ride['passenger_name']}.",
                        ride["id"],
                        kind="geo_casa",
                        send_email=False,
                    )
                for adm in admins:
                    push_notification(
                        adm["id"],
                        "Alerta de geolocalização",
                        f"Taxista a {meters} m da casa de {ride['passenger_name']} (corrida #{ride['id']}).",
                        ride["id"],
                        kind="geo_admin",
                        send_email=False,
                    )
                audit("geofence_casa", str(ride["id"]), f"entrou {meters}m", "info")
                fired.append({"ride_id": ride["id"], "cerca": "casa", "evento": event, "metros": meters})
            elif fence["kind"] == "casa" and event == "saiu" and ride["status"] == "em_andamento":
                push_notification(
                    ride["passenger_id"],
                    "Viagem iniciada",
                    "O táxi saiu da cerca da casa. Boa viagem pelo Cone Sul.",
                    ride["id"],
                    kind="saiu_cerca",
                )
                fired.append({"ride_id": ride["id"], "cerca": "casa", "evento": event, "metros": meters})
            elif fence["kind"] == "destino" and event == "entrou" and ride["status"] in ("a_caminho", "em_andamento"):
                push_notification(
                    ride["passenger_id"],
                    "Chegando no destino",
                    f"O taxista entrou na cerca da cidade de destino ({meters} m).",
                    ride["id"],
                    kind="destino",
                )
                audit("geofence_destino", str(ride["id"]), f"entrou {meters}m", "info")
                fired.append({"ride_id": ride["id"], "cerca": "destino", "evento": event, "metros": meters})
    return fired


def check_driver_approach(driver_id):
    return evaluate_geofences(driver_id)


def route_availability():
    rows = query(
        """
        SELECT r.*,
               COUNT(d.id) AS total_taxis,
               SUM(CASE WHEN d.status='ativo' AND d.payment_status='pago' THEN 1 ELSE 0 END) AS livres
        FROM routes r
        LEFT JOIN drivers d ON d.route_id = r.id
        GROUP BY r.id
        ORDER BY r.id
        """
    )
    return rows


def monthly_revenue():
    hit = cache_get("dash:rev")
    if hit:
        return hit
    row = query(
        """
        SELECT
          COALESCE(SUM(CASE WHEN payment_status='pago' THEN monthly_fee ELSE 0 END),0) AS receita,
          COUNT(*) AS total,
          COALESCE(SUM(CASE WHEN payment_status='pago' THEN 1 ELSE 0 END),0) AS pagos
        FROM drivers
        """,
        one=True,
    )
    data = {"receita": float(row["receita"] or 0), "total": int(row["total"] or 0), "pagos": int(row["pagos"] or 0)}
    cache_set("dash:rev", data, 25)
    return data


# ---------------------------------------------------------------------------
# Rotas públicas / auth
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if user["role"] == "motorista":
        return redirect(url_for("driver_home"))
    return redirect(url_for("passenger_home"))


@app.route("/admin/entrar", methods=["GET", "POST"])
def admin_login():
    user = current_user()
    if user and user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        identifier = (request.form.get("login") or "").strip()
        password = request.form.get("password", "")
        ip = client_ip()
        found = find_user_by_login(identifier)
        phone = found["phone"] if found else "".join(ch for ch in identifier if ch.isdigit())
        if found and found["locked_until"]:
            try:
                until = datetime.fromisoformat(found["locked_until"])
                if until > datetime.now():
                    execute(
                        "INSERT INTO login_attempts (phone, ip, success, reason) VALUES (?,?,0,?)",
                        (phone, ip, "admin_bloqueado"),
                    )
                    flash("Conta administrativa bloqueada no momento.", "err")
                    return render_template("admin_login.html")
            except ValueError:
                pass
        ok = bool(
            found
            and found["active"]
            and found["role"] == "admin"
            and check_password_hash(found["password_hash"], password)
        )
        if ok:
            execute(
                "INSERT INTO login_attempts (phone, ip, success, reason) VALUES (?,?,1,?)",
                (phone, ip, "admin_ok"),
            )
            if found["totp_enabled"]:
                session.clear()
                session["pending_2fa"] = found["id"]
                session["pending_csrf"] = secrets.token_hex(16)
                session["pending_since"] = datetime.now().isoformat(timespec="seconds")
                return redirect(url_for("login_2fa"))
            finish_login(found)
            audit("login_admin", found["phone"], "painel", actor=found)
            flash("Área administrativa liberada.", "ok")
            return redirect(url_for("admin_dashboard"))
        execute(
            "INSERT INTO login_attempts (phone, ip, success, reason) VALUES (?,?,0,?)",
            (phone or "vazio", ip, "admin_negado"),
        )
        if found and found["role"] != "admin":
            flash("Esta conta não é de administrador.", "err")
        else:
            flash("Usuário ou senha do administrador inválidos.", "err")
        audit("admin_login_falhou", phone or identifier, ip, "warn")
    return render_template("admin_login.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("home"))
    if request.method == "POST":
        identifier = request.form.get("login") or request.form.get("phone") or request.form.get("email") or ""
        password = request.form.get("password", "")
        ip = client_ip()
        user = find_user_by_login(identifier)
        phone = user["phone"] if user else "".join(ch for ch in identifier if ch.isdigit())

        if user and user["locked_until"]:
            try:
                until = datetime.fromisoformat(user["locked_until"])
                if until > datetime.now():
                    mins = max(1, int((until - datetime.now()).total_seconds() // 60) + 1)
                    execute(
                        "INSERT INTO login_attempts (phone, ip, success, reason) VALUES (?,?,0,?)",
                        (phone, ip, "conta_bloqueada"),
                    )
                    flash(f"Conta temporariamente bloqueada. Tente em {mins} min.", "err")
                    return render_template("login.html")
            except ValueError:
                pass

        ok = bool(
            user
            and user["active"]
            and check_password_hash(user["password_hash"], password)
        )
        if ok:
            execute(
                "INSERT INTO login_attempts (phone, ip, success, reason) VALUES (?,?,1,?)",
                (phone, ip, "senha_ok"),
            )
            if user["totp_enabled"]:
                session.clear()
                session["pending_2fa"] = user["id"]
                session["pending_csrf"] = secrets.token_hex(16)
                session["pending_since"] = datetime.now().isoformat(timespec="seconds")
                audit("login_aguardando_2fa", user["phone"], "", "info", actor=user)
                return redirect(url_for("login_2fa"))
            finish_login(user)
            flash(f"Bem-vindo(a), {user['name'].split()[0]}!", "ok")
            return redirect(url_for("home"))

        reason = "senha_invalida"
        if user and not user["active"]:
            reason = "conta_desativada"
        execute(
            "INSERT INTO login_attempts (phone, ip, success, reason) VALUES (?,?,0,?)",
            (phone or "vazio", ip, reason),
        )
        if user and user["active"]:
            fails = int(user["failed_logins"] or 0) + 1
            try:
                max_fails = int(setting("max_fails", "5") or 5)
                lock_min = int(setting("lock_minutes", "15") or 15)
            except ValueError:
                max_fails, lock_min = 5, 15
            locked = None
            if fails >= max_fails:
                locked = (datetime.now() + timedelta(minutes=lock_min)).isoformat(timespec="seconds")
                audit("conta_bloqueada_falhas", user["phone"], f"{fails} tentativas", "crit", actor=user)
            execute(
                "UPDATE users SET failed_logins=?, locked_until=? WHERE id=?",
                (fails, locked, user["id"]),
            )
            if locked:
                flash(f"Muitas tentativas. Conta bloqueada por {lock_min} minutos.", "err")
                return render_template("login.html")
        flash("Telefone ou senha inválidos.", "err")
    return render_template("login.html")


@app.route("/cadastro", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = "".join(ch for ch in request.form.get("phone", "") if ch.isdigit())
        password = request.form.get("password", "")
        email = norm_email(request.form.get("email", ""))
        recovery = norm_email(request.form.get("recovery_email", ""))
        address = request.form.get("address", "").strip()
        house = request.form.get("house_label", "").strip()
        try:
            min_pw = int(setting("min_password", "6") or 6)
        except ValueError:
            min_pw = 6
        if not name or len(phone) < 10 or len(password) < min_pw or not valid_email(email):
            flash(f"Preencha nome, e-mail válido, telefone e senha (mín. {min_pw}).", "err")
            return render_template("register.html")
        if recovery and not valid_email(recovery):
            flash("E-mail de recuperação inválido.", "err")
            return render_template("register.html")
        if recovery and recovery == email:
            flash("O e-mail de recuperação precisa ser diferente do e-mail principal.", "err")
            return render_template("register.html")
        exists = query("SELECT id FROM users WHERE phone=?", (phone,), one=True)
        if exists:
            flash("Este telefone já está cadastrado.", "err")
            return render_template("register.html")
        taken = query(
            "SELECT id FROM users WHERE lower(email)=? OR lower(recovery_email)=?",
            (email, email),
            one=True,
        )
        if taken:
            flash("Este e-mail já está em uso.", "err")
            return render_template("register.html")
        execute(
            """INSERT INTO users (name, phone, password_hash, role, address, house_label, email, recovery_email)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, phone, generate_password_hash(password), "passageiro", address, house, email, recovery),
        )
        new_id = query("SELECT last_insert_rowid() AS id", one=True)["id"]
        execute("UPDATE users SET referral_code=? WHERE id=?", (make_referral_code(new_id, name), new_id))
        ref = (request.form.get("ref") or request.cookies.get("ref_code") or request.args.get("ref") or "").strip()
        track_traffic(user_id=new_id, path="/cadastro")
        if apply_referral(new_id, ref):
            flash(f"Cadastro feito. Vocês dois ganharam R$ {share_bonus_value():.2f} de crédito.", "ok")
        else:
            flash("Cadastro feito. Entre com e-mail ou telefone.", "ok")
        audit("cadastro_passageiro", phone, email, "info")
        return redirect(url_for("login"))
    return render_template("register.html", ref=request.args.get("ref") or request.cookies.get("ref_code") or "")


@app.route("/r/<code>")
def share_landing(code):
    track_traffic(path=f"/r/{code}")
    resp = redirect(url_for("register", ref=code, utm_source="indicacao", utm_medium="share", utm_campaign=code))
    resp.set_cookie("ref_code", code.upper(), max_age=60 * 60 * 24 * 30)
    resp.set_cookie("utm_source", "indicacao", max_age=60 * 60 * 24 * 30)
    resp.set_cookie("utm_campaign", code.upper()[:80], max_age=60 * 60 * 24 * 30)
    return resp


@app.route("/app/indicar")
@login_required("passageiro")
def share_module():
    user = current_user()
    code = ensure_referral_code(user)
    link = url_for("share_landing", code=code, _external=True)
    bonus = share_bonus_value()
    lista = query(
        """SELECT referrals.*, u.name AS guest_name
           FROM referrals JOIN users u ON u.id = referrals.guest_id
           WHERE referrals.inviter_id=?
           ORDER BY referrals.id DESC""",
        (user["id"],),
    )
    credit = float(user["ride_credit"] or 0) if "ride_credit" in user.keys() else 0
    return render_template(
        "indicar.html",
        code=code,
        link=link,
        bonus=bonus,
        lista=lista,
        credit=credit,
    )


@app.route("/logout")
def logout():
    audit("logout")
    session.clear()
    flash("Você saiu da conta.", "ok")
    return redirect(url_for("login"))


TOKEN_ERRORS = {
    "formato_invalido": "Token com formato inválido.",
    "inexistente": "Token não encontrado.",
    "ja_usado": "Este token já foi usado.",
    "expirado": "Token expirado. Peça um link novo.",
    "tentativas": "Token bloqueado após várias tentativas.",
    "invalido": "Token inválido.",
}


@app.route("/conta/esqueci", methods=["GET", "POST"])
def forgot_password():
    preview = None
    smtp_on = smtp_config()["enabled"]
    if request.method == "POST":
        identifier = request.form.get("login", "")
        user = find_user_for_recovery(identifier)
        dest = recovery_destination(user) if user else ""
        if user and dest:
            raw = issue_token(user["id"], dest, purpose="reset", minutes=30)
            link = url_for("reset_password", token=raw, _external=True)
            sent, info = queue_email(
                dest,
                "Recuperação de senha — Central Táxi Cone Sul",
                (
                    f"Olá, {user['name']}.\n\n"
                    "Recebemos um pedido para redefinir sua senha.\n"
                    f"Valide o token abrindo este link em até 30 minutos:\n{link}\n\n"
                    "O link é de uso único. Se não foi você, ignore este e-mail.\n"
                ),
                kind="recuperacao",
                user_id=user["id"],
            )
            audit("pedido_recuperacao_senha", dest, info if not sent else "enviado", "warn", actor=user)
            if not smtp_on:
                preview = link
        flash("Se o e-mail existir, enviamos o link para o endereço de recuperação.", "ok")
    return render_template("forgot_password.html", preview=preview, smtp_on=smtp_on)


@app.route("/conta/recuperar/<token>", methods=["GET", "POST"])
def reset_password(token):
    found, status = validate_token(token, purpose="reset")
    if not found:
        audit("token_rejeitado", "reset", status, "warn")
        log_error(
            f"Token de recuperação rejeitado ({status})",
            source="token",
            level="warn",
            detail=status,
        )
        flash(TOKEN_ERRORS.get(status, "Token inválido."), "err")
        return redirect(url_for("forgot_password"))
    min_pw = int(setting("min_password", "6") or 6)
    if request.method == "POST":
        found, status = validate_token(token, purpose="reset")
        if not found:
            flash(TOKEN_ERRORS.get(status, "Token inválido."), "err")
            return redirect(url_for("forgot_password"))
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        if len(new) < min_pw or new != confirm:
            flash("Senha inválida ou confirmação diferente.", "err")
            return render_template("reset_password.html", min_pw=min_pw, token=token)
        execute(
            "UPDATE users SET password_hash=?, must_change_password=0, failed_logins=0, locked_until=NULL WHERE id=?",
            (generate_password_hash(new), found["user_id"]),
        )
        consume_token(found)
        audit("senha_redefinida_email", found["destination"], str(found["user_id"]), "warn")
        flash("Senha redefinida. Token validado e encerrado.", "ok")
        return redirect(url_for("login"))
    return render_template("reset_password.html", min_pw=min_pw, token=token)


@app.route("/conta/emails", methods=["GET", "POST"])
@login_required()
def account_emails():
    user = current_user()
    if request.method == "POST":
        email = norm_email(request.form.get("email", ""))
        recovery = norm_email(request.form.get("recovery_email", ""))
        password = request.form.get("password", "")
        if not check_password_hash(user["password_hash"], password):
            flash("Confirme com a senha atual.", "err")
            return render_template("account_emails.html")
        if email and not valid_email(email):
            flash("E-mail principal inválido.", "err")
            return render_template("account_emails.html")
        if recovery and not valid_email(recovery):
            flash("E-mail de recuperação inválido.", "err")
            return render_template("account_emails.html")
        if recovery and email and recovery == email:
            flash("O e-mail de recuperação precisa ser diferente do principal.", "err")
            return render_template("account_emails.html")
        if email:
            clash = query(
                "SELECT id FROM users WHERE id!=? AND (lower(email)=? OR lower(recovery_email)=?)",
                (user["id"], email, email),
                one=True,
            )
            if clash:
                flash("Este e-mail já pertence a outra conta.", "err")
                return render_template("account_emails.html")
        execute(
            "UPDATE users SET email=?, recovery_email=? WHERE id=?",
            (email, recovery, user["id"]),
        )
        if recovery:
            queue_email(
                recovery,
                "E-mail de recuperação cadastrado — Cone Sul",
                (
                    f"Olá, {user['name']}.\n\n"
                    "Este endereço foi definido como e-mail de recuperação da Central Táxi Cone Sul.\n"
                    "É para ele que enviaremos o link se você esquecer a senha.\n"
                ),
                kind="aviso",
                user_id=user["id"],
            )
        audit("atualizar_emails", email, recovery)
        flash("E-mails da conta atualizados.", "ok")
        return redirect(url_for("account_emails"))
    return render_template("account_emails.html")


@app.route("/conta/senha", methods=["GET", "POST"])
@login_required()
def change_password():
    user = current_user()
    min_pw = int(setting("min_password", "6") or 6)
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        if not check_password_hash(user["password_hash"], current):
            flash("Senha atual incorreta.", "err")
            return render_template("change_password.html", min_pw=min_pw)
        if len(new) < min_pw:
            flash(f"A nova senha precisa ter pelo menos {min_pw} caracteres.", "err")
            return render_template("change_password.html", min_pw=min_pw)
        if new != confirm:
            flash("A confirmação não confere.", "err")
            return render_template("change_password.html", min_pw=min_pw)
        execute(
            "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
            (generate_password_hash(new), user["id"]),
        )
        session["csrf"] = secrets.token_hex(16)
        audit("senha_alterada", user["phone"], "", "info")
        flash("Senha atualizada.", "ok")
        return redirect(url_for("home"))
    return render_template("change_password.html", min_pw=min_pw)


@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    uid = session.get("pending_2fa")
    if not uid:
        return redirect(url_for("login"))
    if session.get("pending_since"):
        try:
            started = datetime.fromisoformat(session["pending_since"])
            if datetime.now() - started > timedelta(minutes=5):
                session.clear()
                flash("O código 2FA expirou. Entre de novo.", "warn")
                return redirect(url_for("login"))
        except ValueError:
            pass
    user = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not user or not user["totp_enabled"]:
        session.clear()
        return redirect(url_for("login"))
    if request.method == "POST":
        token = request.form.get("csrf") or ""
        if session.get("pending_csrf") and token != session.get("pending_csrf"):
            flash("Requisição bloqueada.", "err")
            return render_template("login_2fa.html", pending_csrf=session.get("pending_csrf"))
        code = request.form.get("code", "")
        new_backup = None
        valid = totp_ok(user["totp_secret"] or "", code)
        if not valid:
            new_backup = consume_backup_code(user["totp_backup"] or "", code)
            valid = new_backup is not None
        if not valid:
            audit("2fa_falhou", user["phone"], "", "warn", actor=user)
            flash("Código inválido. Use o app ou um código de recuperação.", "err")
            return render_template("login_2fa.html", pending_csrf=session.get("pending_csrf"))
        if new_backup is not None:
            execute("UPDATE users SET totp_backup=? WHERE id=?", (new_backup, user["id"]))
            audit("2fa_backup_usado", user["phone"], "", "warn", actor=user)
        finish_login(user)
        flash(f"2FA confirmado. Olá, {user['name'].split()[0]}!", "ok")
        return redirect(url_for("home"))
    return render_template("login_2fa.html", pending_csrf=session.get("pending_csrf"))


@app.route("/conta/2fa", methods=["GET", "POST"])
@login_required()
def totp_setup():
    user = current_user()
    if user["totp_enabled"] and request.method == "GET":
        return render_template("totp_setup.html", enabled=True, secret=None, uri=None, qr=None)

    if request.method == "POST":
        action = request.form.get("action", "confirm")
        if action == "start":
            secret = generate_totp_secret()
            session["totp_draft"] = secret
            uri = otpauth_uri(user, secret)
            qr = "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=" + quote(uri)
            return render_template(
                "totp_setup.html",
                enabled=False,
                secret=secret,
                uri=uri,
                qr=qr,
                step="confirm",
            )
        secret = session.get("totp_draft") or ""
        if not totp_ok(secret, request.form.get("code", "")):
            flash("Código do aplicativo não conferiu. Tente o número atual.", "err")
            uri = otpauth_uri(user, secret) if secret else ""
            qr = (
                "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=" + quote(uri)
                if secret
                else None
            )
            return render_template(
                "totp_setup.html",
                enabled=False,
                secret=secret,
                uri=uri,
                qr=qr,
                step="confirm",
            )
        codes = make_backup_codes()
        execute(
            "UPDATE users SET totp_secret=?, totp_enabled=1, totp_backup=? WHERE id=?",
            (secret, store_backup_codes(codes), user["id"]),
        )
        session.pop("totp_draft", None)
        audit("2fa_ativado", user["phone"])
        flash("Autenticação de dois fatores ativada.", "ok")
        return render_template("totp_setup.html", enabled=True, backup_codes=codes, just_on=True)

    return render_template("totp_setup.html", enabled=False, secret=None, uri=None, qr=None)


@app.route("/conta/2fa/desativar", methods=["POST"])
@login_required()
def totp_disable():
    user = current_user()
    password = request.form.get("password", "")
    code = request.form.get("code", "")
    if not check_password_hash(user["password_hash"], password):
        flash("Senha incorreta.", "err")
        return redirect(url_for("totp_setup"))
    if user["totp_enabled"] and not totp_ok(user["totp_secret"] or "", code):
        backup = consume_backup_code(user["totp_backup"] or "", code)
        if backup is None:
            flash("Informe um código 2FA válido para desativar.", "err")
            return redirect(url_for("totp_setup"))
    if setting("require_2fa_admin", "0") == "1" and user["role"] == "admin":
        flash("A política atual não permite desligar o 2FA do administrador.", "err")
        return redirect(url_for("totp_setup"))
    execute(
        "UPDATE users SET totp_secret=NULL, totp_enabled=0, totp_backup=NULL WHERE id=?",
        (user["id"],),
    )
    audit("2fa_desativado", user["phone"], "", "warn")
    flash("2FA desativado nesta conta.", "ok")
    return redirect(url_for("totp_setup"))


# ---------------------------------------------------------------------------
# Passageiro
# ---------------------------------------------------------------------------
@app.route("/app")
@login_required("passageiro")
def passenger_home():
    routes = route_availability()
    upcoming = query(
        """
        SELECT rides.*, routes.label AS route_label
        FROM rides JOIN routes ON routes.id = rides.route_id
        WHERE passenger_id=? AND status NOT IN ('concluida','cancelada')
        ORDER BY COALESCE(scheduled_at, created_at) ASC
        LIMIT 3
        """,
        (session["user_id"],),
    )
    return render_template("passenger_home.html", routes=routes, upcoming=upcoming)


@app.route("/app/rotas")
@login_required("passageiro")
def passenger_routes():
    return render_template("passenger_rotas.html", routes=route_availability())


@app.route("/app/solicitar/<int:route_id>", methods=["GET", "POST"])
@login_required("passageiro")
def request_now(route_id):
    route = query("SELECT * FROM routes WHERE id=?", (route_id,), one=True)
    if not route:
        flash("Rota não encontrada.", "err")
        return redirect(url_for("passenger_home"))
    user = current_user()
    if request.method == "POST":
        address = request.form.get("pickup_address", "").strip()
        notes = request.form.get("notes", "").strip()
        if not address:
            flash("Informe o endereço de embarque.", "err")
            return render_template("solicitar.html", route=route)
        execute(
            """INSERT INTO rides
               (passenger_id, route_id, kind, status, pickup_address, house_label,
                seats, price, notes, pickup_lat, pickup_lng)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user["id"],
                route_id,
                "imediata",
                "pendente",
                address,
                user["house_label"] or "",
                1,
                route["price"],
                notes,
                user["home_lat"],
                user["home_lng"],
            ),
        )
        ride_id = query("SELECT last_insert_rowid() AS id", one=True)["id"]
        execute(
            "INSERT INTO ride_passengers (ride_id, name, phone, is_requester) VALUES (?,?,?,1)",
            (ride_id, user["name"], user["phone"]),
        )
        if setting("driver_push_fila", "1") == "1":
            notify_route_drivers(
                route_id,
                "Nova corrida na sua rota",
                f"{user['name']} pediu {route['label']} agora. {address}",
                ride_id,
                kind="fila",
            )
        flash("Corrida solicitada! Aguardando um taxista da rota.", "ok")
        return redirect(url_for("passenger_rides"))
    return render_template("solicitar.html", route=route)


@app.route("/app/encomenda", methods=["GET", "POST"])
@login_required("passageiro")
def parcel_new():
    routes = query("SELECT * FROM routes ORDER BY id")
    user = current_user()
    if request.method == "POST":
        route_id = int(request.form.get("route_id") or 0)
        route = query("SELECT * FROM routes WHERE id=?", (route_id,), one=True)
        pickup = request.form.get("pickup_address", "").strip()
        dest = request.form.get("delivery_address", "").strip()
        rec_name = request.form.get("recipient_name", "").strip()
        rec_phone = "".join(ch for ch in request.form.get("recipient_phone", "") if ch.isdigit())
        desc = request.form.get("description", "").strip()
        size = request.form.get("size", "media")
        notes = request.form.get("notes", "").strip()
        if size not in ("pequena", "media", "grande"):
            size = "media"
        if not route or len(pickup) < 6 or len(dest) < 6 or len(rec_name) < 3:
            flash("Preencha rota, coleta, entrega e o nome de quem recebe.", "err")
            return render_template("encomenda.html", routes=routes)
        price = float(route["price"] or 0)
        if size == "grande":
            price = round(price * 1.2, 2)
        elif size == "pequena":
            price = round(price * 0.7, 2)
        execute(
            """INSERT INTO parcels
               (sender_id, route_id, pickup_address, delivery_address, recipient_name,
                recipient_phone, description, size, price, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user["id"], route_id, pickup, dest, rec_name, rec_phone, desc, size, price, notes),
        )
        pid = query("SELECT last_insert_rowid() AS id", one=True)["id"]
        notify_route_drivers(
            route_id,
            "Nova encomenda na rota",
            f"{user['name']} envia {size} · {rec_name} · {dest}",
            kind="encomenda",
        )
        audit("encomenda_nova", str(pid), dest)
        notify_whatsapp(user["phone"], f"Central Táxi: encomenda #{pid} registrada. Coleta {pickup}.")
        if rec_phone:
            notify_whatsapp(rec_phone, f"Central Táxi: encomenda a caminho para {dest}. #{pid}")
        flash("Encomenda registrada. A central vai despachar um táxi.", "ok")
        return redirect(url_for("parcel_list"))
    return render_template("encomenda.html", routes=routes, user=user)


@app.route("/app/encomendas")
@login_required("passageiro")
def parcel_list():
    rows = query(
        """SELECT parcels.*, routes.label AS route_label, du.name AS driver_name
           FROM parcels
           LEFT JOIN routes ON routes.id = parcels.route_id
           LEFT JOIN drivers d ON d.id = parcels.driver_id
           LEFT JOIN users du ON du.id = d.user_id
           WHERE parcels.sender_id=?
           ORDER BY parcels.id DESC""",
        (session["user_id"],),
    )
    return render_template("encomendas.html", parcels=rows)


@app.route("/app/encomenda/<int:pid>/rastreio")
@login_required("passageiro")
def parcel_track(pid):
    row = query(
        """SELECT parcels.*, routes.label AS route_label, du.name AS driver_name, d.plate, d.lat, d.lng
           FROM parcels
           LEFT JOIN routes ON routes.id = parcels.route_id
           LEFT JOIN drivers d ON d.id = parcels.driver_id
           LEFT JOIN users du ON du.id = d.user_id
           WHERE parcels.id=? AND parcels.sender_id=?""",
        (pid, session["user_id"]),
        one=True,
    )
    if not row:
        flash("Encomenda não encontrada.", "err")
        return redirect(url_for("parcel_list"))
    return render_template("encomenda_rastreio.html", p=row)


@app.route("/api/gps/encomenda/<int:pid>")
@login_required()
def api_gps_parcel(pid):
    user = current_user()
    row = query("SELECT * FROM parcels WHERE id=?", (pid,), one=True)
    if not row:
        return jsonify({"ok": False}), 404
    if user["role"] == "passageiro" and row["sender_id"] != user["id"]:
        return jsonify({"ok": False}), 403
    if user["role"] == "motorista":
        drv = driver_record(user["id"])
        if not drv or drv["id"] != row["driver_id"]:
            return jsonify({"ok": False}), 403
    drv = query(
        "SELECT lat, lng, plate, loc_updated_at FROM drivers WHERE id=?",
        (row["driver_id"] or 0),
        one=True,
    )
    return jsonify(
        {
            "ok": True,
            "status": row["status"],
            "lat": drv["lat"] if drv else None,
            "lng": drv["lng"] if drv else None,
            "plate": drv["plate"] if drv else None,
            "quando": drv["loc_updated_at"] if drv else None,
        }
    )


@app.route("/admin/encomendas")
@login_required("admin")
def admin_parcels():
    rows = query(
        """SELECT parcels.*, routes.label AS route_label, u.name AS sender_name, u.phone AS sender_phone,
                  du.name AS driver_name, d.plate
           FROM parcels
           LEFT JOIN routes ON routes.id = parcels.route_id
           LEFT JOIN users u ON u.id = parcels.sender_id
           LEFT JOIN drivers d ON d.id = parcels.driver_id
           LEFT JOIN users du ON du.id = d.user_id
           ORDER BY CASE parcels.status WHEN 'pendente' THEN 0 ELSE 1 END, parcels.id DESC
           LIMIT 80"""
    )
    livres = query(
        """SELECT drivers.id, users.name, drivers.route_id, drivers.plate
           FROM drivers JOIN users ON users.id = drivers.user_id
           WHERE drivers.status='ativo'
           ORDER BY users.name"""
    )
    by_route = {}
    for d in livres:
        by_route.setdefault(d["route_id"], []).append(d)
    return render_template("admin_encomendas.html", parcels=rows, livres_by_route=by_route)


@app.route("/admin/encomendas/<int:pid>/despachar", methods=["POST"])
@login_required("admin")
def admin_parcel_dispatch(pid):
    driver_id = int(request.form.get("driver_id") or 0)
    parcel = query("SELECT * FROM parcels WHERE id=?", (pid,), one=True)
    drv = query("SELECT * FROM drivers WHERE id=?", (driver_id,), one=True)
    if not parcel or not drv:
        flash("Encomenda ou taxista inválido.", "err")
        return redirect(url_for("admin_parcels"))
    execute("UPDATE parcels SET status='aceita', driver_id=? WHERE id=?", (driver_id, pid))
    push_notification(
        drv["user_id"],
        "Encomenda despachada",
        f"Coleta: {parcel['pickup_address']} → {parcel['delivery_address']}",
        kind="encomenda",
        send_email=False,
    )
    push_notification(
        parcel["sender_id"],
        "Táxi a caminho da encomenda",
        "Um taxista foi designado para sua encomenda.",
        kind="encomenda",
        send_email=False,
    )
    cache_bust("dash")
    audit("encomenda_despacho", str(pid), str(driver_id))
    sender = query("SELECT phone FROM users WHERE id=?", (parcel["sender_id"],), one=True)
    if sender:
        notify_whatsapp(sender["phone"], f"Táxi designado para sua encomenda #{pid}.")
    if parcel["recipient_phone"]:
        notify_whatsapp(parcel["recipient_phone"], f"Sua encomenda #{pid} saiu com um táxi da Central Cone Sul.")
    cache_bust("dash")
    flash("Encomenda despachada.", "ok")
    return redirect(url_for("admin_parcels"))


@app.route("/motorista/encomenda/<int:pid>/<status>", methods=["POST"])
@login_required("motorista")
def driver_parcel_status(pid, status):
    if status not in ("a_caminho", "entregue", "cancelada"):
        return redirect(url_for("driver_home"))
    drv = driver_record(session["user_id"])
    row = query("SELECT * FROM parcels WHERE id=? AND driver_id=?", (pid, drv["id"] if drv else 0), one=True)
    if not row:
        flash("Encomenda não encontrada.", "err")
        return redirect(url_for("driver_home"))
    execute("UPDATE parcels SET status=? WHERE id=?", (status, pid))
    labels = {"a_caminho": "Táxi saiu com sua encomenda", "entregue": "Encomenda entregue", "cancelada": "Encomenda cancelada"}
    push_notification(row["sender_id"], labels[status], row["delivery_address"], kind="encomenda", send_email=False)
    sender = query("SELECT phone FROM users WHERE id=?", (row["sender_id"],), one=True)
    if sender:
        notify_whatsapp(sender["phone"], f"{labels[status]} #{pid}")
    if row["recipient_phone"] and status == "entregue":
        notify_whatsapp(row["recipient_phone"], f"Encomenda #{pid} entregue.")
    flash("Status da encomenda atualizado.", "ok")
    return redirect(url_for("driver_home"))


@app.route("/app/agendar", methods=["GET", "POST"])
@login_required("passageiro")
def schedule_ride():
    """Módulo: agendar até 4 pessoas da mesma casa."""
    routes = query("SELECT * FROM routes ORDER BY id")
    user = current_user()
    if request.method == "POST":
        route_id = int(request.form.get("route_id") or 0)
        route = query("SELECT * FROM routes WHERE id=?", (route_id,), one=True)
        date = request.form.get("date", "")
        time = request.form.get("time", "")
        address = request.form.get("pickup_address", "").strip()
        house = request.form.get("house_label", "").strip() or user["house_label"]
        notes = request.form.get("notes", "").strip()

        people = []
        for i in range(1, 5):
            name = request.form.get(f"p{i}_name", "").strip()
            phone = request.form.get(f"p{i}_phone", "").strip()
            doc = request.form.get(f"p{i}_doc", "").strip()
            if name:
                people.append((name, phone, doc, 1 if i == 1 else 0))

        if not route:
            flash("Escolha a rota.", "err")
            return render_template("agendar.html", routes=routes, form=request.form)
        if not date or not time:
            flash("Informe data e horário.", "err")
            return render_template("agendar.html", routes=routes, form=request.form)
        if len(address) < 8:
            flash("Informe o endereço completo da casa.", "err")
            return render_template("agendar.html", routes=routes, form=request.form)
        if not people:
            flash("Inclua pelo menos 1 passageiro.", "err")
            return render_template("agendar.html", routes=routes, form=request.form)
        p1_phone = "".join(ch for ch in (people[0][1] or "") if ch.isdigit())
        if len(p1_phone) < 10:
            flash("Informe o WhatsApp de quem solicita, com DDD.", "err")
            return render_template("agendar.html", routes=routes, form=request.form)
        for nome, fone, _doc, _req in people[1:]:
            digs = "".join(ch for ch in (fone or "") if ch.isdigit())
            if fone and len(digs) < 10:
                flash(f"Telefone de {nome} inválido.", "err")
                return render_template("agendar.html", routes=routes, form=request.form)
        if len(people) > 4:
            flash("Máximo de 4 pessoas da mesma casa.", "err")
            return render_template("agendar.html", routes=routes, form=request.form)

        try:
            when = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        except ValueError:
            flash("Data/hora inválidas.", "err")
            return render_template("agendar.html", routes=routes, form=request.form)
        if when < datetime.now() - timedelta(minutes=5):
            flash("O horário precisa ser no futuro.", "err")
            return render_template("agendar.html", routes=routes, form=request.form)

        execute(
            """INSERT INTO rides
               (passenger_id, route_id, kind, status, pickup_address, house_label,
                scheduled_at, seats, price, notes, pickup_lat, pickup_lng)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user["id"],
                route_id,
                "agendada",
                "pendente",
                address,
                house,
                when.isoformat(sep=" ", timespec="minutes"),
                len(people),
                route["price"],
                notes,
                user["home_lat"],
                user["home_lng"],
            ),
        )
        ride_id = query("SELECT last_insert_rowid() AS id", one=True)["id"]
        for name, phone, doc, is_req in people:
            execute(
                """INSERT INTO ride_passengers (ride_id, name, phone, document, is_requester)
                   VALUES (?,?,?,?,?)""",
                (ride_id, name, phone, doc, is_req),
            )
        if setting("driver_push_fila", "1") == "1":
            notify_route_drivers(
                route_id,
                "Família agendada na sua rota",
                f"{house or user['name']} · {len(people)} pessoa(s) · {when.strftime('%d/%m %H:%M')} · {address}",
                ride_id,
                kind="familia",
            )
        flash(
            f"Agendamento confirmado para {when.strftime('%d/%m/%Y às %H:%M')} "
            f"com {len(people)} passageiro(s) da casa.",
            "ok",
        )
        return redirect(url_for("passenger_rides"))
    return render_template("agendar.html", routes=routes)


@app.route("/app/corridas")
@login_required("passageiro")
def passenger_rides():
    rides = query(
        """
        SELECT rides.*, routes.label AS route_label,
               u.name AS driver_name, d.plate
        FROM rides
        JOIN routes ON routes.id = rides.route_id
        LEFT JOIN drivers d ON d.id = rides.driver_id
        LEFT JOIN users u ON u.id = d.user_id
        WHERE passenger_id=?
        ORDER BY rides.id DESC
        """,
        (session["user_id"],),
    )
    people_map = {}
    if rides:
        ids = tuple(r["id"] for r in rides)
        placeholders = ",".join("?" * len(ids))
        people = query(
            f"SELECT * FROM ride_passengers WHERE ride_id IN ({placeholders})",
            ids,
        )
        for p in people:
            people_map.setdefault(p["ride_id"], []).append(p)
    return render_template("passenger_corridas.html", rides=rides, people_map=people_map)


@app.route("/app/corridas/<int:ride_id>/cancelar", methods=["POST"])
@login_required("passageiro")
def cancel_ride(ride_id):
    ride = query(
        "SELECT * FROM rides WHERE id=? AND passenger_id=?",
        (ride_id, session["user_id"]),
        one=True,
    )
    if not ride:
        flash("Corrida não encontrada.", "err")
    elif ride["status"] in ("concluida", "cancelada", "em_andamento"):
        flash("Esta corrida não pode mais ser cancelada.", "warn")
    else:
        execute("UPDATE rides SET status='cancelada' WHERE id=?", (ride_id,))
        flash("Corrida cancelada.", "ok")
    return redirect(url_for("passenger_rides"))


@app.route("/app/perfil", methods=["GET", "POST"])
@login_required("passageiro")
def passenger_profile():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        house = request.form.get("house_label", "").strip()
        email = norm_email(request.form.get("email", ""))
        recovery = norm_email(request.form.get("recovery_email", ""))
        if email and not valid_email(email):
            flash("E-mail principal inválido.", "err")
            return redirect(url_for("passenger_profile"))
        if recovery and not valid_email(recovery):
            flash("E-mail de recuperação inválido.", "err")
            return redirect(url_for("passenger_profile"))
        if recovery and email and recovery == email:
            flash("O e-mail de recuperação deve ser outro endereço.", "err")
            return redirect(url_for("passenger_profile"))
        home_lat = to_coord(request.form.get("home_lat"))
        home_lng = to_coord(request.form.get("home_lng"))
        execute(
            """UPDATE users SET name=?, address=?, house_label=?, email=?, recovery_email=?,
               home_lat=?, home_lng=?
               WHERE id=?""",
            (name or user["name"], address, house, email, recovery, home_lat, home_lng, user["id"]),
        )
        flash("Perfil atualizado.", "ok")
        return redirect(url_for("passenger_profile"))
    stats = query(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='concluida' THEN 1 ELSE 0 END) AS feitas
           FROM rides WHERE passenger_id=?""",
        (user["id"],),
        one=True,
    )
    return render_template("passenger_perfil.html", stats=stats)


# ---------------------------------------------------------------------------
# Motorista
# ---------------------------------------------------------------------------
def driver_record(user_id):
    return query(
        """
        SELECT drivers.*, users.name, users.phone, routes.label AS route_label
        FROM drivers
        JOIN users ON users.id = drivers.user_id
        LEFT JOIN routes ON routes.id = drivers.route_id
        WHERE drivers.user_id=?
        """,
        (user_id,),
        one=True,
    )


@app.route("/motorista")
@login_required("motorista")
def driver_home():
    drv = driver_record(session["user_id"])
    if not drv:
        flash("Cadastro de motorista incompleto.", "err")
        return redirect(url_for("logout"))
    if drv["status"] in ("bloqueado", "suspenso"):
        return render_template("driver_bloqueado.html", drv=drv)
    queue = query(
        """
        SELECT rides.*, routes.label AS route_label, u.name AS passenger_name, u.phone AS passenger_phone
        FROM rides
        JOIN routes ON routes.id = rides.route_id
        JOIN users u ON u.id = rides.passenger_id
        WHERE rides.route_id=? AND rides.status='pendente' AND rides.driver_id IS NULL
        ORDER BY CASE WHEN kind='imediata' THEN 0 ELSE 1 END, scheduled_at, rides.id
        """,
        (drv["route_id"],),
    )
    mine = query(
        """
        SELECT rides.*, routes.label AS route_label, u.name AS passenger_name
        FROM rides
        JOIN routes ON routes.id = rides.route_id
        JOIN users u ON u.id = rides.passenger_id
        WHERE rides.driver_id=? AND rides.status IN ('aceita','a_caminho','em_andamento')
        ORDER BY rides.id DESC
        """,
        (drv["id"],),
    )
    fences = []
    if mine:
        ids = tuple(r["id"] for r in mine)
        ph = ",".join("?" * len(ids))
        fences = query(f"SELECT * FROM geofences WHERE ride_id IN ({ph})", ids)
    fence_map = {}
    for f in fences:
        fence_map.setdefault(f["ride_id"], []).append(f)
    notes = query(
        "SELECT * FROM notifications WHERE user_id=? AND read_at IS NULL ORDER BY id DESC LIMIT 5",
        (session["user_id"],),
    )
    my_parcels = query(
        """SELECT parcels.*, routes.label AS route_label, u.name AS sender_name
           FROM parcels
           LEFT JOIN routes ON routes.id = parcels.route_id
           LEFT JOIN users u ON u.id = parcels.sender_id
           WHERE parcels.driver_id=? AND parcels.status IN ('aceita','a_caminho')
           ORDER BY parcels.id DESC""",
        (drv["id"],),
    )
    return render_template(
        "driver_home.html",
        drv=drv,
        queue=queue,
        mine=mine,
        fence_map=fence_map,
        notes=notes,
        my_parcels=my_parcels,
    )


@app.route("/motorista/notificacoes")
@login_required("motorista")
def driver_notifications():
    unread = request.args.get("nao_lidas") == "1"
    sql = "SELECT * FROM notifications WHERE user_id=?"
    args = [session["user_id"]]
    if unread:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id DESC LIMIT 30"
    rows = query(sql, args)
    if request.args.get("json") == "1":
        return jsonify([dict(r) for r in rows])
    return render_template("driver_notificacoes.html", notes=rows)


@app.route("/motorista/notificacoes/<int:note_id>/lida", methods=["POST"])
@login_required("motorista")
def driver_read_notification(note_id):
    execute(
        "UPDATE notifications SET read_at=? WHERE id=? AND user_id=?",
        (datetime.now().isoformat(timespec="seconds"), note_id, session["user_id"]),
    )
    if request.headers.get("X-Requested-With") == "fetch" or request.args.get("json"):
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("driver_notifications"))


@app.route("/motorista/aceitar/<int:ride_id>", methods=["POST"])
@login_required("motorista")
def driver_accept(ride_id):
    drv = driver_record(session["user_id"])
    if not drv or drv["status"] != "ativo":
        flash("Motorista bloqueado ou inválido.", "err")
        return redirect(url_for("driver_home"))
    ride = query("SELECT * FROM rides WHERE id=?", (ride_id,), one=True)
    if not ride or ride["status"] != "pendente" or ride["driver_id"]:
        flash("Corrida indisponível.", "warn")
        return redirect(url_for("driver_home"))
    if ride["route_id"] != drv["route_id"]:
        flash("Esta corrida é de outra rota.", "warn")
        return redirect(url_for("driver_home"))
    execute(
        "UPDATE rides SET driver_id=?, status='aceita' WHERE id=? AND driver_id IS NULL",
        (drv["id"], ride_id),
    )
    ride = query("SELECT * FROM rides WHERE id=?", (ride_id,), one=True)
    ensure_ride_fences(ride)
    flash("Corrida aceita. Geofence da casa e do destino ativado.", "ok")
    return redirect(url_for("driver_home"))


@app.route("/motorista/status/<int:ride_id>/<status>", methods=["POST"])
@login_required("motorista")
def driver_set_status(ride_id, status):
    allowed = {"a_caminho", "em_andamento", "concluida"}
    if status not in allowed:
        flash("Status inválido.", "err")
        return redirect(url_for("driver_home"))
    drv = driver_record(session["user_id"])
    ride = query(
        "SELECT * FROM rides WHERE id=? AND driver_id=?",
        (ride_id, drv["id"] if drv else 0),
        one=True,
    )
    if not ride:
        flash("Corrida não encontrada.", "err")
        return redirect(url_for("driver_home"))
    execute("UPDATE rides SET status=? WHERE id=?", (status, ride_id))
    if status == "a_caminho" and ride:
        pax = query("SELECT home_lat, home_lng FROM users WHERE id=?", (ride["passenger_id"],), one=True)
        if pax and (not ride["pickup_lat"] or not ride["pickup_lng"]):
            execute(
                "UPDATE rides SET pickup_lat=COALESCE(pickup_lat,?), pickup_lng=COALESCE(pickup_lng,?) WHERE id=?",
                (pax["home_lat"], pax["home_lng"], ride_id),
            )
        check_driver_approach(drv["id"])
    flash("Status atualizado.", "ok")
    return redirect(url_for("driver_home"))


@app.route("/motorista/posicao", methods=["POST"])
@login_required("motorista")
def driver_position():
    drv = driver_record(session["user_id"])
    if not drv:
        return jsonify({"ok": False}), 400
    data = request.get_json(silent=True) or request.form
    lat = to_coord(data.get("lat"))
    lng = to_coord(data.get("lng"))
    if lat is None or lng is None:
        return jsonify({"ok": False, "erro": "coordenada"}), 400
    execute(
        "UPDATE drivers SET lat=?, lng=?, loc_updated_at=? WHERE id=?",
        (lat, lng, datetime.now().isoformat(timespec="seconds"), drv["id"]),
    )
    fired = check_driver_approach(drv["id"])
    return jsonify({"ok": True, "avisos": fired, "lat": lat, "lng": lng})


@app.route("/api/push/key")
def api_push_key():
    return jsonify({"publicKey": vapid_public_b64()})


@app.route("/api/push/subscribe", methods=["POST"])
@login_required()
def api_push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    if not endpoint:
        return jsonify({"ok": False}), 400
    exists = query("SELECT id FROM push_subscriptions WHERE endpoint=?", (endpoint,), one=True)
    if exists:
        execute("UPDATE push_subscriptions SET user_id=?, p256dh=?, auth=? WHERE endpoint=?",
                (session["user_id"], keys.get("p256dh"), keys.get("auth"), endpoint))
    else:
        execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?,?,?,?)",
            (session["user_id"], endpoint, keys.get("p256dh"), keys.get("auth")),
        )
    return jsonify({"ok": True})


@app.route("/api/gps/ao-vivo")
@login_required()
def api_gps_live():
    user = current_user()
    pontos = []
    if user["role"] == "admin":
        rows = query(
            """SELECT drivers.id, drivers.lat, drivers.lng, drivers.loc_updated_at,
                      drivers.plate, users.name, routes.label AS rota
               FROM drivers
               JOIN users ON users.id = drivers.user_id
               LEFT JOIN routes ON routes.id = drivers.route_id
               WHERE drivers.lat IS NOT NULL AND drivers.lng IS NOT NULL
                 AND drivers.status='ativo'"""
        )
        pontos = [dict(r) for r in rows]
    elif user["role"] == "motorista":
        drv = driver_record(user["id"])
        if drv and drv["lat"] is not None:
            pontos = [{"id": drv["id"], "lat": drv["lat"], "lng": drv["lng"], "name": user["name"], "plate": drv["plate"], "rota": ""}]
    else:
        rows = query(
            """SELECT drivers.id, drivers.lat, drivers.lng, drivers.plate, users.name
               FROM rides
               JOIN drivers ON drivers.id = rides.driver_id
               JOIN users ON users.id = drivers.user_id
               WHERE rides.passenger_id=? AND rides.status IN ('aceita','a_caminho','em_andamento')
                 AND drivers.lat IS NOT NULL""",
            (user["id"],),
        )
        pontos = [dict(r) for r in rows]
    return jsonify({"ok": True, "pontos": pontos})


@app.route("/motorista/simular-chegada/<int:ride_id>", methods=["POST"])
@login_required("motorista")
def driver_simulate_arrival(ride_id):
    drv = driver_record(session["user_id"])
    ride = query(
        """SELECT rides.*, u.home_lat, u.home_lng
           FROM rides JOIN users u ON u.id = rides.passenger_id
           WHERE rides.id=? AND rides.driver_id=?""",
        (ride_id, drv["id"] if drv else 0),
        one=True,
    )
    if not ride:
        flash("Corrida não encontrada.", "err")
        return redirect(url_for("driver_home"))
    lat = to_coord(ride["pickup_lat"]) or to_coord(ride["home_lat"])
    lng = to_coord(ride["pickup_lng"]) or to_coord(ride["home_lng"])
    if lat is None or lng is None:
        flash("A casa do cliente ainda não tem GPS. Peça para marcar no perfil.", "warn")
        return redirect(url_for("driver_home"))
    # ~120 m ao norte da casa — entra no raio de 200 m
    near_lat = lat + 0.00108
    execute(
        "UPDATE drivers SET lat=?, lng=?, loc_updated_at=? WHERE id=?",
        (near_lat, lng, datetime.now().isoformat(timespec="seconds"), drv["id"]),
    )
    execute("UPDATE rides SET status='a_caminho', pickup_lat=?, pickup_lng=? WHERE id=?", (lat, lng, ride_id))
    fired = check_driver_approach(drv["id"])
    if fired:
        flash("Cliente avisado: você está a menos de 200 metros da casa.", "ok")
    else:
        flash("Posição atualizada. Aviso já tinha sido enviado ou a casa não tem GPS.", "warn")
    return redirect(url_for("driver_home"))


@app.route("/app/notificacoes")
@login_required("passageiro")
def passenger_notifications():
    unread = request.args.get("nao_lidas") == "1"
    sql = "SELECT * FROM notifications WHERE user_id=?"
    args = [session["user_id"]]
    if unread:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id DESC LIMIT 20"
    rows = query(sql, args)
    if request.args.get("json") == "1" or request.headers.get("Accept", "").find("application/json") >= 0:
        return jsonify([dict(r) for r in rows])
    return render_template("passenger_notificacoes.html", notes=rows)


@app.route("/app/notificacoes/<int:note_id>/lida", methods=["POST"])
@login_required("passageiro")
def passenger_read_notification(note_id):
    execute(
        "UPDATE notifications SET read_at=? WHERE id=? AND user_id=?",
        (datetime.now().isoformat(timespec="seconds"), note_id, session["user_id"]),
    )
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("passenger_notifications"))


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    rev = monthly_revenue()
    drivers = query(
        """
        SELECT drivers.*, users.name, users.phone, routes.label AS route_label
        FROM drivers
        JOIN users ON users.id = drivers.user_id
        LEFT JOIN routes ON routes.id = drivers.route_id
        ORDER BY users.name
        """
    )
    pending_rides = query(
        """
        SELECT rides.*, routes.label AS route_label, u.name AS passenger_name,
               u.phone AS passenger_phone
        FROM rides JOIN routes ON routes.id=rides.route_id
        JOIN users u ON u.id=rides.passenger_id
        WHERE rides.status='pendente'
        ORDER BY CASE WHEN rides.kind='agendada' THEN 0 ELSE 1 END, rides.scheduled_at, rides.id DESC
        LIMIT 10
        """
    )
    people_map = {}
    if pending_rides:
        ids = tuple(r["id"] for r in pending_rides)
        ph = ",".join("?" * len(ids))
        for p in query(f"SELECT * FROM ride_passengers WHERE ride_id IN ({ph})", ids):
            people_map.setdefault(p["ride_id"], []).append(p)
    livres = query(
        """
        SELECT drivers.id, users.name, drivers.route_id, drivers.plate
        FROM drivers JOIN users ON users.id = drivers.user_id
        WHERE drivers.status='ativo' AND drivers.payment_status='pago'
        ORDER BY users.name
        """
    )
    livres_by_route = {}
    for d in livres:
        livres_by_route.setdefault(d["route_id"], []).append(d)
    meses = {
        "January": "Janeiro", "February": "Fevereiro", "March": "Março",
        "April": "Abril", "May": "Maio", "June": "Junho",
        "July": "Julho", "August": "Agosto", "September": "Setembro",
        "October": "Outubro", "November": "Novembro", "December": "Dezembro",
    }
    en = datetime.now().strftime("%B")
    month_label = f"{meses.get(en, en)} {datetime.now().year}"
    ride_stats = query(
        """
        SELECT
          SUM(CASE WHEN status='pendente' THEN 1 ELSE 0 END) AS pendentes,
          SUM(CASE WHEN status IN ('aceita','a_caminho','em_andamento') THEN 1 ELSE 0 END) AS andamento,
          SUM(CASE WHEN status='concluida' THEN 1 ELSE 0 END) AS concluidas,
          SUM(CASE WHEN kind='agendada' AND status NOT IN ('cancelada','concluida') THEN 1 ELSE 0 END) AS agendas
        FROM rides
        """,
        one=True,
    )
    totals = query(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN status='ativo' THEN 1 ELSE 0 END),0) AS ativos,
                  COALESCE(SUM(CASE WHEN status='bloqueado' THEN 1 ELSE 0 END),0) AS bloqueados,
                  COALESCE(SUM(CASE WHEN payment_status='pendente' THEN 1 ELSE 0 END),0) AS pend_pay
           FROM drivers""",
        one=True,
    )
    ativos = int(totals["ativos"] or 0)
    bloqueados = int(totals["bloqueados"] or 0)
    pend_pay = int(totals["pend_pay"] or 0)
    receita = float(rev["receita"] or 0)
    receita_fmt = f"{receita:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return render_template(
        "admin_dashboard.html",
        rev=rev,
        receita_fmt=receita_fmt,
        drivers=drivers[:10],
        all_count=int(totals["total"] or 0),
        pending_rides=pending_rides,
        month_label=month_label,
        ride_stats=ride_stats,
        ativos=ativos,
        bloqueados=bloqueados,
        pend_pay=pend_pay,
        people_map=people_map,
        livres_by_route=livres_by_route,
        now_label=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )


def fmt_when(value):
    text = str(value or "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            piece = text[:19] if len(text) >= 19 else text[:16]
            return datetime.strptime(piece, fmt).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            continue
    return value or "nunca"


def fmt_phone(phone):
    d = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return phone or ""


@app.route("/admin/motoristas")
@login_required("admin")
def admin_drivers():
    q = request.args.get("q", "").strip()
    rota = request.args.get("rota", "").strip()
    sit = request.args.get("sit", "").strip()
    pag = request.args.get("pag", "").strip()
    sql = """
        SELECT drivers.*, users.name, users.phone, users.email, users.last_login_at,
               users.active AS user_active, routes.label AS route_label,
               (SELECT COUNT(*) FROM rides
                 WHERE driver_id=drivers.id
                   AND status IN ('aceita','a_caminho','em_andamento')) AS corridas_abertas,
               (SELECT COUNT(*) FROM rides
                 WHERE driver_id=drivers.id AND status='concluida') AS concluidas
        FROM drivers
        JOIN users ON users.id = drivers.user_id
        LEFT JOIN routes ON routes.id = drivers.route_id
        WHERE 1=1
    """
    args = []
    if q:
        sql += " AND (users.name LIKE ? OR users.phone LIKE ? OR drivers.plate LIKE ? OR drivers.car_model LIKE ?)"
        args.extend([f"%{q}%"] * 4)
    if rota.isdigit():
        sql += " AND drivers.route_id=?"
        args.append(int(rota))
    if sit in ("ativo", "bloqueado", "suspenso"):
        sql += " AND drivers.status=?"
        args.append(sit)
    if pag in ("pago", "pendente"):
        sql += " AND drivers.payment_status=?"
        args.append(pag)
    sql += " ORDER BY CASE drivers.status WHEN 'bloqueado' THEN 0 ELSE 1 END, users.name"
    raw = query(sql, args)
    ids = [row["id"] for row in raw]
    docs_by = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for doc in query(
            f"SELECT driver_id, kind, status, expires_at FROM driver_documents WHERE driver_id IN ({ph})",
            ids,
        ):
            docs_by.setdefault(doc["driver_id"], {})[doc["kind"]] = doc
    drivers = []
    today = datetime.now().date()
    for row in raw:
        d = dict(row)
        d["phone_fmt"] = fmt_phone(d.get("phone"))
        d["wa"] = "".join(ch for ch in str(d.get("phone") or "") if ch.isdigit())
        pack = docs_by.get(d["id"], {})
        cnh = pack.get("cnh")
        d["docs_ok"] = bool(cnh and cnh["status"] == "aprovado")
        d["cnh_state"] = "sem_validade"
        d["cnh_left"] = None
        if cnh and cnh.get("expires_at"):
            day = parse_doc_date(cnh["expires_at"])
            if day:
                left = (day - today).days
                d["cnh_left"] = left
                if left < 0:
                    d["cnh_state"] = "vencida"
                    d["docs_ok"] = False
                elif left <= 30:
                    d["cnh_state"] = "vence"
                else:
                    d["cnh_state"] = "ok"
        drivers.append(d)
    notify_cnh_expiry()
    totals = query(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='ativo' THEN 1 ELSE 0 END) AS ativos,
                  SUM(CASE WHEN status='bloqueado' THEN 1 ELSE 0 END) AS bloqueados,
                  SUM(CASE WHEN status='suspenso' THEN 1 ELSE 0 END) AS suspensos,
                  SUM(CASE WHEN payment_status='pago' THEN 1 ELSE 0 END) AS pagos,
                  SUM(CASE WHEN payment_status='pendente' THEN 1 ELSE 0 END) AS pendentes
           FROM drivers""",
        one=True,
    )
    routes = query("SELECT * FROM routes ORDER BY id")
    return render_template(
        "admin_motoristas.html",
        drivers=drivers,
        routes=routes,
        q=q,
        rota=rota,
        sit=sit,
        pag=pag,
        totals=totals,
    )


@app.route("/admin/motoristas/novo", methods=["POST"])
@login_required("admin")
def admin_new_driver():
    name = request.form.get("name", "").strip()
    phone = "".join(ch for ch in request.form.get("phone", "") if ch.isdigit())
    plate = request.form.get("plate", "").strip().upper()
    car = request.form.get("car_model", "").strip()
    route_id = int(request.form.get("route_id") or 0)
    password = request.form.get("password") or "123456"
    total = query("SELECT COUNT(*) AS c FROM drivers", one=True)
    if total and int(total["c"] or 0) >= 40:
        flash("A frota já tem 40 taxistas. Libere uma vaga antes de cadastrar outro.", "err")
        return redirect(url_for("admin_drivers"))
    if not name or len(phone) < 10 or not route_id:
        flash("Preencha nome, telefone e rota.", "err")
        return redirect(url_for("admin_drivers"))
    if query("SELECT id FROM users WHERE phone=?", (phone,), one=True):
        flash("Telefone já cadastrado.", "err")
        return redirect(url_for("admin_drivers"))
    parts = name.split()
    initials = (parts[0][0] + (parts[-1][0] if len(parts) > 1 else parts[0][1:2])).upper()
    execute(
        "INSERT INTO users (name, phone, password_hash, role) VALUES (?,?,?,?)",
        (name, phone, generate_password_hash(password), "motorista"),
    )
    uid = query("SELECT last_insert_rowid() AS id", one=True)["id"]
    execute(
        """INSERT INTO drivers (user_id, route_id, plate, car_model, initials, payment_status)
           VALUES (?,?,?,?,?, 'pendente')""",
        (uid, route_id, plate, car, initials),
    )
    did = query("SELECT last_insert_rowid() AS id", one=True)["id"]
    ensure_driver_docs(did)
    audit("cadastrar_taxista", phone, f"{name}; docs CNH/CRLV pendentes")
    flash(f"Taxista {name} cadastrado. Informe a CNH para liberar na fila. Senha: {password}", "ok")
    return redirect(url_for("admin_drivers"))


@app.route("/admin/motoristas/limpar-demo", methods=["POST"])
@login_required("admin")
def admin_clear_demo_fleet():
    rows = query("SELECT id, user_id FROM drivers")
    for drv in rows:
        execute("UPDATE rides SET driver_id=NULL WHERE driver_id=?", (drv["id"],))
        execute("DELETE FROM payments WHERE driver_id=?", (drv["id"],))
        execute("DELETE FROM drivers WHERE id=?", (drv["id"],))
        execute("UPDATE users SET active=0 WHERE id=? AND role='motorista'", (drv["user_id"],))
        execute("DELETE FROM users WHERE id=? AND role='motorista'", (drv["user_id"],))
    audit("limpar_frota_demo", "motoristas", str(len(rows)), "warn")
    flash("Frota de demonstração apagada. Cadastre os taxistas reais.", "ok")
    return redirect(url_for("admin_drivers"))


@app.route("/admin/motoristas/<int:driver_id>/bloquear", methods=["POST"])
@login_required("admin")
def admin_block(driver_id):
    motivo = (request.form.get("motivo") or "bloqueio operacional").strip()
    drv = query(
        "SELECT drivers.status, users.name FROM drivers JOIN users ON users.id=drivers.user_id WHERE drivers.id=?",
        (driver_id,),
        one=True,
    )
    execute("UPDATE drivers SET status='bloqueado' WHERE id=?", (driver_id,))
    audit(
        "bloquear_motorista",
        str(driver_id),
        f"{drv['name'] if drv else ''} · de {drv['status'] if drv else '?'} para bloqueado · motivo: {motivo}",
        "warn",
    )
    flash("Motorista bloqueado. Registro gravado na auditoria.", "ok")
    return redirect(request.referrer or url_for("admin_drivers"))


@app.route("/admin/motoristas/<int:driver_id>/liberar", methods=["POST"])
@login_required("admin")
def admin_unblock(driver_id):
    ensure_driver_docs(driver_id)
    if not documents_ok(driver_id):
        flash("Não libera sem a CNH aprovada.", "err")
        return redirect(url_for("admin_driver_docs", driver_id=driver_id))
    drv = query(
        "SELECT drivers.status, users.name FROM drivers JOIN users ON users.id=drivers.user_id WHERE drivers.id=?",
        (driver_id,),
        one=True,
    )
    execute("UPDATE drivers SET status='ativo' WHERE id=?", (driver_id,))
    audit("liberar_motorista", str(driver_id), f"{drv['name'] if drv else ''} · docs ok", "info")
    flash("Motorista liberado na fila.", "ok")
    return redirect(request.referrer or url_for("admin_drivers"))


@app.route("/admin/motoristas/<int:driver_id>/suspender", methods=["POST"])
@login_required("admin")
def admin_suspend(driver_id):
    motivo = (request.form.get("motivo") or "suspensão temporária").strip()
    drv = query(
        "SELECT drivers.status, users.name FROM drivers JOIN users ON users.id=drivers.user_id WHERE drivers.id=?",
        (driver_id,),
        one=True,
    )
    execute("UPDATE drivers SET status='suspenso' WHERE id=?", (driver_id,))
    audit(
        "suspender_motorista",
        str(driver_id),
        f"{drv['name'] if drv else ''} · de {drv['status'] if drv else '?'} para suspenso · motivo: {motivo}",
        "warn",
    )
    flash("Motorista suspenso. Auditoria registrada.", "ok")
    return redirect(request.referrer or url_for("admin_drivers"))


@app.route("/admin/motoristas/<int:driver_id>/remover", methods=["POST"])
@login_required("admin")
def admin_remove(driver_id):
    drv = query("SELECT * FROM drivers WHERE id=?", (driver_id,), one=True)
    if drv:
        execute("UPDATE rides SET driver_id=NULL WHERE driver_id=?", (driver_id,))
        execute("DELETE FROM payments WHERE driver_id=?", (driver_id,))
        execute("DELETE FROM drivers WHERE id=?", (driver_id,))
        execute("UPDATE users SET active=0 WHERE id=?", (drv["user_id"],))
        audit("remover_motorista", str(driver_id), f"user={drv['user_id']}", "crit")
        flash("Motorista removido da central.", "ok")
    return redirect(url_for("admin_drivers"))


@app.route("/admin/motoristas/<int:driver_id>/documentos")
@login_required("admin")
def admin_driver_docs(driver_id):
    drv = query(
        """SELECT drivers.*, users.name, users.phone
           FROM drivers JOIN users ON users.id=drivers.user_id WHERE drivers.id=?""",
        (driver_id,),
        one=True,
    )
    if not drv:
        flash("Taxista não encontrado.", "err")
        return redirect(url_for("admin_drivers"))
    ensure_driver_docs(driver_id)
    docs = query("SELECT * FROM driver_documents WHERE driver_id=? ORDER BY kind", (driver_id,))
    logs = query(
        """SELECT * FROM audit_logs
           WHERE target=? AND action IN ('bloquear_motorista','suspender_motorista','liberar_motorista','validar_documento','rejeitar_documento')
           ORDER BY id DESC LIMIT 20""",
        (str(driver_id),),
    )
    return render_template(
        "admin_documentos.html",
        drv=drv,
        docs=docs,
        logs=logs,
        labels=DOC_LABEL,
        docs_ok=documents_ok(driver_id),
    )


@app.route("/admin/motoristas/<int:driver_id>/documentos/salvar", methods=["POST"])
@login_required("admin")
def admin_save_doc(driver_id):
    kind = request.form.get("kind") or "cnh"
    if kind not in DOC_LABEL:
        kind = "cnh"
    number = request.form.get("number", "").strip()
    expires = request.form.get("expires_at", "").strip()
    row = query(
        "SELECT id FROM driver_documents WHERE driver_id=? AND kind=?",
        (driver_id, kind),
        one=True,
    )
    if row:
        execute(
            "UPDATE driver_documents SET number=?, expires_at=? WHERE id=?",
            (number, expires, row["id"]),
        )
    else:
        execute(
            "INSERT INTO driver_documents (driver_id, kind, number, expires_at, status) VALUES (?,?,?,?, 'pendente')",
            (driver_id, kind, number, expires),
        )
    audit("atualizar_documento", str(driver_id), f"{kind} {number} validade {expires}")
    flash("Documento atualizado.", "ok")
    return redirect(url_for("admin_driver_docs", driver_id=driver_id))


@app.route("/admin/documentos/<int:doc_id>/validar", methods=["POST"])
@login_required("admin")
def admin_validate_doc(doc_id):
    decision = request.form.get("decision", "aprovado")
    notes = request.form.get("notes", "").strip()
    doc = query("SELECT * FROM driver_documents WHERE id=?", (doc_id,), one=True)
    if not doc:
        flash("Documento não encontrado.", "err")
        return redirect(url_for("admin_drivers"))
    status = "aprovado" if decision == "aprovado" else "rejeitado"
    execute(
        "UPDATE driver_documents SET status=?, notes=?, reviewed_at=? WHERE id=?",
        (status, notes, datetime.now().isoformat(timespec="seconds"), doc_id),
    )
    audit(
        "validar_documento" if status == "aprovado" else "rejeitar_documento",
        str(doc["driver_id"]),
        f"{doc['kind']} → {status} · {notes}",
        "info" if status == "aprovado" else "warn",
    )
    flash(("Documento aprovado." if status == "aprovado" else "Documento recusado."), "ok")
    return redirect(url_for("admin_driver_docs", driver_id=doc["driver_id"]))


@app.route("/admin/motoristas/<int:driver_id>/antecedentes-online", methods=["POST"])
@login_required("admin")
def admin_antecedentes_online(driver_id):
    ensure_driver_docs(driver_id)
    drv = query(
        """SELECT drivers.*, users.name, users.phone
           FROM drivers JOIN users ON users.id=drivers.user_id WHERE drivers.id=?""",
        (driver_id,),
        one=True,
    )
    doc = query(
        "SELECT * FROM driver_documents WHERE driver_id=? AND kind='antecedentes'",
        (driver_id,),
        one=True,
    )
    api = setting("antecedentes_api_url", "").strip()
    resultado = "consulta_registrada"
    detalhe = "Consulta online registrada na central."
    if api.startswith("http"):
        payload = json.dumps(
            {
                "nome": drv["name"] if drv else "",
                "telefone": drv["phone"] if drv else "",
                "cnh": "",
                "driver_id": driver_id,
            }
        ).encode()
        try:
            req = Request(api, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            data = json.loads(raw) if raw else {}
            status_api = str(data.get("status") or data.get("resultado") or "").lower()
            detalhe = str(data.get("detalhe") or data.get("protocolo") or raw)[:240]
            if status_api in ("limpo", "nada_consta", "aprovado", "ok"):
                resultado = "aprovado"
            elif status_api in ("apontamento", "rejeitado", "restrito"):
                resultado = "rejeitado"
        except Exception as exc:
            detalhe = f"Falha na API: {exc}"[:240]
            resultado = "pendente"
    else:
        detalhe = "Sem API configurada. Use o portal oficial e lance Nada consta ou Apontamento."
        resultado = "pendente"
    execute(
        """UPDATE driver_documents
           SET notes=?, number=COALESCE(NULLIF(number,''), ?), status=CASE WHEN ? IN ('aprovado','rejeitado') THEN ? ELSE status END
           WHERE id=?""",
        (detalhe, datetime.now().strftime("%Y%m%d%H%M"), resultado, resultado, doc["id"] if doc else 0),
    )
    audit("antecedentes_online", str(driver_id), f"{resultado} · {detalhe}", "info")
    flash("Consulta de antecedentes registrada: " + detalhe, "ok")
    return redirect(url_for("admin_driver_docs", driver_id=driver_id))


@app.route("/admin/avisos")
@login_required("admin")
def admin_avisos():
    rows = query(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 40",
        (session["user_id"],),
    )
    return render_template("admin_avisos.html", notes=rows)


@app.route("/admin/seguranca/antecedentes-api", methods=["POST"])
@login_required("admin")
def admin_antecedentes_api():
    url = (request.form.get("antecedentes_api_url") or "").strip()
    if url and not url.startswith("https://"):
        flash("A API de antecedentes precisa ser HTTPS.", "err")
        return redirect(url_for("admin_sql"))
    set_setting("antecedentes_api_url", url)
    audit("configurar_api_antecedentes", url or "vazia")
    flash("Endpoint de antecedentes salvo.", "ok")
    return redirect(url_for("admin_sql"))


@app.route("/admin/motoristas/<int:driver_id>/mensalidade", methods=["POST"])
@login_required("admin")
def admin_toggle_fee(driver_id):
    drv = query("SELECT * FROM drivers WHERE id=?", (driver_id,), one=True)
    if not drv:
        flash("Motorista não encontrado.", "err")
        return redirect(url_for("admin_drivers"))
    novo = "pendente" if drv["payment_status"] == "pago" else "pago"
    execute("UPDATE drivers SET payment_status=? WHERE id=?", (novo, driver_id))
    if novo == "pago":
        execute(
            """INSERT INTO payments (driver_id, amount, month, year, status, paid_at)
               VALUES (?,?,?,?,?,?)""",
            (
                driver_id,
                drv["monthly_fee"] or 50,
                datetime.now().month,
                datetime.now().year,
                "pago",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    audit("mensalidade_motorista", str(driver_id), novo)
    flash("Mensalidade marcada como " + novo + ".", "ok")
    return redirect(request.referrer or url_for("admin_drivers"))


@app.route("/admin/pagamentos")
@login_required("admin")
def admin_payments():
    pag = request.args.get("pag", "").strip()
    rota = request.args.get("rota", "").strip()
    q = request.args.get("q", "").strip()
    sql = """
        SELECT drivers.*, users.name, users.phone, routes.label AS route_label
        FROM drivers
        JOIN users ON users.id = drivers.user_id
        LEFT JOIN routes ON routes.id = drivers.route_id
        WHERE 1=1
    """
    args = []
    if pag in ("pago", "pendente"):
        sql += " AND drivers.payment_status=?"
        args.append(pag)
    if rota.isdigit():
        sql += " AND drivers.route_id=?"
        args.append(int(rota))
    if q:
        sql += " AND (users.name LIKE ? OR drivers.plate LIKE ? OR users.phone LIKE ?)"
        args.extend([f"%{q}%"] * 3)
    sql += " ORDER BY CASE drivers.payment_status WHEN 'pendente' THEN 0 ELSE 1 END, users.name"
    raw = query(sql, args)
    drivers = []
    for row in raw:
        d = dict(row)
        d["phone_fmt"] = fmt_phone(d.get("phone"))
        d["wa"] = "".join(ch for ch in str(d.get("phone") or "") if ch.isdigit())
        fee = float(d.get("monthly_fee") or 50)
        d["fee_fmt"] = f"{fee:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        drivers.append(d)
    rev = monthly_revenue()
    receita = float(rev["receita"] or 0)
    aberto = float(rev["total"] or 0) * 50 - receita
    if aberto < 0:
        aberto = 0
    receipts = query(
        """SELECT payments.*, users.name, drivers.plate
           FROM payments
           JOIN drivers ON drivers.id = payments.driver_id
           JOIN users ON users.id = drivers.user_id
           ORDER BY payments.id DESC LIMIT 8"""
    )
    routes = query("SELECT * FROM routes ORDER BY id")
    meses = {
        "January": "Janeiro", "February": "Fevereiro", "March": "Março",
        "April": "Abril", "May": "Maio", "June": "Junho",
        "July": "Julho", "August": "Agosto", "September": "Setembro",
        "October": "Outubro", "November": "Novembro", "December": "Dezembro",
    }
    month_label = f"{meses.get(datetime.now().strftime('%B'), '')} {datetime.now().year}"
    return render_template(
        "admin_pagamentos.html",
        drivers=drivers,
        rev=rev,
        receita_fmt=f"{receita:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
        aberto_fmt=f"{aberto:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
        receipts=receipts,
        routes=routes,
        pag=pag,
        rota=rota,
        q=q,
        month_label=month_label,
    )


@app.route("/admin/pagamentos/<int:driver_id>/marcar", methods=["POST"])
@login_required("admin")
def admin_mark_paid(driver_id):
    status = request.form.get("status", "pago")
    if status not in ("pago", "pendente"):
        status = "pago"
    execute("UPDATE drivers SET payment_status=? WHERE id=?", (status, driver_id))
    if status == "pago":
        now = datetime.now()
        execute(
            """INSERT INTO payments (driver_id, month, year, amount, status, paid_at)
               VALUES (?,?,?,50.00,'pago',?)""",
            (driver_id, now.month, now.year, now.isoformat(sep=" ", timespec="seconds")),
        )
        audit("mensalidade_paga", str(driver_id))
        flash("Mensalidade marcada como paga.", "ok")
    else:
        audit("mensalidade_pendente", str(driver_id), "", "warn")
        flash("Mensalidade marcada como pendente.", "warn")
    cache_bust("dash")
    return redirect(url_for("admin_payments"))


@app.route("/admin/pagamentos/bloquear-inadimplentes", methods=["POST"])
@login_required("admin")
def admin_block_unpaid():
    n = query(
        "SELECT COUNT(*) AS c FROM drivers WHERE payment_status='pendente' AND status!='bloqueado'",
        one=True,
    )
    execute("UPDATE drivers SET status='bloqueado' WHERE payment_status='pendente'")
    audit("bloquear_inadimplentes", "motoristas", "", "crit")
    flash(f"{n['c'] if n else 0} taxista(s) com taxa pendente saíram da fila.", "ok")
    return redirect(request.referrer or url_for("admin_payments"))


@app.route("/admin/corridas/<int:ride_id>/despachar", methods=["POST"])
@login_required("admin")
def admin_dispatch(ride_id):
    ride = query("SELECT * FROM rides WHERE id=?", (ride_id,), one=True)
    if not ride or ride["status"] != "pendente":
        flash("Essa corrida não está mais na fila.", "warn")
        return redirect(url_for("admin_dashboard"))
    driver_id = int(request.form.get("driver_id") or 0)
    drv = query("SELECT * FROM drivers WHERE id=?", (driver_id,), one=True)
    if not drv or drv["status"] != "ativo":
        flash("Escolha um taxista ativo da rota.", "err")
        return redirect(url_for("admin_dashboard"))
    if drv["route_id"] != ride["route_id"]:
        flash("Esse taxista não é desta rota.", "err")
        return redirect(url_for("admin_dashboard"))
    execute(
        "UPDATE rides SET driver_id=?, status='aceita' WHERE id=? AND status='pendente'",
        (driver_id, ride_id),
    )
    ensure_ride_fences(query("SELECT * FROM rides WHERE id=?", (ride_id,), one=True))
    if setting("driver_push_despacho", "1") == "1":
        notify_route_drivers(
            ride["route_id"],
            "Corrida despachada para você",
            f"A central mandou uma corrida. Endereço: {ride['pickup_address']}",
            ride_id,
            kind="despacho",
            only_user_id=drv["user_id"],
        )
    audit("despachar_corrida", str(ride_id), str(driver_id))
    cache_bust("dash")
    flash("Corrida despachada para o taxista.", "ok")
    return redirect(request.referrer or url_for("admin_rides"))


@app.route("/admin/corridas/<int:ride_id>/cancelar", methods=["POST"])
@login_required("admin")
def admin_cancel_ride(ride_id):
    execute(
        "UPDATE rides SET status='cancelada' WHERE id=? AND status IN ('pendente','aceita')",
        (ride_id,),
    )
    audit("cancelar_corrida_admin", str(ride_id), "", "warn")
    flash("Corrida cancelada pela central.", "ok")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/corridas")
@login_required("admin")
def admin_rides():
    sit = request.args.get("sit", "").strip()
    tipo = request.args.get("tipo", "").strip()
    rota = request.args.get("rota", "").strip()
    sql = """
        SELECT rides.*, routes.label AS route_label,
               u.name AS passenger_name, u.phone AS passenger_phone,
               du.name AS driver_name, d.plate
        FROM rides
        LEFT JOIN routes ON routes.id = rides.route_id
        LEFT JOIN users u ON u.id = rides.passenger_id
        LEFT JOIN drivers d ON d.id = rides.driver_id
        LEFT JOIN users du ON du.id = d.user_id
        WHERE 1=1
    """
    args = []
    if sit:
        sql += " AND rides.status=?"
        args.append(sit)
    if tipo in ("agendada", "imediata"):
        sql += " AND rides.kind=?"
        args.append(tipo)
    if rota.isdigit():
        sql += " AND rides.route_id=?"
        args.append(int(rota))
    sql += """ ORDER BY CASE rides.status
                 WHEN 'pendente' THEN 0 WHEN 'aceita' THEN 1
                 WHEN 'a_caminho' THEN 2 WHEN 'em_andamento' THEN 3
                 ELSE 4 END, rides.scheduled_at, rides.id DESC
               LIMIT 80"""
    raw = query(sql, args)
    rides = []
    for row in raw:
        item = dict(row)
        item["passenger_name"] = item.get("passenger_name") or "Passageiro"
        item["route_label"] = item.get("route_label") or "Rota"
        item["phone_fmt"] = fmt_phone(item.get("passenger_phone"))
        item["wa"] = "".join(ch for ch in str(item.get("passenger_phone") or "") if ch.isdigit())
        when = item.get("scheduled_at") or ""
        if when and "T" not in when:
            try:
                item["when_fmt"] = datetime.strptime(when[:16], "%Y-%m-%d %H:%M").strftime("%d/%m/%Y às %H:%M")
            except ValueError:
                item["when_fmt"] = when
        else:
            item["when_fmt"] = when
        rides.append(item)
    people_map = {}
    if rides:
        ids = tuple(r["id"] for r in rides)
        ph = ",".join("?" * len(ids))
        for p in query(f"SELECT * FROM ride_passengers WHERE ride_id IN ({ph})", ids):
            people_map.setdefault(p["ride_id"], []).append(p)
    livres = query(
        """
        SELECT drivers.id, users.name, drivers.route_id, drivers.plate
        FROM drivers JOIN users ON users.id = drivers.user_id
        WHERE drivers.status='ativo'
        ORDER BY users.name
        """
    )
    livres_by_route = {}
    for d in livres:
        livres_by_route.setdefault(d["route_id"], []).append(d)
    stats = query(
        """SELECT
             COUNT(*) AS total,
             COALESCE(SUM(CASE WHEN status='pendente' THEN 1 ELSE 0 END),0) AS fila,
             COALESCE(SUM(CASE WHEN status IN ('aceita','a_caminho','em_andamento') THEN 1 ELSE 0 END),0) AS curso,
             COALESCE(SUM(CASE WHEN kind='agendada' AND status NOT IN ('cancelada','concluida') THEN 1 ELSE 0 END),0) AS familia,
             COALESCE(SUM(CASE WHEN status='concluida' THEN 1 ELSE 0 END),0) AS feitas
           FROM rides""",
        one=True,
    )
    routes = query("SELECT * FROM routes ORDER BY id")
    return render_template(
        "admin_corridas.html",
        rides=rides,
        people_map=people_map,
        livres_by_route=livres_by_route,
        stats=stats,
        routes=routes,
        sit=sit,
        tipo=tipo,
        rota=rota,
        agora=datetime.now().strftime("%H:%M:%S"),
    )


@app.route("/admin/config", methods=["GET", "POST"])
@login_required("admin")
def admin_config():
    routes = query("SELECT * FROM routes ORDER BY id")
    if request.method == "POST":
        for r in routes:
            price = request.form.get(f"price_{r['id']}")
            dist = request.form.get(f"dist_{r['id']}")
            dur = request.form.get(f"dur_{r['id']}")
            try:
                execute(
                    "UPDATE routes SET price=?, distance_km=?, duration_min=? WHERE id=?",
                    (float(price), float(dist), int(dur), r["id"]),
                )
            except (TypeError, ValueError):
                pass
        flash("Configurações das rotas salvas.", "ok")
        return redirect(url_for("admin_config"))
    cfg = {r["key"]: r["value"] for r in query("SELECT * FROM security_settings")}
    events = query(
        """SELECT geofence_events.*, rides.pickup_address
           FROM geofence_events
           LEFT JOIN rides ON rides.id = geofence_events.ride_id
           ORDER BY geofence_events.id DESC LIMIT 12"""
    )
    return render_template(
        "admin_config.html",
        routes=query("SELECT * FROM routes ORDER BY id"),
        cfg=cfg,
        geo_events=events,
    )


@app.route("/admin/whatsapp", methods=["POST"])
@login_required("admin")
def admin_whatsapp():
    set_setting("wa_enabled", "1" if request.form.get("wa_enabled") else "0")
    set_setting("wa_phone", "".join(ch for ch in request.form.get("wa_phone", "") if ch.isdigit()))
    set_setting("wa_phone_id", request.form.get("wa_phone_id", "").strip())
    set_setting("wa_template", request.form.get("wa_template", "").strip())
    token = request.form.get("wa_token", "").strip()
    if token:
        set_setting("wa_token", token)
    test = request.form.get("wa_test", "").strip()
    if test:
        ok = notify_whatsapp(test, "Teste da Central Táxi Cone Sul no WhatsApp.")
        flash("WhatsApp de teste enviado." if ok else "Não enviou. Confira token/Phone ID ou use o link wa.me.", "ok" if ok else "warn")
    else:
        flash("WhatsApp salvo.", "ok")
    audit("whatsapp_config")
    return redirect(url_for("admin_config"))


@app.route("/admin/trafego")
@login_required("admin")
def admin_traffic():
    hits = query("SELECT * FROM traffic_hits ORDER BY id DESC LIMIT 80")
    return render_template("admin_trafego.html", hits=hits)


@app.route("/admin/tema", methods=["POST"])
@login_required("admin")
def admin_theme():
    allowed = {"auto", "amazonia", "noite", "ouro", "violeta", "claro", "uber"}
    theme = (request.form.get("ui_theme") or "amazonia").strip()
    if theme not in allowed:
        theme = "amazonia"
    set_setting("ui_theme", theme)
    audit("trocar_tema", theme)
    flash("Tema visual atualizado.", "ok")
    return redirect(request.referrer or url_for("admin_config"))


@app.route("/admin/geofencing", methods=["POST"])
@login_required("admin")
def admin_geofencing():
    set_setting("geo_enabled", "1" if request.form.get("geo_enabled") else "0")
    set_setting("geo_auto_status", "1" if request.form.get("geo_auto_status") else "0")
    casa = request.form.get("geo_radius_casa", "200").strip()
    dest = request.form.get("geo_radius_destino", "400").strip()
    set_setting("geo_radius_casa", casa if casa.isdigit() else "200")
    set_setting("geo_radius_destino", dest if dest.isdigit() else "400")
    audit("atualizar_geofencing", f"casa={casa}", dest)
    flash("Geofencing automático atualizado.", "ok")
    return redirect(url_for("admin_config"))


@app.route("/admin/push-motoristas", methods=["POST"])
@login_required("admin")
def admin_driver_push_config():
    set_setting("driver_push_enabled", "1" if request.form.get("driver_push_enabled") else "0")
    set_setting("driver_push_fila", "1" if request.form.get("driver_push_fila") else "0")
    set_setting("driver_push_despacho", "1" if request.form.get("driver_push_despacho") else "0")
    set_setting("driver_push_email", "1" if request.form.get("driver_push_email") else "0")
    audit("atualizar_push_motoristas", "settings")
    flash("Notificações push dos motoristas atualizadas.", "ok")
    return redirect(url_for("admin_config"))


@app.route("/admin/push-motoristas/teste", methods=["POST"])
@login_required("admin")
def admin_driver_push_test():
    n = 0
    for row in query(
        """SELECT users.id FROM drivers JOIN users ON users.id=drivers.user_id
           WHERE drivers.status='ativo' AND users.active=1"""
    ):
        push_notification(
            row["id"],
            "Teste de push da central",
            "Se você está vendo isto no app do taxista, o aviso está ligado.",
            kind="teste",
            send_email=False,
        )
        n += 1
    flash(f"Teste enviado para {n} motorista(s) ativo(s).", "ok")
    return redirect(url_for("admin_config"))


@app.route("/admin/animacoes")
@login_required("admin")
def admin_animations():
    return render_template("admin_animacoes.html")


@app.route("/admin/seguranca")
@login_required("admin")
def admin_security():
    q = request.args.get("q", "").strip()
    perfil = request.args.get("perfil", "").strip()
    logs = query("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 25")
    attempts = query("SELECT * FROM login_attempts ORDER BY id DESC LIMIT 12")
    fails_24h = query(
        """SELECT COUNT(*) AS c FROM login_attempts
           WHERE success=0 AND created_at >= datetime('now','-1 day')""",
        one=True,
    )
    ok_24h = query(
        """SELECT COUNT(*) AS c FROM login_attempts
           WHERE success=1 AND created_at >= datetime('now','-1 day')""",
        one=True,
    )
    locked = query(
        """SELECT * FROM users
           WHERE (locked_until IS NOT NULL AND locked_until != '')
              OR active=0
           ORDER BY name"""
    )
    sql = """SELECT id, name, phone, email, role, active, failed_logins, locked_until,
                    last_login_at, last_login_ip, totp_enabled
             FROM users WHERE 1=1"""
    args = []
    if q:
        sql += " AND (name LIKE ? OR phone LIKE ? OR IFNULL(email,'') LIKE ?)"
        args.extend([f"%{q}%"] * 3)
    if perfil in ("admin", "motorista", "passageiro"):
        sql += " AND role=?"
        args.append(perfil)
    sql += " ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'passageiro' THEN 1 ELSE 2 END, name"
    raw_users = query(sql, args)
    users = []
    for row in raw_users:
        u = dict(row)
        u["phone_fmt"] = fmt_phone(u.get("phone"))
        u["last_fmt"] = fmt_when(u.get("last_login_at"))
        users.append(u)
    counts = query(
        """SELECT
             SUM(CASE WHEN role='admin' THEN 1 ELSE 0 END) AS admins,
             SUM(CASE WHEN role='motorista' THEN 1 ELSE 0 END) AS motoristas,
             SUM(CASE WHEN role='passageiro' THEN 1 ELSE 0 END) AS passageiros,
             SUM(CASE WHEN totp_enabled=1 THEN 1 ELSE 0 END) AS com_2fa
           FROM users""",
        one=True,
    )
    cfg = {r["key"]: r["value"] for r in query("SELECT * FROM security_settings")}
    outbox = query("SELECT * FROM email_outbox ORDER BY id DESC LIMIT 8")
    err_open = query(
        "SELECT COUNT(*) AS c FROM error_logs WHERE resolved=0",
        one=True,
    )
    err_24h = query(
        """SELECT COUNT(*) AS c FROM error_logs
           WHERE created_at >= datetime('now','-1 day')""",
        one=True,
    )
    return render_template(
        "admin_seguranca.html",
        logs=logs,
        attempts=attempts,
        fails_24h=(fails_24h["c"] if fails_24h else 0),
        ok_24h=(ok_24h["c"] if ok_24h else 0),
        locked=locked,
        users=users,
        cfg=cfg,
        outbox=outbox,
        err_open=(err_open["c"] if err_open else 0),
        err_24h=(err_24h["c"] if err_24h else 0),
        q=q,
        perfil=perfil,
        counts=counts,
    )


@app.route("/admin/seguranca/config", methods=["POST"])
@login_required("admin")
def admin_security_config():
    for key in ("max_fails", "lock_minutes", "session_minutes", "min_password"):
        raw = (request.form.get(key) or "").strip()
        if raw.isdigit() and int(raw) > 0:
            set_setting(key, raw)
    set_setting("require_2fa_admin", "1" if request.form.get("require_2fa_admin") else "0")
    audit("atualizar_politica_seguranca", "settings")
    flash("Política de segurança atualizada.", "ok")
    return redirect(url_for("admin_security"))


@app.route("/admin/seguranca/alertas", methods=["POST"])
@login_required("admin")
def admin_alert_config():
    set_setting("alert_crit_enabled", "1" if request.form.get("alert_crit_enabled") else "0")
    set_setting("alert_notify_admins", "1" if request.form.get("alert_notify_admins") else "0")
    email = norm_email(request.form.get("alert_email", ""))
    if email and not valid_email(email):
        flash("E-mail de alerta inválido.", "err")
        return redirect(url_for("admin_errors"))
    set_setting("alert_email", email)
    cool = (request.form.get("alert_cooldown_min") or "15").strip()
    set_setting("alert_cooldown_min", cool if cool.isdigit() else "15")
    set_setting("slack_enabled", "1" if request.form.get("slack_enabled") else "0")
    set_setting("teams_enabled", "1" if request.form.get("teams_enabled") else "0")
    slack_url = request.form.get("slack_webhook", "").strip()
    teams_url = request.form.get("teams_webhook", "").strip()
    if slack_url and not slack_url.startswith("https://"):
        flash("Webhook do Slack precisa ser HTTPS.", "err")
        return redirect(url_for("admin_errors"))
    if teams_url and not teams_url.startswith("https://"):
        flash("Webhook do Teams precisa ser HTTPS.", "err")
        return redirect(url_for("admin_errors"))
    if slack_url:
        set_setting("slack_webhook", slack_url)
    if teams_url:
        set_setting("teams_webhook", teams_url)
    if request.form.get("clear_slack"):
        set_setting("slack_webhook", "")
    if request.form.get("clear_teams"):
        set_setting("teams_webhook", "")
    audit("atualizar_alertas_criticos", email or "slack/teams")
    flash("Alertas críticos atualizados.", "ok")
    return redirect(url_for("admin_errors"))


@app.route("/admin/seguranca/alertas/teste", methods=["POST"])
@login_required("admin")
def admin_alert_test():
    log_error(
        "Teste manual de alerta crítico",
        source="alerta",
        level="crit",
        detail="Disparado pelo administrador para validar e-mail e banner.",
    )
    flash("Alerta de teste disparado. Confira o e-mail e o monitor.", "ok")
    return redirect(url_for("admin_errors"))


@app.route("/admin/erros/alertas/vistos", methods=["POST"])
@login_required("admin")
def admin_alerts_seen():
    execute("UPDATE error_alerts SET seen=1 WHERE seen=0")
    return redirect(request.referrer or url_for("admin_errors"))


@app.route("/admin/seguranca/smtp", methods=["POST"])
@login_required("admin")
def admin_smtp_config():
    set_setting("smtp_enabled", "1" if request.form.get("smtp_enabled") else "0")
    set_setting("smtp_host", request.form.get("smtp_host", "").strip())
    port = (request.form.get("smtp_port") or "587").strip()
    set_setting("smtp_port", port if port.isdigit() else "587")
    set_setting("smtp_user", request.form.get("smtp_user", "").strip())
    new_pass = request.form.get("smtp_pass", "")
    if new_pass.strip():
        set_setting("smtp_pass", new_pass)
    set_setting("smtp_from", request.form.get("smtp_from", "").strip())
    set_setting("smtp_from_name", request.form.get("smtp_from_name", "").strip())
    sec = request.form.get("smtp_security", "starttls")
    if sec not in ("starttls", "ssl", "none"):
        sec = "starttls"
    set_setting("smtp_security", sec)
    audit("atualizar_smtp", setting("smtp_host"), sec)
    flash("Serviço SMTP salvo.", "ok")
    return redirect(url_for("admin_security"))


@app.route("/admin/seguranca/smtp/teste", methods=["POST"])
@login_required("admin")
def admin_smtp_test():
    user = current_user()
    dest = norm_email(request.form.get("test_email") or user["email"] or user["recovery_email"] or "")
    if not valid_email(dest):
        flash("Informe um e-mail válido para o teste.", "err")
        return redirect(url_for("admin_security"))
    raw = issue_token(user["id"], dest, purpose="smtp_test", minutes=10)
    sent, info = queue_email(
        dest,
        "Teste SMTP — Central Táxi Cone Sul",
        (
            f"Olá, {user['name']}.\n\n"
            "Este é um e-mail de teste do serviço SMTP da central.\n"
            f"Token de verificação: {raw}\n"
            "Se chegou, o SMTP está funcionando.\n"
        ),
        kind="teste_smtp",
        user_id=user["id"],
    )
    audit("teste_smtp", dest, info if not sent else "enviado", "info")
    if sent:
        flash(f"E-mail de teste enviado para {dest}.", "ok")
    elif info == "smtp_desligado":
        flash("SMTP ainda está desligado. Ative e salve o servidor antes de testar.", "warn")
    else:
        flash(f"Falha no SMTP: {info}", "err")
    return redirect(url_for("admin_security"))


@app.route("/admin/seguranca/usuario/<int:user_id>/desativar", methods=["POST"])
@login_required("admin")
def admin_deactivate_user(user_id):
    me = current_user()
    if me and me["id"] == user_id:
        flash("Você não pode desativar a própria conta.", "err")
        return redirect(url_for("admin_security"))
    execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
    audit("desativar_usuario", str(user_id), "", "crit")
    flash("Conta desativada.", "ok")
    return redirect(url_for("admin_security"))


@app.route("/admin/seguranca/usuario/<int:user_id>/ativar", methods=["POST"])
@login_required("admin")
def admin_activate_user(user_id):
    execute(
        "UPDATE users SET active=1, failed_logins=0, locked_until=NULL WHERE id=?",
        (user_id,),
    )
    audit("ativar_usuario", str(user_id))
    flash("Conta reativada e desbloqueada.", "ok")
    return redirect(url_for("admin_security"))


@app.route("/admin/seguranca/usuario/<int:user_id>/reset-senha", methods=["POST"])
@login_required("admin")
def admin_reset_password(user_id):
    temp = "ConeSul@" + secrets.token_hex(2)
    execute(
        "UPDATE users SET password_hash=?, must_change_password=1, failed_logins=0, locked_until=NULL WHERE id=?",
        (generate_password_hash(temp), user_id),
    )
    audit("reset_senha", str(user_id), "", "warn")
    flash(f"Senha temporária: {temp} — a pessoa precisará trocar no próximo acesso.", "ok")
    return redirect(url_for("admin_security"))


@app.route("/admin/seguranca/usuario/<int:user_id>/reset-2fa", methods=["POST"])
@login_required("admin")
def admin_reset_2fa(user_id):
    execute(
        "UPDATE users SET totp_secret=NULL, totp_enabled=0, totp_backup=NULL WHERE id=?",
        (user_id,),
    )
    audit("reset_2fa", str(user_id), "", "crit")
    flash("2FA dessa conta foi removido. A pessoa precisará cadastrar de novo.", "ok")
    return redirect(url_for("admin_security"))


@app.route("/admin/erros")
@login_required("admin")
def admin_errors():
    level = request.args.get("level", "").strip()
    source = request.args.get("source", "").strip()
    only_open = request.args.get("abertos", "1") != "0"
    sql = "SELECT * FROM error_logs WHERE 1=1"
    args = []
    if only_open:
        sql += " AND resolved=0"
    if level:
        sql += " AND level=?"
        args.append(level)
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY id DESC LIMIT 80"
    rows = query(sql, args)
    sources = query("SELECT DISTINCT source FROM error_logs ORDER BY source")
    stats = query(
        """SELECT
             COUNT(*) AS total,
             SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS abertos,
             SUM(CASE WHEN level='crit' AND resolved=0 THEN 1 ELSE 0 END) AS criticos,
             SUM(CASE WHEN created_at >= datetime('now','-1 day') THEN 1 ELSE 0 END) AS dia
           FROM error_logs""",
        one=True,
    )
    cfg = {r["key"]: r["value"] for r in query("SELECT * FROM security_settings")}
    sent_alerts = query("SELECT * FROM error_alerts ORDER BY id DESC LIMIT 12")
    return render_template(
        "admin_erros.html",
        rows=rows,
        sources=sources,
        stats=stats,
        level=level,
        source=source,
        only_open=only_open,
        log_file=LOG_FILE,
        cfg=cfg,
        sent_alerts=sent_alerts,
    )


@app.route("/admin/erros/<int:error_id>/resolver", methods=["POST"])
@login_required("admin")
def admin_resolve_error(error_id):
    execute("UPDATE error_logs SET resolved=1 WHERE id=?", (error_id,))
    audit("erro_resolvido", str(error_id))
    return redirect(request.referrer or url_for("admin_errors"))


@app.route("/admin/erros/resolver-todos", methods=["POST"])
@login_required("admin")
def admin_resolve_all_errors():
    execute("UPDATE error_logs SET resolved=1 WHERE resolved=0")
    audit("erros_resolvidos_lote", "error_logs")
    flash("Todos os erros abertos foram marcados como resolvidos.", "ok")
    return redirect(url_for("admin_errors"))


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or request.form
    identifier = (data.get("login") or data.get("email") or data.get("phone") or "").strip()
    password = data.get("password") or ""
    user = find_user_by_login(identifier)
    if not user or not user["active"] or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "erro": "credenciais inválidas"}), 401
    token = make_jwt(user)
    return jsonify(
        {
            "ok": True,
            "token": token,
            "tipo": "Bearer",
            "expira_min": JWT_TTL_MIN,
            "usuario": {"id": user["id"], "nome": user["name"], "perfil": user["role"]},
        }
    )


@app.route("/api/me")
@jwt_required()
def api_me():
    u = request.jwt_user
    return jsonify(
        {
            "ok": True,
            "id": u["id"],
            "nome": u["name"],
            "perfil": u["role"],
            "telefone": u["phone"],
            "email": u["email"],
            "banco": "sqlite",
            "arquivo": os.path.basename(DB_PATH),
        }
    )


@app.route("/api/rotas")
@jwt_required()
def api_rotas():
    rows = query("SELECT id, origin, destination, label, distance_km, price, duration_min FROM routes ORDER BY id")
    return jsonify({"ok": True, "rotas": [dict(r) for r in rows]})


@app.route("/api/corridas")
@jwt_required()
def api_corridas():
    u = request.jwt_user
    if u["role"] == "admin":
        rows = query(
            """SELECT rides.id, rides.status, rides.kind, rides.pickup_address, rides.scheduled_at,
                      routes.label AS rota, pu.name AS passageiro
               FROM rides
               JOIN routes ON routes.id = rides.route_id
               JOIN users pu ON pu.id = rides.passenger_id
               ORDER BY rides.id DESC LIMIT 50"""
        )
    elif u["role"] == "motorista":
        drv = driver_record(u["id"])
        if not drv:
            return jsonify({"ok": False, "erro": "motorista sem frota"}), 400
        rows = query(
            """SELECT rides.id, rides.status, rides.kind, rides.pickup_address, routes.label AS rota
               FROM rides JOIN routes ON routes.id = rides.route_id
               WHERE rides.route_id=? AND rides.status='pendente'
               ORDER BY rides.id DESC LIMIT 50""",
            (drv["route_id"],),
        )
    else:
        rows = query(
            """SELECT rides.id, rides.status, rides.kind, rides.pickup_address, routes.label AS rota
               FROM rides JOIN routes ON routes.id = rides.route_id
               WHERE rides.passenger_id=?
               ORDER BY rides.id DESC LIMIT 50""",
            (u["id"],),
        )
    return jsonify({"ok": True, "corridas": [dict(r) for r in rows]})


@app.route("/admin/banco")
@login_required("admin")
def admin_sql():
    tables = query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    counts = []
    for t in tables:
        n = query(f"SELECT COUNT(*) AS c FROM {t['name']}", one=True)
        counts.append({"tabela": t["name"], "linhas": n["c"] if n else 0})
    return render_template(
        "admin_sql.html",
        db_path=DB_PATH,
        engine="SQLite (SQL)",
        counts=counts,
        jwt_ttl=JWT_TTL_MIN,
        cfg={r["key"]: r["value"] for r in query("SELECT * FROM security_settings")},
    )


@app.errorhandler(404)
def on_404(_e):
    if request.path.startswith("/admin") or request.path.startswith("/api"):
        log_error("Página não encontrada", source="http", level="warn", path=request.path)
    return render_template(
        "error.html",
        code=404,
        title="Não encontrado",
        text="Essa página não existe na central.",
    ), 404


@app.errorhandler(500)
def on_500(e):
    log_error(
        "Erro interno 500",
        source="http",
        level="crit",
        detail="".join(traceback.format_exception(type(e), e, e.__traceback__)),
    )
    return (
        render_template(
            "error.html",
            code=500,
            title="Falha interna",
            text="O erro foi gravado no monitor. Tente de novo ou avise o administrador.",
        ),
        500,
    )


# ---------------------------------------------------------------------------
# API simples (contadores do mapa)
# ---------------------------------------------------------------------------
@app.route("/api/rotas")
def api_routes():
    data = []
    for r in route_availability():
        data.append(
            {
                "id": r["id"],
                "label": r["label"],
                "livres": r["livres"] or 0,
                "total": r["total_taxis"] or 0,
                "price": r["price"],
            }
        )
    return jsonify(data)


# ---------------------------------------------------------------------------
init_schema()
seed_if_empty()

if __name__ == "__main__":
    print("\n  Central Táxi Cone Sul")
    print("  http://127.0.0.1:5000")
    print("  Primeiro acesso admin: beto@conesul.taxi / admin123\n")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
