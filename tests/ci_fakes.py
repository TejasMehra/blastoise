"""A fake GitHub API, so the CI integration's API behaviour is testable.

Small on purpose: it records every call and answers the four endpoints the
integration uses. The point is not to simulate GitHub, it is to make claims
like "re-pushing edits the comment instead of adding one" assertions rather
than hopes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from blastoise.ci.github import Response


@dataclass
class Call:
    method: str
    url: str
    body: dict[str, Any] | None
    headers: dict[str, str]

    @property
    def path(self) -> str:
        return self.url.split("api.github.com", 1)[-1].split("?", 1)[0]


@dataclass
class FakeGitHub:
    """Comments, check runs and a pull request file list, in memory."""

    files: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    check_runs: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    status_for: dict[str, int] = field(default_factory=dict)
    """Force a status code for any path fragment, e.g. {"issues": 403}."""

    _next_id: int = 1000

    def add_comment(self, body: str, *, author: str = "someone") -> int:
        self._next_id += 1
        self.comments.append(
            {
                "id": self._next_id,
                "body": body,
                "user": {"login": author},
                "html_url": f"https://github.com/o/r/pull/7#issuecomment-{self._next_id}",
            }
        )
        return self._next_id

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> Response:
        payload = None if body is None else json.loads(body.decode("utf-8"))
        call = Call(method=method, url=url, body=payload, headers=headers)
        self.calls.append(call)

        for fragment, status in self.status_for.items():
            if fragment in url:
                return Response(status, json.dumps({"message": "forced"}).encode())

        if "/pulls/" in url and url.split("?")[0].endswith("/files"):
            return self._paged(url, self.files)
        if method == "GET" and "/issues/" in url and "/comments" in url:
            return self._paged(url, self.comments)
        if method == "POST" and "/issues/" in url and "/comments" in url:
            assert payload is not None
            identifier = self.add_comment(str(payload["body"]), author="github-actions[bot]")
            return self._ok(self.comments[-1] | {"id": identifier})
        if method == "PATCH" and "/issues/comments/" in url:
            assert payload is not None
            identifier = int(url.rstrip("/").rsplit("/", 1)[-1])
            for comment in self.comments:
                if comment["id"] == identifier:
                    comment["body"] = str(payload["body"])
                    return self._ok(comment)
            return Response(404, b'{"message": "Not Found"}')
        if method == "POST" and url.endswith("/check-runs"):
            assert payload is not None
            self.check_runs.append(payload)
            return self._ok({"html_url": "https://github.com/o/r/runs/1"})
        return Response(404, b'{"message": "unhandled in the fake"}')

    @staticmethod
    def _ok(payload: dict[str, Any]) -> Response:
        return Response(200, json.dumps(payload).encode())

    @staticmethod
    def _paged(url: str, items: list[dict[str, Any]]) -> Response:
        page = 1
        if "page=" in url:
            page = int(url.rsplit("page=", 1)[-1].split("&")[0])
        start = (page - 1) * 100
        return FakeGitHub._ok_list(items[start : start + 100])

    @staticmethod
    def _ok_list(items: list[dict[str, Any]]) -> Response:
        return Response(200, json.dumps(items).encode())


def pull_request_event(
    *, number: int = 7, head: str = "f" * 40, base: str = "a" * 40
) -> dict[str, Any]:
    return {
        "pull_request": {
            "number": number,
            "head": {"sha": head},
            "base": {"sha": base},
        }
    }


def actions_environment(tmp_path: Any, event: dict[str, Any] | None = None) -> dict[str, str]:
    """A minimal but realistic GitHub Actions environment."""
    environ = {
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_TOKEN": "ghs_faketoken0123456789",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_SHA": "m" * 40,
    }
    if event is not None:
        path = tmp_path / "event.json"
        path.write_text(json.dumps(event), encoding="utf-8")
        environ["GITHUB_EVENT_PATH"] = str(path)
    return environ
