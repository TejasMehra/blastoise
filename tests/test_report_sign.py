"""The Shell Seal: Ed25519 signing, verification, and key loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from blastoise.report import (
    SIGNING_KEY_ENV,
    SigningError,
    load_signing_key,
    resolve_signing_key,
    sign_payload,
    signed_message,
    verify_signature,
)

SEED = bytes(range(32))


def _payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "verdict": "proceed",
        "statements": [{"index": 0, "classification": "safe"}],
        "unverified": [{"source": "no_snapshot", "reason": "offline"}],
    }


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(SEED)


class TestSignAndVerify:
    def test_signed_report_verifies(self) -> None:
        signed = sign_payload(_payload(), _key())
        ok, detail = verify_signature(signed)
        assert ok, detail
        assert signed["signature"]["algorithm"] == "ed25519"

    def test_tampered_report_fails(self) -> None:
        signed = sign_payload(_payload(), _key())
        signed["verdict"] = "block"
        ok, detail = verify_signature(signed)
        assert not ok
        assert "does not match" in detail

    def test_tampered_nested_field_fails(self) -> None:
        signed = sign_payload(_payload(), _key())
        signed["statements"][0]["classification"] = "unsafe"
        ok, _ = verify_signature(signed)
        assert not ok

    def test_unsigned_report_fails_verification(self) -> None:
        ok, detail = verify_signature(_payload())
        assert not ok
        assert "unsigned" in detail

    def test_stripped_signature_fails_verification(self) -> None:
        signed = sign_payload(_payload(), _key())
        del signed["signature"]
        ok, detail = verify_signature(signed)
        assert not ok
        assert "unsigned" in detail

    def test_swapped_key_fails(self) -> None:
        signed = sign_payload(_payload(), _key())
        other = Ed25519PrivateKey.from_private_bytes(bytes(reversed(SEED)))
        from cryptography.hazmat.primitives.serialization import PublicFormat

        signed["signature"]["public_key"] = (
            other.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        )
        ok, _ = verify_signature(signed)
        assert not ok

    def test_malformed_blocks_fail_without_raising(self) -> None:
        signed = sign_payload(_payload(), _key())
        for breakage in (
            {"algorithm": "rsa"},
            {"algorithm": "ed25519", "public_key": "zz", "signature": "zz"},
            {"algorithm": "ed25519", "public_key": "ab", "signature": "cd"},
            "not-an-object",
        ):
            broken = dict(signed)
            broken["signature"] = breakage
            ok, _ = verify_signature(broken)
            assert not ok

    def test_signature_covers_canonical_payload_without_signature_key(self) -> None:
        payload = _payload()
        signed = sign_payload(payload, _key())
        assert signed_message(signed) == signed_message(payload)

    def test_resigning_is_idempotent_on_content(self) -> None:
        # Signing an already-signed payload replaces the seal; it never
        # signs the previous signature into the message.
        once = sign_payload(_payload(), _key())
        twice = sign_payload(once, _key())
        assert once["signature"] == twice["signature"]


class TestKeyLoading:
    def test_hex_seed_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.hex"
        key_file.write_text(SEED.hex() + "\n", encoding="ascii")
        key = load_signing_key(key_file)
        signed = sign_payload(_payload(), key)
        assert verify_signature(signed)[0]

    def test_pem_file(self, tmp_path: Path) -> None:
        pem = _key().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        key_file = tmp_path / "key.pem"
        key_file.write_bytes(pem)
        key = load_signing_key(key_file)
        signed = sign_payload(_payload(), key)
        assert verify_signature(signed)[0]

    def test_garbage_file_is_rejected(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text("not a key at all", encoding="ascii")
        with pytest.raises(SigningError, match="64-hex-character seed"):
            load_signing_key(key_file)

    def test_non_ed25519_pem_is_rejected(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives.asymmetric.ec import (
            SECP256R1,
            generate_private_key,
        )

        pem = generate_private_key(SECP256R1()).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
        key_file = tmp_path / "ec.pem"
        key_file.write_bytes(pem)
        with pytest.raises(SigningError, match="not an Ed25519 key"):
            load_signing_key(key_file)

    def test_missing_file_is_a_signing_error(self, tmp_path: Path) -> None:
        with pytest.raises(SigningError, match="cannot read"):
            load_signing_key(tmp_path / "nope.hex")

    def test_resolve_prefers_cli_over_environment(self, tmp_path: Path) -> None:
        cli_file = tmp_path / "cli.hex"
        cli_file.write_text(SEED.hex(), encoding="ascii")
        env_file = tmp_path / "env.hex"
        env_file.write_text(bytes(reversed(SEED)).hex(), encoding="ascii")
        env = {SIGNING_KEY_ENV: str(env_file)}

        from cryptography.hazmat.primitives.serialization import PublicFormat

        chosen = resolve_signing_key(str(cli_file), env)
        assert chosen is not None
        assert chosen.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) == _key(
        ).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        from_env = resolve_signing_key(None, env)
        assert from_env is not None

    def test_resolve_without_any_key_is_none_and_never_generates(self) -> None:
        assert resolve_signing_key(None, {}) is None
