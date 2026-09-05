#!/bin/bash
set -e

# Run database migrations automatically before starting the service
if [ "$RUN_MIGRATIONS" = "true" ]; then
    echo "Running makemigrations & migrate..."
    python manage.py makemigrations --noinput
    python manage.py migrate --noinput
fi

exec "$@"
