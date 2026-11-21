#!/usr/bin/env bash
# Escolhe a variante do Whisper local pela placa da máquina e chama o instalador dela.
#   NVIDIA (nvidia-smi)             -> engine/cuda_nvidia/instalar.sh
#   GPU Vulkan real (AMD/Intel)     -> engine/vulkan_amd/instalar.sh
#   nenhuma                         -> engine/cpu_sem_gpu/instalar.sh
# Uso: bash install.sh [--perfil cuda|vulkan|cpu]
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"
P="${2:-}"
if [[ -z "$P" ]]; then
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then P=cuda
  elif command -v vulkaninfo >/dev/null && vulkaninfo --summary 2>/dev/null | grep -Eq 'PHYSICAL_DEVICE_TYPE_(INTEGRATED|DISCRETE)_GPU'; then P=vulkan
  else P=cpu; fi
fi
echo "[whisper-ditado] perfil: $P"
case "$P" in
  cuda) exec bash "$AQUI/engine/cuda_nvidia/instalar.sh" ;;
  vulkan) exec bash "$AQUI/engine/vulkan_amd/instalar.sh" ;;
  cpu) exec bash "$AQUI/engine/cpu_sem_gpu/instalar.sh" ;;
  *) echo "perfil inválido: $P" >&2; exit 2 ;;
esac
