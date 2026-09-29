#!/bin/bash

echo "[MT0D12] Starting the rover"

cd ~/mt0d12 || exit 1
source venv/bin/activate

python -m apps.rover.main
