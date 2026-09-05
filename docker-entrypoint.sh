#!/bin/bash
set -e

if [ "$RUN_MIGRATIONS" = "true" ]; then
    echo "Running makemigrations and migrate..."
    python manage.py makemigrations --noinput || true
    python manage.py migrate --noinput
fi

exec "$@"
