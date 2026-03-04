#!/bin/sh
# Run database migrations before starting the service
python -m turnstone.core.storage._migrate 2>/dev/null || true
# Execute the actual command
exec "$@"
