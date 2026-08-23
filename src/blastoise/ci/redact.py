"""Redaction of connection details from every output path.

The CI integration is the one place Blastoise runs with a real database
credential in its environment, and it is also the place whose output is
public: a PR comment, a job log, a workflow artifact. Everything printed by
``blastoise ci`` -- including exception messages and tracebacks -- goes
through a :class:`Redactor` first.

Two mechanisms, deliberately both:

* **Literal secrets.** The connection string the run was given, plus each
  distinctive component parsed out of it (password, host, user, dbname), are
  registered as literals and replaced wherever they appear. This catches
  what no pattern can: a driver that formats the host into a message in a
  shape we never anticipated.
* **Patterns.** Connection-string-shaped text (URI form, ``password=`` in
  keyword form, libpq's ``connection to server at "host" (addr)``) is
  replaced even when the value was never registered -- because the string
  that leaks may not be the one we were configured with.

The parsing here is stdlib-only on purpose. ``blastoise.live`` already has
:func:`~blastoise.live.redact_conninfo`, but it needs ``psycopg``, and the
redactor has to work in the offline install where the driver is absent --
the run that never connects still has the credential in its environment.

Over-redaction is the accepted failure mode. Component literals are held to
a minimum length and a stoplist of generic values (``localhost``,
``postgres``, ``app``) so that ordinary prose is not shredded, but where the
two directions conflict the secret wins.
"""

from __future__ import annotations

import re
import traceback
from urllib.parse import unquote, urlsplit

PLACEHOLDER = "<redacted>"

_MIN_LITERAL = 4
"""Shortest component value registered as a literal. Below this the value
carries little entropy and matches far too much ordinary text; passwords are
exempt (see :meth:`Redactor.add_connection_string`)."""

_MIN_PASSWORD = 3

# Values common enough that redacting every occurrence would mangle prose
# without hiding anything an attacker does not already assume.
_GENERIC = frozenset(
    {
        "postgres",
        "postgresql",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "app",
        "main",
        "test",
        "database",
        "db",
        "public",
        "user",
        "admin",
        "root",
        "none",
        "null",
    }
)

_URI_SCHEME_RE = re.compile(r"(?i)^(?:postgres|postgresql|pgsql)(?:\+[A-Za-z0-9_]+)?://")

# A URI in any of the shapes libpq and the SQLAlchemy-style drivers accept.
# The terminator set stops the match at whitespace, quotes and markdown
# punctuation so a URI embedded in prose does not swallow the sentence.
_URI_RE = re.compile(
    r"(?i)\b(?:postgres|postgresql|pgsql)(?:\+[A-Za-z0-9_]+)?://[^\s'\"`<>)\]}]*"
)

# Keyword/value form: only the password key. Redacting ``user=`` or ``host=``
# by pattern would rewrite ordinary SQL (WHERE user = 'x'); those components
# are covered as literals instead.
_PASSWORD_RE = re.compile(
    r"(?i)\b(password|pgpassword)\s*=\s*(?:'(?:[^'\\]|\\.)*'|\"[^\"]*\"|[^\s;,'\"]+)"
)

# libpq's connection failure text: connection to server at "db.internal"
# (10.0.0.4), port 5432 failed: ... The host is usually registered as a
# literal; the resolved address never is.
_SERVER_AT_RE = re.compile(
    r"(?i)(connection to server (?:at|on socket) )(?:\"[^\"]*\"|'[^']*')(\s*\([^)]*\))?"
)


def parse_conninfo(conninfo: str) -> dict[str, str]:
    """Best-effort split of a connection string into libpq parameters.

    Handles both the URI form and the keyword/value form, and never raises:
    a string this cannot parse still gets registered as a literal in full,
    which is the part that matters.
    """
    text = conninfo.strip()
    if not text:
        return {}
    if _URI_SCHEME_RE.match(text):
        return _parse_uri(text)
    return _parse_keywords(text)


def _parse_uri(text: str) -> dict[str, str]:
    try:
        parts = urlsplit(text)
    except ValueError:
        return {}
    params: dict[str, str] = {}
    if parts.username:
        params["user"] = unquote(parts.username)
    if parts.password:
        params["password"] = unquote(parts.password)
    # A multi-host URI (host1:5432,host2:5432) has no single hostname, and
    # urlsplit raises on the second colon; split the authority ourselves.
    authority = parts.netloc.rpartition("@")[2]
    hosts: list[str] = []
    for chunk in authority.split(","):
        host, separator, port = chunk.rpartition(":")
        if not separator:
            host, port = chunk, ""
        host = host.strip("[]")
        if host:
            hosts.append(host)
        if port and "port" not in params:
            params["port"] = port
    for offset, value in enumerate(hosts):
        params["host" if offset == 0 else f"host{offset}"] = value
    dbname = parts.path.lstrip("/")
    if dbname:
        params["dbname"] = unquote(dbname)
    for pair in parts.query.split("&"):
        key, separator, value = pair.partition("=")
        if separator and key:
            params.setdefault(unquote(key), unquote(value))
    return params


def _parse_keywords(text: str) -> dict[str, str]:
    """Tokenize ``k=v k='v v'`` pairs, honouring libpq's backslash escapes."""
    params: dict[str, str] = {}
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        start = index
        while index < length and not text[index].isspace() and text[index] != "=":
            index += 1
        key = text[start:index]
        while index < length and text[index].isspace():
            index += 1
        if index >= length or text[index] != "=":
            continue
        index += 1
        while index < length and text[index].isspace():
            index += 1
        value_chars: list[str] = []
        if index < length and text[index] == "'":
            index += 1
            while index < length and text[index] != "'":
                if text[index] == "\\" and index + 1 < length:
                    index += 1
                value_chars.append(text[index])
                index += 1
            index += 1
        else:
            while index < length and not text[index].isspace():
                if text[index] == "\\" and index + 1 < length:
                    index += 1
                value_chars.append(text[index])
                index += 1
        if key:
            params[key] = "".join(value_chars)
    return params


class Redactor:
    """Replaces registered secrets and connection-shaped text with a placeholder.

    Instances are cheap and additive: register everything sensitive as it
    becomes known, then run every outgoing string through :meth:`scrub`.
    """

    def __init__(self) -> None:
        self._literals: list[str] = []

    def _register(self, value: str) -> None:
        if value not in self._literals:
            self._literals.append(value)
            # Longest first, so the full connection string is replaced as a
            # unit rather than perforated component by component.
            self._literals.sort(key=len, reverse=True)

    def add_secret(self, value: str | None, *, minimum: int = _MIN_LITERAL) -> None:
        """Register one literal to replace wherever it appears."""
        if value is None:
            return
        text = value.strip()
        if len(text) < minimum or text.lower() in _GENERIC:
            return
        self._register(text)

    def add_connection_string(self, conninfo: str | None) -> None:
        """Register a connection string and its distinctive components."""
        if conninfo is None or not conninfo.strip():
            return
        self.add_secret(conninfo, minimum=1)
        params = parse_conninfo(conninfo)
        password = params.pop("password", None)
        # A password is registered however short and however generic: the
        # cost of mangling the word "app" in a comment is not comparable to
        # the cost of printing a credential.
        if password is not None and len(password) >= _MIN_PASSWORD:
            self._register(password)
        for key, value in params.items():
            if key.startswith(("host", "user", "dbname")):
                self.add_secret(value)

    def add_environment(self, environ: dict[str, str], *names: str) -> None:
        """Register the values of the named environment variables, if set."""
        for name in names:
            value = environ.get(name)
            if not value:
                continue
            if name.upper().endswith(("URL", "DSN", "URI", "CONNINFO")):
                self.add_connection_string(value)
            else:
                self.add_secret(value, minimum=_MIN_PASSWORD)

    @property
    def secret_count(self) -> int:
        return len(self._literals)

    def scrub(self, text: str) -> str:
        """Return ``text`` with every known and every recognizable secret gone."""
        result = text
        for literal in self._literals:
            if literal in result:
                result = result.replace(literal, PLACEHOLDER)
        result = _URI_RE.sub(PLACEHOLDER, result)
        result = _PASSWORD_RE.sub(lambda match: f"{match.group(1)}={PLACEHOLDER}", result)
        return _SERVER_AT_RE.sub(lambda match: f"{match.group(1)}{PLACEHOLDER}", result)

    def scrub_exception(self, exc: BaseException) -> str:
        """A one-line ``Type: message`` for an exception, scrubbed."""
        message = str(exc).strip() or exc.__class__.__name__
        return self.scrub(f"{exc.__class__.__name__}: {message}")

    def scrub_traceback(self, exc: BaseException) -> str:
        """The full formatted traceback, scrubbed.

        Tracebacks are the output path that leaks by accident -- a frame in
        the driver whose source line is the connect call, a chained cause
        carrying the DSN. They are only ever printed through this.
        """
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return self.scrub(formatted)
