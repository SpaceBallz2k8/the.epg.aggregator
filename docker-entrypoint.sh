#!/bin/bash
set -e

echo "⏳ Waiting for PostgreSQL to be ready..."
until python -c "
import psycopg2, os, sys
try:
    psycopg2.connect(os.environ.get('DATABASE_URL', ''))
    sys.exit(0)
except Exception as e:
    sys.exit(1)
" 2>/dev/null; do
    echo "   PostgreSQL not ready yet, retrying in 2s..."
    sleep 2
done
echo "✅ PostgreSQL is ready."

echo "🚀 Starting EPG app..."
exec python app.py
