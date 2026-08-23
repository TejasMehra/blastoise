"""The GitHub surface: comment upsert, check status, changed-file listing."""

from __future__ import annotations

from ci_fakes import FakeGitHub, pull_request_event

from blastoise.ci.github import (
    CONCLUSION_OF_VERDICT,
    GitHubClient,
    GitHubContext,
    GitHubError,
)
from blastoise.ci.markdown import COMMENT_MARKER
from blastoise.report import FileVerdict


def _client(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient(token="ghs_x", repository="acme/app", transport=fake)


class TestCommentUpsert:
    """Re-pushing must edit the comment, not stack up stale verdicts."""

    def test_first_run_creates(self) -> None:
        fake = FakeGitHub()
        action, url = _client(fake).upsert_comment(7, COMMENT_MARKER, f"{COMMENT_MARKER}\nBLOCK")
        assert action == "created"
        assert url
        assert len(fake.comments) == 1

    def test_second_run_updates_the_same_comment(self) -> None:
        fake = FakeGitHub()
        client = _client(fake)
        client.upsert_comment(7, COMMENT_MARKER, f"{COMMENT_MARKER}\nBLOCK")
        first_id = fake.comments[0]["id"]

        action, _ = client.upsert_comment(7, COMMENT_MARKER, f"{COMMENT_MARKER}\nPROCEED")
        assert action == "updated"
        assert len(fake.comments) == 1, "a re-push must not post a second comment"
        assert fake.comments[0]["id"] == first_id
        assert "PROCEED" in fake.comments[0]["body"]
        assert "BLOCK" not in fake.comments[0]["body"]
        assert [call.method for call in fake.calls if call.method == "PATCH"] == ["PATCH"]

    def test_other_peoples_comments_are_left_alone(self) -> None:
        fake = FakeGitHub()
        fake.add_comment("looks good to me", author="a-reviewer")
        fake.add_comment("<!-- some-other-bot -->\nunrelated", author="other-bot")
        _client(fake).upsert_comment(7, COMMENT_MARKER, f"{COMMENT_MARKER}\nBLOCK")
        assert len(fake.comments) == 3
        assert fake.comments[0]["body"] == "looks good to me"
        assert "unrelated" in fake.comments[1]["body"]

    def test_the_marker_is_what_identifies_ours_not_the_author(self) -> None:
        # The token that posts is the workflow's, which is not a stable
        # identity across a repository's history of tokens and apps.
        fake = FakeGitHub()
        fake.add_comment(f"{COMMENT_MARKER}\nan older run", author="someone-else")
        action, _ = _client(fake).upsert_comment(7, COMMENT_MARKER, f"{COMMENT_MARKER}\nnew")
        assert action == "updated"
        assert len(fake.comments) == 1

    def test_the_comment_is_found_on_a_later_page(self) -> None:
        fake = FakeGitHub()
        for index in range(150):
            fake.add_comment(f"chatter {index}")
        fake.add_comment(f"{COMMENT_MARKER}\nolder")
        action, _ = _client(fake).upsert_comment(7, COMMENT_MARKER, f"{COMMENT_MARKER}\nnew")
        assert action == "updated"
        assert len(fake.comments) == 151

    def test_a_read_only_token_raises_a_permission_error(self) -> None:
        fake = FakeGitHub(status_for={"/issues/": 403})
        try:
            _client(fake).upsert_comment(7, COMMENT_MARKER, "body")
        except GitHubError as exc:
            assert exc.is_permission
            assert exc.status == 403
        else:  # pragma: no cover - the fake always forces the status
            raise AssertionError("expected a GitHubError")


class TestCheckRun:
    def test_conclusion_mapping_is_the_contract(self) -> None:
        assert CONCLUSION_OF_VERDICT == {
            FileVerdict.PROCEED: "success",
            FileVerdict.REQUIRES_APPROVAL: "neutral",
            FileVerdict.BLOCK: "failure",
        }

    def test_a_check_run_carries_the_conclusion_and_head_sha(self) -> None:
        fake = FakeGitHub()
        _client(fake).create_check_run(
            "f" * 40, conclusion="neutral", title="REQUIRES APPROVAL", summary="body"
        )
        (run,) = fake.check_runs
        assert run["head_sha"] == "f" * 40
        assert run["conclusion"] == "neutral"
        assert run["status"] == "completed"
        assert run["output"]["title"] == "REQUIRES APPROVAL"

    def test_an_oversized_summary_is_truncated_not_rejected(self) -> None:
        fake = FakeGitHub()
        _client(fake).create_check_run(
            "f" * 40, conclusion="failure", title="t" * 400, summary="s" * 90_000
        )
        (run,) = fake.check_runs
        assert len(run["output"]["title"]) == 255
        assert len(run["output"]["summary"]) == 65535


class TestPullRequestFiles:
    def test_paths_are_listed_and_deletions_dropped(self) -> None:
        fake = FakeGitHub(
            files=[
                {"filename": "migrations/0001.sql", "status": "added"},
                {"filename": "migrations/0000.sql", "status": "removed"},
                {"filename": "README.md", "status": "modified"},
            ]
        )
        assert _client(fake).pull_request_files(7) == (
            "migrations/0001.sql",
            "README.md",
        )

    def test_pagination(self) -> None:
        fake = FakeGitHub(
            files=[{"filename": f"migrations/{i:04d}.sql", "status": "added"} for i in range(250)]
        )
        assert len(_client(fake).pull_request_files(7)) == 250


class TestContext:
    def test_a_check_run_attaches_to_the_head_commit_not_the_merge_commit(self) -> None:
        # On a pull_request event GITHUB_SHA is the ephemeral merge commit;
        # a check run against it shows up on nothing the pull request shows.
        context = GitHubContext.from_environment(
            {"GITHUB_REPOSITORY": "acme/app", "GITHUB_SHA": "m" * 40},
            event=pull_request_event(head="h" * 40),
        )
        assert context.head_sha == "h" * 40
        assert context.pull_number == 7
        assert context.is_pull_request

    def test_a_push_event_is_not_a_pull_request(self) -> None:
        context = GitHubContext.from_environment(
            {"GITHUB_REPOSITORY": "acme/app", "GITHUB_SHA": "m" * 40}, event={}
        )
        assert not context.is_pull_request
        assert context.head_sha == "m" * 40

    def test_a_github_enterprise_api_url_is_honoured(self) -> None:
        context = GitHubContext.from_environment(
            {"GITHUB_REPOSITORY": "acme/app", "GITHUB_API_URL": "https://ghe.acme.com/api/v3"}
        )
        assert context.api_url == "https://ghe.acme.com/api/v3"


class TestRequestShape:
    def test_every_call_is_authenticated_and_versioned(self) -> None:
        fake = FakeGitHub()
        _client(fake).upsert_comment(7, COMMENT_MARKER, "body")
        for call in fake.calls:
            assert call.headers["Authorization"] == "Bearer ghs_x"
            assert call.headers["X-GitHub-Api-Version"]
            assert call.headers["Accept"] == "application/vnd.github+json"

    def test_the_api_url_prefix_is_used(self) -> None:
        fake = FakeGitHub()
        GitHubClient(
            token="t", repository="acme/app", api_url="https://ghe.acme.com/api/v3", transport=fake
        ).create_check_run("f" * 40, conclusion="success", title="t", summary="s")
        assert fake.calls[0].url.startswith("https://ghe.acme.com/api/v3/repos/acme/app/")


class TestMalformedResponses:
    """Enterprise proxies and rate limiters do not always return what the docs say."""

    def test_a_non_json_error_body_still_produces_a_useful_error(self) -> None:
        from blastoise.ci.github import Response

        def transport(*_args: object, **_kwargs: object) -> Response:
            return Response(502, b"<html>Bad Gateway</html>")

        client = GitHubClient(token="t", repository="acme/app", transport=transport)
        try:
            client.find_comment(7, COMMENT_MARKER)
        except GitHubError as exc:
            assert exc.status == 502
            assert "502" in str(exc)
            assert not exc.is_permission
        else:  # pragma: no cover - the transport always fails
            raise AssertionError("expected a GitHubError")

    def test_a_response_that_is_not_a_list_is_treated_as_no_results(self) -> None:
        from blastoise.ci.github import Response

        def transport(*_args: object, **_kwargs: object) -> Response:
            return Response(200, b'{"message": "unexpected"}')

        client = GitHubClient(token="t", repository="acme/app", transport=transport)
        assert client.find_comment(7, COMMENT_MARKER) is None
        assert client.pull_request_files(7) == ()

    def test_a_created_comment_with_no_url_is_not_an_error(self) -> None:
        from blastoise.ci.github import Response

        calls: list[str] = []

        def transport(method: str, *_args: object, **_kwargs: object) -> Response:
            calls.append(method)
            return Response(200, b"[]" if method == "GET" else b"{}")

        client = GitHubClient(token="t", repository="acme/app", transport=transport)
        action, url = client.upsert_comment(7, COMMENT_MARKER, "body")
        assert action == "created"
        assert url is None

    def test_an_error_with_no_status_is_not_read_as_a_permission_problem(self) -> None:
        assert not GitHubError("network died").is_permission


class TestNoMigrationsComment:
    """A check that comments on every pull request is a check people mute."""

    def test_create_false_does_not_open_a_new_comment(self) -> None:
        fake = FakeGitHub()
        action, url = _client(fake).upsert_comment(
            7, COMMENT_MARKER, "nothing to say", create=False
        )
        assert action == "skipped"
        assert url is None
        assert fake.comments == []
        assert not any(call.method == "POST" for call in fake.calls)

    def test_create_false_still_corrects_an_existing_comment(self) -> None:
        # The push that removed the migration must not leave the previous
        # push's verdict standing.
        fake = FakeGitHub()
        fake.add_comment(f"{COMMENT_MARKER}\nBLOCK")
        action, _ = _client(fake).upsert_comment(
            7, COMMENT_MARKER, f"{COMMENT_MARKER}\nnothing to assess", create=False
        )
        assert action == "updated"
        assert len(fake.comments) == 1
        assert "BLOCK" not in fake.comments[0]["body"]
