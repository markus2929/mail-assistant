#!/bin/sh
# One-time: generate migration 0001 from the models against an EMPTY database, then add 0002
# (audit_log append-only triggers). Review both files and commit them.
# Start the database first (local: docker compose --profile local-db up -d db).
set -eu
docker compose run --rm --no-deps -v "$PWD/migrations:/app/migrations" migrate \
  alembic revision --autogenerate --rev-id 0001 -m baseline
cp migrations/pending/*.py migrations/versions/
echo "Created migrations/versions/0001_baseline.py plus the files from migrations/pending."
echo "Review them, then: docker compose --profile local-db up -d"
