"""Signing and attestation for verdict documents (the **Shell Seal**).

Ed25519 over the report's canonical serialization: the signed message is
the canonical JSON of the payload with the ``signature`` key absent, so an
unsigned report and a signed report differ only by that one key and the
signature can always be recomputed from the document itself.

Keys come from a file the operator points at — the ``--sign-key`` option
or the ``BLASTOISE_SIGNING_KEY`` environment variable (a path) — and are
**never generated silently**: a missing key means the report ships
unsigned, which is a valid, merely unattested, document. Signing is not a
prerequisite for anything.

Requires the ``cryptography`` package (``pip install pgblastoise[sign]``);
everything else in blastoise works without it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from blastoise.report.serialize import canonical_json

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SIGNING_KEY_ENV = "BLASTOISE_SIGNING_KEY"
_HEX_SEED = re.compile(r"[0-9a-fA-F]{64}")


class SigningError(ValueError):
    """A key file that cannot be used, or a malformed signature block."""


class SigningUnavailableError(RuntimeError):
    """The ``cryptography`` package is not installed."""


def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - installed in the dev env
        raise SigningUnavailableError(
            "signing requires the 'cryptography' package: "
            "pip install pgblastoise[sign]"
        ) from exc
    return ed25519


def load_signing_key(path: str | Path) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from ``path``.

    Accepts a PEM-encoded private key (``openssl genpkey -algorithm
    ed25519``) or a bare 64-hex-character seed (32 bytes). Anything else is
    a :class:`SigningError` — a key is never generated on the caller's
    behalf.
    """
    ed25519 = _ed25519()
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SigningError(f"cannot read signing key {path}: {exc}") from exc
    if b"-----BEGIN" in data:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        try:
            key = load_pem_private_key(data, password=None)
        except (ValueError, TypeError) as exc:
            raise SigningError(f"cannot parse PEM signing key {path}: {exc}") from exc
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise SigningError(
                f"signing key {path} is not an Ed25519 key ({type(key).__name__})"
            )
        pem_key: Ed25519PrivateKey = key
        return pem_key
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SigningError(
            f"signing key {path} is neither PEM nor a hex seed"
        ) from exc
    if not _HEX_SEED.fullmatch(text):
        raise SigningError(
            f"signing key {path} must be a PEM Ed25519 private key or a "
            "64-hex-character seed (32 bytes)"
        )
    result: Ed25519PrivateKey = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(text)
    )
    return result


def resolve_signing_key(
    cli_path: str | None, env: Mapping[str, str]
) -> Ed25519PrivateKey | None:
    """The key the caller asked for, or None (report ships unsigned).

    ``--sign-key`` wins over the environment; both name a file path.
    """
    path = cli_path or env.get(SIGNING_KEY_ENV) or None
    if not path:
        return None
    return load_signing_key(path)


def signed_message(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes the signature covers: everything but ``signature``."""
    body = {key: value for key, value in payload.items() if key != "signature"}
    return canonical_json(body).encode("ascii")


def sign_payload(payload: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    """A copy of ``payload`` with an Ed25519 ``signature`` block attached."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    message = signed_message(payload)
    signature = key.sign(message)
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    body = {k: v for k, v in payload.items() if k != "signature"}
    body["signature"] = {
        "algorithm": "ed25519",
        "public_key": public.hex(),
        "signature": signature.hex(),
    }
    return body


def verify_signature(payload: Mapping[str, Any]) -> tuple[bool, str]:
    """Check the report's signature against its own content.

    Returns ``(ok, detail)``. An unsigned report is ``(False, ...)`` — a
    stripped signature is indistinguishable from one that never existed, so
    ``verify`` must not treat absence as success. The public key is taken
    from the report itself; pin it externally by comparing the key this
    function reports against the one you expect.
    """
    ed25519 = _ed25519()
    from cryptography.exceptions import InvalidSignature

    block = payload.get("signature")
    if block is None:
        return False, "report is unsigned: there is no attestation to verify"
    if not isinstance(block, dict):
        return False, "signature block is malformed (not an object)"
    if block.get("algorithm") != "ed25519":
        return False, f"unsupported signature algorithm {block.get('algorithm')!r}"
    try:
        public_bytes = bytes.fromhex(str(block.get("public_key", "")))
        signature = bytes.fromhex(str(block.get("signature", "")))
        public = ed25519.Ed25519PublicKey.from_public_bytes(public_bytes)
    except ValueError:
        return False, "signature block is malformed (bad hex or key length)"
    try:
        public.verify(signature, signed_message(payload))
    except InvalidSignature:
        return False, "signature does not match the report content"
    return True, f"ed25519 signature valid; public key {public_bytes.hex()}"
