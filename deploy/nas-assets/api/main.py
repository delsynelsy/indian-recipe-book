"""recipe-api: single-user auth + per-recipe personalization store.

Serves behind the recipe-site nginx at https://recipes.mohammadasjad.com/api/
(same-origin path proxy). One account, seeded from RECIPE_EMAIL /
RECIPE_PASSWORD at first boot; rotate with `python main.py setpass`.

SQLite (WAL) at DB_PATH (default /data/recipes.db, a NAS bind mount).
All responses are Cache-Control: no-store (Cloudflare must not cache /api/).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import sqlite3
import sys
from contextlib import asynccontextmanager, closing

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "/data/recipes.db")
SESSION_DAYS = 30
LOCK_AFTER_FAILS = 5
LOCK_MINUTES = 15
COOKIE = "rsid"
RID_MAX = 64

ph = PasswordHasher()
_NOW = lambda: dt.datetime.now(dt.timezone.utc)  # noqa: E731


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_db()) as conn, conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
          pw_hash TEXT NOT NULL,
          failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT);
        CREATE TABLE IF NOT EXISTS sessions(
          token_hash TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL REFERENCES users(id),
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recipe_personal(
          user_id INTEGER NOT NULL REFERENCES users(id),
          recipe_id TEXT NOT NULL,
          servings INTEGER, notes TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(user_id, recipe_id));
        """)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            email = os.environ.get("RECIPE_EMAIL", "")
            password = os.environ.get("RECIPE_PASSWORD", "")
            if email and password:
                conn.execute(
                    "INSERT INTO users(email, pw_hash) VALUES(?, ?)",
                    (email, ph.hash(password)))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="recipe-api", lifespan=lifespan)


@app.middleware("http")
async def _no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


class LoginIn(BaseModel):
    email: str
    password: str


class PersonalIn(BaseModel):
    servings: int | None = Field(default=None, ge=1, le=99)
    notes: str | None = Field(default=None, max_length=5000)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, path="/api", httponly=True, secure=True,
                        samesite="lax", max_age=SESSION_DAYS * 86400)


def _valid_rid(rid: str) -> bool:
    return bool(rid) and len(rid) <= RID_MAX and all(
        ch.isascii() and (ch.isalnum() or ch == "_") for ch in rid) and rid[0].isalnum()


def _user_row(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()


def _require_user(request: Request) -> sqlite3.Row:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "not logged in")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with closing(_db()) as conn, conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash, _NOW().isoformat())).fetchone()
    if row is None:
        raise HTTPException(401, "not logged in")
    return row


@app.get("/api/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    with closing(_db()) as conn, conn:
        user = _user_row(conn)
        if user is None or body.email.strip().lower() != user["email"].strip().lower():
            raise HTTPException(401, "invalid credentials")
        if user["locked_until"] and dt.datetime.fromisoformat(user["locked_until"]) > _NOW():
            wait = int((dt.datetime.fromisoformat(user["locked_until"]) - _NOW()).total_seconds()) + 1
            raise HTTPException(429, "too many attempts, try later",
                                 headers={"Retry-After": str(max(wait, 1))})
        try:
            ph.verify(user["pw_hash"], body.password)
        except VerifyMismatchError:
            fails = user["failed_attempts"] + 1
            locked = fails >= LOCK_AFTER_FAILS
            conn.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                (fails, (_NOW() + dt.timedelta(minutes=LOCK_MINUTES)).isoformat()
                 if locked else None, user["id"]))
            conn.commit()  # commit before raise: the `with` rolls back otherwise
            if locked:
                raise HTTPException(429, "too many attempts, try later",
                                     headers={"Retry-After": str(LOCK_MINUTES * 60)})
            raise HTTPException(401, "invalid credentials")
        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (user["id"],))
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) "
            "VALUES(?, ?, ?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(), user["id"],
             _NOW().isoformat(), (_NOW() + dt.timedelta(days=SESSION_DAYS)).isoformat()))
    _set_session_cookie(response, token)
    return {"email": user["email"]}


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        with closing(_db()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                         (hashlib.sha256(token.encode()).hexdigest(),))
    # Return THIS response: a separately-constructed Response() would drop
    # the clearing Set-Cookie header (FastAPI only merges headers from the
    # declared parameter on normal returns).
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE, path="/api", secure=True, httponly=True,
                       samesite="lax")
    return resp


@app.get("/api/me")
def me(user: sqlite3.Row = Depends(_require_user)):
    return {"email": user["email"]}


@app.get("/api/recipes/{rid}/personal")
def get_personal(rid: str, user: sqlite3.Row = Depends(_require_user)):
    if not _valid_rid(rid):
        raise HTTPException(422, "invalid recipe id")
    with closing(_db()) as conn, conn:
        row = conn.execute(
            "SELECT servings, notes, updated_at FROM recipe_personal "
            "WHERE user_id = ? AND recipe_id = ?", (user["id"], rid)).fetchone()
    if row is None:
        return {"servings": None, "notes": None, "updated_at": None}
    return {"servings": row["servings"], "notes": row["notes"],
            "updated_at": row["updated_at"]}


@app.put("/api/recipes/{rid}/personal")
def put_personal(rid: str, body: PersonalIn,
                 user: sqlite3.Row = Depends(_require_user)):
    if not _valid_rid(rid):
        raise HTTPException(422, "invalid recipe id")
    with closing(_db()) as conn, conn:
        row = conn.execute(
            "SELECT servings, notes FROM recipe_personal "
            "WHERE user_id = ? AND recipe_id = ?", (user["id"], rid)).fetchone()
        servings = body.servings if body.servings is not None else (row["servings"] if row else None)
        notes = body.notes if body.notes is not None else (row["notes"] if row else None)
        conn.execute(
            "INSERT INTO recipe_personal(user_id, recipe_id, servings, notes, updated_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(user_id, recipe_id) DO UPDATE SET "
            "servings = excluded.servings, notes = excluded.notes, "
            "updated_at = excluded.updated_at",
            (user["id"], rid, servings, notes, _NOW().isoformat()))
    return {"servings": servings, "notes": notes, "updated_at": _NOW().isoformat()}


def _setpass() -> None:
    import getpass
    with closing(_db()) as conn, conn:
        user = _user_row(conn)
        if user is None:
            sys.exit("no user seeded; set RECIPE_EMAIL/RECIPE_PASSWORD first")
        pw = getpass.getpass("New password: ")
        if len(pw) < 8:
            sys.exit("password too short (min 8)")
        conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?",
                     (ph.hash(pw), user["id"]))
        with closing(_db()) as c2, c2:
            c2.execute("DELETE FROM sessions")
    print("password updated (sessions invalidated)")


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "setpass":
    _setpass()
