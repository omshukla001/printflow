#!/bin/sh
# Initialize Flask-Migrate against the *current* (post-upgrade) schema, so future
# schema changes are tracked with Alembic. The compat layer handles the existing
# delta; this script just stamps the DB so Alembic agrees it's at HEAD.
set -e
cd "$(dirname "$0")/.."

export FLASK_APP=run.py
export FLASK_SKIP_SCHEDULER=1

if [ ! -d migrations ]; then
    venv/bin/flask db init
fi
venv/bin/flask db migrate -m "Baseline after security/reliability pass" || true
venv/bin/flask db stamp head
echo "Flask-Migrate ready. Use 'flask db migrate -m \"msg\"' + 'flask db upgrade' going forward."
