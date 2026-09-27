#!/bin/bash

echo "[MT0D12] Starting receiver"

cd ~/mt0d12 || exit 1
source .venv-3.12/bin/activate

python -m lib.gamepad.udp_receiver
