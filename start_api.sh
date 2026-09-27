#!/bin/bash

echo "[MT0D12] Starting vehicle status API"

cd ~/mt0d12 || exit 1
source .venv-3.12/bin/activate

uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
