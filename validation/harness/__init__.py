"""The validation harness: does Blastoise's tier match what actually happens?

Runs every corpus case against a real Postgres, measures what the statement
did (locks on pre-existing relations, hold duration including lock waits,
rewrite, error, rows touched, stalls seen by concurrent traffic), derives a
ground-truth tier from those measurements with a rule that is written down
in :mod:`validation.harness.labeling` and does not consult the engine, and
scores the engine's prediction against it — precision and recall per tier,
errors broken down by statement classification and by the duration
constant that produced them.

Nothing in ``src/`` imports from here. The harness is a consumer of the
public engine API exactly as the CLI is.
"""
