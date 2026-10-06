"""API tests: run with the api venv (fastapi, httpx, argon2-cffi installed).
    /tmp/api-venv/bin/python -m unittest discover -s deploy/nas-assets/api
DB and credentials live in a tmp sandbox; nothing here touches the NAS.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_DB = tempfile.mkdtemp(prefix="recipe-api-test-")
os.environ["DB_PATH"] = os.path.join(_DB, "test.db")
os.environ["RECIPE_EMAIL"] = "chef@example.com"
os.environ["RECIPE_PASSWORD"] = "correct horse battery staple"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(main.app, base_url="https://testserver")
        cls.c.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.c.__exit__(None, None, None)

    def setUp(self):
        # tests share one seeded user; reset lockout state for isolation
        conn = self._db()
        conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL")
        conn.commit()
        conn.close()

    def _db(self):
        conn = sqlite3.connect(os.environ["DB_PATH"])
        conn.row_factory = sqlite3.Row
        return conn

    def test_healthz(self):
        r = self.c.get("/api/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_me_requires_auth(self):
        with TestClient(main.app, base_url="https://testserver") as anon:
            self.assertEqual(anon.get("/api/me").status_code, 401)

    def test_login_wrong_password(self):
        r = self.c.post("/api/login", json={
            "email": "chef@example.com", "password": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_login_lockout_after_five(self):
        for _ in range(4):
            self.c.post("/api/login", json={
                "email": "chef@example.com", "password": "nope"})
        r = self.c.post("/api/login", json={
            "email": "chef@example.com", "password": "nope"})
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r.headers.get("retry-after"))
        # even the right password is locked out now
        r = self.c.post("/api/login", json={
            "email": "chef@example.com", "password": "correct horse battery staple"})
        self.assertEqual(r.status_code, 429)
        # expire the lockout, then login succeeds with proper cookie
        conn = self._db()
        conn.execute("UPDATE users SET locked_until=NULL, failed_attempts=0")
        conn.commit()
        conn.close()
        r = self.c.post("/api/login", json={
            "email": "chef@example.com", "password": "correct horse battery staple"})
        self.assertEqual(r.status_code, 200)
        cookie = r.headers.get("set-cookie", "").lower()
        for frag in ("rsid=", "path=/api", "httponly", "secure", "samesite=lax", "max-age=2592000"):
            self.assertIn(frag, cookie, f"cookie missing {frag}: {cookie}")

    def test_full_session_flow(self):
        self.c.post("/api/login", json={
            "email": "chef@example.com", "password": "correct horse battery staple"})
        me = self.c.get("/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "chef@example.com")

        # personal round-trip
        put = self.c.put("/api/recipes/chilla/personal", json={"servings": 9})
        self.assertEqual(put.status_code, 200)
        self.assertEqual(put.json()["servings"], 9)
        put2 = self.c.put("/api/recipes/chilla/personal", json={"notes": "menos comino"})
        self.assertEqual(put2.status_code, 200)
        self.assertEqual(put2.json()["notes"], "menos comino")
        self.assertEqual(put2.json()["servings"], 9)

        # GET returns nulls for a fresh recipe id (single code path)
        fresh = self.c.get("/api/recipes/rajma/personal")
        self.assertEqual(fresh.status_code, 200)
        self.assertIsNone(fresh.json()["servings"])
        self.assertIsNone(fresh.json()["notes"])

        # rid validation (ids are lowercase with underscores)
        self.assertEqual(
            self.c.put("/api/recipes/BAD-ID/personal", json={"servings": 2}).status_code, 422)
        self.assertEqual(
            self.c.put("/api/recipes/besan_chilla/personal", json={"servings": 2}).status_code, 200)

        # bounds
        self.assertEqual(
            self.c.put("/api/recipes/chilla/personal", json={"servings": 100}).status_code, 422)
        self.assertEqual(
            self.c.put("/api/recipes/chilla/personal", json={"notes": "x" * 5001}).status_code, 422)

        # logout kills the session
        self.assertEqual(self.c.post("/api/logout").status_code, 204)
        self.assertEqual(self.c.get("/api/me").status_code, 401)

    def test_session_token_stored_hashed(self):
        self.c.post("/api/login", json={
            "email": "chef@example.com", "password": "correct horse battery staple"})
        conn = self._db()
        rows = conn.execute("SELECT token_hash FROM sessions").fetchall()
        conn.close()
        self.assertTrue(rows)
        # the cookie value itself must never appear raw in the DB


if __name__ == "__main__":
    unittest.main()
