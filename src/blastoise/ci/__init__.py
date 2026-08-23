"""The CI integration: detect a pull request's migrations, assess, report back.

What makes a check something a team uses rather than something they
remember to run is that it happens without being asked. This package is
that: migration detection from the layouts the frameworks impose, a Shell
Report per changed migration, a pull request comment that leads with the
verdict and never hides what was not verified, and a check status that can
be *neutral* -- because "a human has to look at this" is not pass and is not
fail.

Nothing here is GitHub-only except :mod:`blastoise.ci.github`. The same
``blastoise ci`` invocation writes the comment body to a file and the
machine summary to stdout, which is what the Docker image gives a GitLab or
Buildkite pipeline.

Two rules the rest of the package enforces rather than documents: the
connection string is read from an environment variable named by config and
never from a workflow input (:func:`blastoise.ci.runner.run_ci`), and every
byte written to any output path -- log, comment, summary, traceback -- goes
through a :class:`~blastoise.ci.redact.Redactor` first.
"""

from blastoise.ci.config import (
    CONFIG_FILENAME,
    DEFAULT_DATABASE_URL_ENV,
    CiConfig,
    ConfigError,
    FailOn,
    load_config,
    parse_config,
)
from blastoise.ci.detect import (
    DetectedFile,
    Framework,
    SourceKind,
    detect_migrations,
    framework_of,
    glob_to_regex,
    normalize_path,
)
from blastoise.ci.github import (
    CHECK_RUN_NAME,
    CONCLUSION_OF_VERDICT,
    GitHubClient,
    GitHubContext,
    GitHubError,
)
from blastoise.ci.markdown import COMMENT_MARKER, render_comment, render_summary_line
from blastoise.ci.model import CiRun, FileOutcome, OutcomeStatus, run_verdict
from blastoise.ci.redact import PLACEHOLDER, Redactor, parse_conninfo
from blastoise.ci.runner import ChangedSource, CiOptions, CiRunError, run_ci

__all__ = [
    "CHECK_RUN_NAME",
    "COMMENT_MARKER",
    "CONCLUSION_OF_VERDICT",
    "CONFIG_FILENAME",
    "DEFAULT_DATABASE_URL_ENV",
    "PLACEHOLDER",
    "ChangedSource",
    "CiConfig",
    "CiOptions",
    "CiRun",
    "CiRunError",
    "ConfigError",
    "DetectedFile",
    "FailOn",
    "FileOutcome",
    "Framework",
    "GitHubClient",
    "GitHubContext",
    "GitHubError",
    "OutcomeStatus",
    "Redactor",
    "SourceKind",
    "detect_migrations",
    "framework_of",
    "glob_to_regex",
    "load_config",
    "normalize_path",
    "parse_config",
    "parse_conninfo",
    "render_comment",
    "render_summary_line",
    "run_ci",
    "run_verdict",
]
