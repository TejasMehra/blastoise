"""The verdict document layer: build, serialize, sign, verify, render.

The document a check produces is called the **Shell Report** in prose and
its signature the **Shell Seal**; both names are display-only. The payload
is versioned plain JSON (``schema_version``), canonically serialized
(sorted keys, compact, ASCII, no floats), and every claim in it references
an evidence bundle entry by name and sha256. ``unverified`` is a
first-class section and never serializes empty — the honest account of
what was not checked is the point of the artifact.
"""

from blastoise.report.build import (
    build_report,
    check_evidence,
    tool_version,
    write_bundle,
)
from blastoise.report.model import (
    BUNDLE_DIRNAME,
    EXIT_CODES,
    EXIT_TOOL_ERROR,
    REPORT_FILENAME,
    SCHEMA_VERSION,
    FileVerdict,
    exit_code,
    file_verdict,
)
from blastoise.report.render import render_report
from blastoise.report.serialize import canonical_json, jsonable, sha256_hex
from blastoise.report.sign import (
    SIGNING_KEY_ENV,
    SigningError,
    SigningUnavailableError,
    load_signing_key,
    resolve_signing_key,
    sign_payload,
    signed_message,
    verify_signature,
)

__all__ = [
    "BUNDLE_DIRNAME",
    "EXIT_CODES",
    "EXIT_TOOL_ERROR",
    "REPORT_FILENAME",
    "SCHEMA_VERSION",
    "SIGNING_KEY_ENV",
    "FileVerdict",
    "SigningError",
    "SigningUnavailableError",
    "build_report",
    "canonical_json",
    "check_evidence",
    "exit_code",
    "file_verdict",
    "jsonable",
    "load_signing_key",
    "render_report",
    "resolve_signing_key",
    "sha256_hex",
    "sign_payload",
    "signed_message",
    "tool_version",
    "verify_signature",
    "write_bundle",
]
