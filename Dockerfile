# Blastoise, packaged so a pipeline that is not GitHub Actions runs exactly
# the same check. The GitHub Action and this image call the same
# `blastoise ci`; there is no second implementation to drift.
#
#   docker run --rm \
#     -v "$PWD:/repo" -w /repo \
#     -e BLASTOISE_DATABASE_URL \
#     ghcr.io/tejasmehra/blastoise:0.1.0 \
#     ci --changed-source git --base-ref "$CI_MERGE_REQUEST_DIFF_BASE_SHA" \
#        --no-comment --no-check-run \
#        --comment-output report.md --json-output report.json
#
# The connection string is passed as an environment variable, never as an
# argument: an argument is visible in `ps` and in the job's command log.
FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /wheels


FROM python:3.12-slim

LABEL org.opencontainers.image.title="blastoise" \
      org.opencontainers.image.description="Know the blast radius before you migrate." \
      org.opencontainers.image.source="https://github.com/TejasMehra/blastoise" \
      org.opencontainers.image.licenses="MIT"

# git is here for `--changed-source git`, which is how a pipeline without a
# pull request API finds what changed. Nothing else is added: the image has
# no shell tooling a migration check needs, and a smaller image is a smaller
# thing to trust with a database credential.
RUN apt-get update \
 && apt-get install --no-install-recommends -y git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels/*.whl /tmp/
# [live] for the read-only introspection driver, [sign] so a pipeline can
# seal the report it produces.
RUN set -eux; \
    wheel="$(ls /tmp/*.whl)"; \
    python -m pip install --no-cache-dir "${wheel}[live,sign]"; \
    rm -f /tmp/*.whl

# `git diff` refuses to operate on a repository owned by another user, which
# is exactly what a bind-mounted checkout looks like from inside a container.
# --system, not --global: a caller passing `docker run --user` to match its
# own uid would never see a setting written into one user's home.
RUN git config --system --add safe.directory '*'

# Runs as a non-root user: the check reads SQL files and opens a read-only
# database connection, and needs nothing else. Pass
# `--user "$(id -u):$(id -g)"` when the reports must land in a bind mount
# you own.
RUN useradd --create-home --uid 1001 blastoise
USER blastoise
WORKDIR /repo

ENTRYPOINT ["blastoise"]
CMD ["--help"]
