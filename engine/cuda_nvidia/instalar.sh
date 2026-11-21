#!/usr/bin/env bash
# Whisper local com GPU NVIDIA (CUDA) via faster-whisper/CTranslate2 — instalação em máquina nova.
# Idempotente, sem sudo. Só o driver NVIDIA é pré-requisito (cuBLAS/cuDNN/NVRTC vêm no venv,
# versões fixas em requirements_whisper.txt).
# Etapas: venv + modelos faster-whisper (large-v3-turbo int8 + small) + unidades systemd --user
# (whisper-ditado, ditado-stream).
# Uso: bash instalar.sh
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"
command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 || { echo "sem GPU NVIDIA visível (nvidia-smi): use vulkan_amd/ ou o perfil cpu"; exit 1; }
bash "$AQUI/../install-engine.sh" --perfil cuda
systemctl --user is-active whisper-ditado.service ditado-stream.service
echo "Teste: python3 $(cd "$AQUI/../.." && pwd)/ditado/ditar.py --arquivo <wav 16 kHz mono> --sem-digitar"
