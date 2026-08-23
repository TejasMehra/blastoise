"""The GitHub surface: changed files, the PR comment, the check status.

A small hand-written client rather than a dependency. Three endpoints are
needed and the alternative is pulling a REST library into a tool whose whole
pitch is that it installs in one line, so this is `urllib` plus a
:class:`Transport` seam that tests substitute -- which is also what makes
"updates its comment instead of posting a new one" a testable claim rather
than a hopeful one.

Failures here are reported, never fatal. A pull request from a fork gets a
read-only ``GITHUB_TOKEN``: the comment POST returns 403, and the right
response is to say so and leave the verdict in the job summary and the exit
code, not to fail a run whose actual work succeeded.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from blastoise.report import FileVerdict

__all__ = [
    "CHECK_RUN_NAME",
    "CONCLUSION_OF_VERDICT",
    "GitHubClient",
    "GitHubContext",
    "GitHubError",
    "Response",
    "Transport",
    "urllib_transport",
]

CHECK_RUN_NAME = "blastoise"

CONCLUSION_OF_VERDICT: dict[FileVerdict, str] = {
    FileVerdict.PROCEED: "success",
    FileVerdict.REQUIRES_APPROVAL: "neutral",
    FileVerdict.BLOCK: "failure",
}
"""PROCEED passes, REQUIRES_APPROVAL is neutral, BLOCK fails.

``neutral`` is the reason this goes through the Checks API at all: a job's
exit code can only pass or fail, and "a human has to look at this" is
neither. It needs ``checks: write``."""

_CHECK_OUTPUT_LIMIT = 65535
_USER_AGENT = "blastoise-ci"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


class GitHubError(Exception):
    """An API call failed. Carries the status so callers can be lenient."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_permission(self) -> bool:
        """403/404 on a write: almost always a fork's read-only token."""
        return self.status in (401, 403, 404)


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


Transport = Callable[[str, str, bytes | None, dict[str, str]], Response]
"""``(method, url, body, headers) -> Response``. The seam tests replace."""


def urllib_transport(
    method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as handle:
            return Response(status=handle.status, body=handle.read())
    except urllib.error.HTTPError as exc:
        return Response(status=exc.code, body=exc.read())
    except urllib.error.URLError as exc:
        raise GitHubError(f"could not reach the GitHub API: {exc.reason}") from exc


@dataclass(frozen=True, slots=True)
class GitHubContext:
    """What the workflow environment says about the run.

    Built from environment variables only. Nothing here is ever a
    connection string, and there is deliberately no field that could hold
    one -- see the runner's note on why the database URL is not an input.
    """

    repository: str | None = None
    api_url: str = "https://api.github.com"
    pull_number: int | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    event_name: str | None = None

    @property
    def is_pull_request(self) -> bool:
        return self.pull_number is not None and self.repository is not None

    @classmethod
    def from_environment(
        cls, environ: dict[str, str], *, event: dict[str, Any] | None = None
    ) -> GitHubContext:
        pull_request = (event or {}).get("pull_request") or {}
        number = pull_request.get("number") or (event or {}).get("number")
        head = pull_request.get("head") or {}
        base = pull_request.get("base") or {}
        return cls(
            repository=environ.get("GITHUB_REPOSITORY") or None,
            api_url=environ.get("GITHUB_API_URL") or "https://api.github.com",
            pull_number=int(number) if isinstance(number, int | str) and str(number).isdigit()
            else None,
            # On a pull_request event GITHUB_SHA is the ephemeral merge
            # commit; a check run has to attach to the head commit or it
            # shows up on nothing the pull request displays.
            head_sha=head.get("sha") or environ.get("GITHUB_SHA") or None,
            base_sha=base.get("sha") or None,
            event_name=environ.get("GITHUB_EVENT_NAME") or None,
        )


@dataclass
class GitHubClient:
    token: str
    repository: str
    api_url: str = "https://api.github.com"
    transport: Transport = urllib_transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, payload: Any = None) -> Any:
        url = f"{self.api_url.rstrip('/')}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        response = self.transport(method, url, body, self._headers())
        if response.status >= 400:
            detail = ""
            try:
                parsed = response.json()
            except (ValueError, UnicodeDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                detail = str(parsed.get("message", ""))
            raise GitHubError(
                f"{method} {path} failed with HTTP {response.status}"
                + (f": {detail}" if detail else ""),
                status=response.status,
            )
        return response.json()

    # -- changed files ---------------------------------------------------

    def pull_request_files(self, number: int, *, max_pages: int = 30) -> tuple[str, ...]:
        """Every path the pull request touches, deleted files excluded.

        Uses the API rather than ``git diff`` so that the default
        ``fetch-depth: 1`` checkout works: the endpoint computes against the
        merge base server-side, which a shallow clone cannot do locally.

        The endpoint pages at 100 and stops at 3000 files; a pull request
        that large has its own problems, and hitting the page cap is
        reported by the caller rather than passed off as a complete list.
        """
        paths: list[str] = []
        for page in range(1, max_pages + 1):
            batch = self._call(
                "GET",
                f"/repos/{self.repository}/pulls/{number}/files"
                f"?per_page=100&page={page}",
            )
            if not isinstance(batch, list) or not batch:
                break
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") == "removed":
                    continue
                filename = entry.get("filename")
                if isinstance(filename, str):
                    paths.append(filename)
                # A rename reports the new path as filename; the old one is
                # not a migration that will run, so it is not collected.
            if len(batch) < 100:
                break
        return tuple(paths)

    # -- comments --------------------------------------------------------

    def find_comment(self, number: int, marker: str, *, max_pages: int = 20) -> int | None:
        """The id of our previous comment on this pull request, if any."""
        for page in range(1, max_pages + 1):
            batch = self._call(
                "GET",
                f"/repos/{self.repository}/issues/{number}/comments"
                f"?per_page=100&page={page}",
            )
            if not isinstance(batch, list) or not batch:
                return None
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                body = entry.get("body")
                if isinstance(body, str) and marker in body:
                    identifier = entry.get("id")
                    if isinstance(identifier, int):
                        return identifier
            if len(batch) < 100:
                return None
        return None

    def delete_comment(self, number: int, marker: str) -> bool:
        """Remove our comment if there is one. Returns whether there was.

        For the case where a pull request no longer touches any migration:
        the previous verdict is not merely stale, there is nothing true to
        replace it with. An empty table left behind is noise on a pull
        request that has nothing to do with migrations, and a deleted
        comment is the honest form of "never mind".
        """
        existing = self.find_comment(number, marker)
        if existing is None:
            return False
        self._call("DELETE", f"/repos/{self.repository}/issues/comments/{existing}")
        return True

    def upsert_comment(
        self, number: int, marker: str, body: str, *, create: bool = True
    ) -> tuple[str, str | None]:
        """Edit our comment if it exists, otherwise post one.

        Returns ``(action, url)`` where action is ``"updated"``, ``"created"``
        or ``"skipped"``. Re-pushing a branch must not leave a column of stale
        verdicts behind: only the newest one is true, and the older ones
        differ from it in exactly the cases that matter most.

        ``create=False`` updates an existing comment but will not open a new
        one. That is the "this pull request has no migrations" case: there is
        nothing to say, but if a previous push *did* have migrations, the
        comment it left is now wrong and must be corrected rather than left
        standing.
        """
        existing = self.find_comment(number, marker)
        if existing is None and not create:
            return "skipped", None
        if existing is not None:
            result = self._call(
                "PATCH",
                f"/repos/{self.repository}/issues/comments/{existing}",
                {"body": body},
            )
            return "updated", _html_url(result)
        result = self._call(
            "POST",
            f"/repos/{self.repository}/issues/{number}/comments",
            {"body": body},
        )
        return "created", _html_url(result)

    # -- check runs ------------------------------------------------------

    def create_check_run(
        self,
        head_sha: str,
        *,
        conclusion: str,
        title: str,
        summary: str,
        name: str = CHECK_RUN_NAME,
    ) -> str | None:
        result = self._call(
            "POST",
            f"/repos/{self.repository}/check-runs",
            {
                "name": name,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {
                    "title": title[:255],
                    "summary": summary[:_CHECK_OUTPUT_LIMIT],
                },
            },
        )
        return _html_url(result)


def _html_url(result: Any) -> str | None:
    if isinstance(result, dict):
        url = result.get("html_url")
        if isinstance(url, str):
            return url
    return None
