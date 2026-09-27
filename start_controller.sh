#!/bin/bash

echo "[MT0D12] Starting Xbox controller"

cd ~/mt0d12 || exit 1
source .venv-3.12/bin/activate

python -m apps.controller.gamepad_main
