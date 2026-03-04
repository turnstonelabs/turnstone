#!/bin/sh
# Run database migrations before starting the service
python -m turnstone.core.storage._migrate || true
# Execute the actual command
exec "$@"
