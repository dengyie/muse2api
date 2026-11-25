"""Tests for self-maintained Muse auto-login (credentials, store, circuit breaker, URL allowlist)."""
from __future__ import annotations

import os
import tempfile
import time
import unittest

from auto_login import _allowed_muse_url
from config import Config
from credentials import (
    _extract_muse_code_or_link,
    _parse_mail_ts,
    generate_totp,
    mask_identifier,
    public_auth_profile,
    seal_secret,
    unseal_secret,
)
from store import Store


class TestCredentials(unittest.TestCase):
    def test_seal_unseal_roundtrip_and_tamper(self):
        key = "test-master-key-32bytes-long-ok"
        ct = seal_secret("s3cr3t-pass!", key)
        self.assertTrue(ct.startswith("v1:"))
        self.assertNotIn("s3cr3t-pass!", ct)
        self.assertEqual(unseal_secret(ct, key), "s3cr3t-pass!")
        with self.assertRaises(ValueError):
            unseal_secret(ct, "wrong-key")
        with self.assertRaises(ValueError):
            unseal_secret(ct[:-2] + "AA", key)

    def test_totp_rfc6238_vector(self):
        # RFC 6238 SHA1 test vector: secret="12345678901234567890" (Base32: GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ)
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(generate_totp(secret, ts=59, digits=8), "94287082")
        self.assertEqual(len(generate_totp(secret, ts=59, digits=6)), 6)

    def test_public_auth_profile_redaction(self):
        prof = {
            "auth_type": "password",
            "identifier": "mango@example.com",
            "password_enc": "v1:secret:ct:mac",
            "totp_secret_enc": "v1:secret2:ct:mac",
            "onemail_jwt_enc": "v1:jwt:ct:mac",
            "onemail_base": "https://mail-api.mangoqwq.com",
            "auto_relogin": True,
            "needs_manual": False,
        }
        pub = public_auth_profile(prof)
        self.assertIsNotNone(pub)
        self.assertEqual(pub["identifier_masked"], "m***o@example.com")
        self.assertTrue(pub["has_password"])
        self.assertTrue(pub["has_totp"])
        self.assertTrue(pub["has_onemail"])
        for forbidden in ("password_enc", "totp_secret_enc", "onemail_jwt_enc", "password"):
            self.assertNotIn(forbidden, pub)
        self.assertEqual(mask_identifier("ab@x.com"), "a***@x.com")

    def test_extract_otp_and_magic_link(self):
        code, link = _extract_muse_code_or_link({
            "sender": "Muse <noreply@muse.ai>",
            "subject": "Your Muse verification code",
            "text": "Use code 482910 to sign in.",
        })
        self.assertEqual(code, "482910")
        self.assertIsNone(link)

        code2, link2 = _extract_muse_code_or_link({
            "sender": "Muse <noreply@muse.ai>",
            "subject": "Sign in to Muse",
            "text": "Click https://muse.ai/auth/verify?token=abc123 to continue.",
        })
        self.assertIsNone(code2)
        self.assertEqual(link2, "https://muse.ai/auth/verify?token=abc123")

    def test_allowed_muse_url(self):
        self.assertTrue(_allowed_muse_url("https://muse.ai/auth/callback"))
        self.assertTrue(_allowed_muse_url("https://auth.muse.ai/login"))
        self.assertFalse(_allowed_muse_url("https://evilmuse.ai/login"))
        self.assertFalse(_allowed_muse_url("javascript:alert(1)"))

    def test_parse_mail_ts(self):
        ts1 = _parse_mail_ts("2026-11-25 10:15:00")
        ts2 = _parse_mail_ts("2026-11-25T10:15:00.123Z")
        self.assertIsNotNone(ts1)
        self.assertEqual(ts1, ts2)
        self.assertIsNone(_parse_mail_ts("bad"))


class TestStoreAuthProfile(unittest.TestCase):
    def test_pending_account_and_auth_profile_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(base_dir=tmp)
            cfg.ensure_dirs()
            st = Store(cfg)
            prof = {
                "auth_type": "email_otp",
                "identifier": "user@mangoqwq.com",
                "password_enc": "",
                "totp_secret_enc": "",
                "onemail_base": "https://mail-api.mangoqwq.com",
                "onemail_jwt_enc": "v1:a:b:c",
                "auto_relogin": True,
                "needs_manual": False,
            }
            acc = st.add_pending_account(label="acc-otp", auth_profile=prof)
            aid = acc["id"]
            pub = st.public_account(acc)
            self.assertEqual(pub["auth_profile"]["auth_type"], "email_otp")
            self.assertNotIn("onemail_jwt_enc", str(pub))

            st.record_relogin_result(aid, False, error="checkpoint", needs_manual=True)
            updated = st.get_account(aid)
            self.assertTrue(updated["auth_profile"]["needs_manual"])
            self.assertGreater(updated["auth_profile"]["last_relogin_at"], int(time.time()) - 10)

            st.record_relogin_result(aid, True)
            updated2 = st.get_account(aid)
            self.assertFalse(updated2["auth_profile"]["needs_manual"])
            self.assertTrue(updated2["auth_profile"]["last_relogin_ok"])

            st.clear_auth_profile(aid)
            self.assertIsNone(st.get_account(aid).get("auth_profile"))

    def test_find_by_hatch_sess_and_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(base_dir=tmp)
            cfg.ensure_dirs()
            st = Store(cfg)
            a1 = st.add_account({"hatch_sess": "sess_abc"}, label="a1")
            st.set_auth_profile(a1["id"], {"identifier": "User@Example.com", "auth_type": "password"})
            a2 = st.add_pending_account(label="a2")
            self.assertEqual(st.find_by_hatch_sess("sess_abc", exclude_aid=a2["id"])["id"], a1["id"])
            self.assertIsNone(st.find_by_hatch_sess("sess_abc", exclude_aid=a1["id"]))
            self.assertEqual(st.find_by_identifier("user@example.com")["id"], a1["id"])
            self.assertIsNone(st.find_by_identifier("other@example.com"))


if __name__ == "__main__":
    unittest.main()
