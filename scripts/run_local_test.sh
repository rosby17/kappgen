#!/bin/bash
set -e

echo "=========================================================="
echo "          Nichecut - Local Test & Run Script              "
echo "=========================================================="

# Activate python virtual environment
if [ -d ".venv" ]; then
    source .venv/bin/activate
else
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
fi

# 1. Run Phase 0 isolated CLI rendering test
echo "[1/3] Running Phase 0 isolated video rendering pipeline test..."
python3 src/main.py --test

# 2. Build Frontend UI
if [ -d "frontend" ]; then
    echo "[2/3] Building frontend UI bundle..."
    cd frontend && npm run build && cd ..
fi

# 3. Start FastAPI Server and Worker
echo "[3/3] Launching FastAPI Web Application on http://localhost:8000 ..."
echo "Starting uvicorn server in background..."
python3 -m uvicorn src.api.app:app --host 0.0.0.0 --port 8000
