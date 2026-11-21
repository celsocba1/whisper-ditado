#!/usr/bin/env bash
# Whisper local SEM GPU utilizável (CPU, faster-whisper int8) — reserva para máquina sem NVIDIA e sem Vulkan real.
# Idempotente, sem sudo. Mais lento que as variantes com GPU: o turbo int8 leva ~0,2 s por segundo de áudio em CPU
# (ditado de 10 s ≈ 2 s + carga; CPU fraca: mais). Se ficar lento, troque o modelo final por um menor
# (ex.: small) em install-engine.sh (MODELOS) e no daemon.
# Uso: bash instalar.sh
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"
bash "$AQUI/../install-engine.sh" --perfil cpu
systemctl --user is-active whisper-ditado.service ditado-stream.service
echo "Teste: python3 $(cd "$AQUI/../.." && pwd)/ditado/ditar.py --arquivo <wav 16 kHz mono> --sem-digitar"
