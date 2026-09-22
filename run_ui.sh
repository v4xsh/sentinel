#!/usr/bin/env bash
# Sentinel UI launcher.
# One command to bring up the FastAPI backend + static frontend at :8000.
set -e
cd "$(dirname "$0")"
exec uvicorn ui.backend.main:app --host 0.0.0.0 --port 8000 --reload
