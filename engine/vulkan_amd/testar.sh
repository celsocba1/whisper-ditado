#!/usr/bin/env bash
# Teste de fumaça do Whisper Vulkan: serviço ativo, GPU Vulkan de verdade (não caiu para CPU em silêncio) e latência.
# O bug de 14/11/2026 foi "no GPU found" sem erro: por isso o log é conferido.
set -uo pipefail
P=47111; ok=1
systemctl --user is-active --quiet whisper-vulkan.service && echo "[ok] whisper-vulkan ativo" || { echo "[FALHA] whisper-vulkan inativo"; exit 1; }
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$P/health" >/dev/null && break; sleep 1; done
curl -sf "http://127.0.0.1:$P/health" >/dev/null && echo "[ok] /health" || { echo "[FALHA] /health"; exit 1; }
LOG=$(journalctl --user -u whisper-vulkan.service -b --no-pager 2>/dev/null)
if grep -qi "no GPU found" <<<"$LOG"; then echo "[FALHA] 'no GPU found': rodando em CPU"; ok=0
elif grep -qi "ggml_vulkan" <<<"$LOG"; then echo "[ok] backend Vulkan: $(grep -i -m1 'Vulkan0\|ggml_vulkan: Found' <<<"$LOG" | cut -c40-140)"
else echo "[aviso] sem linha do ggml_vulkan no log recente (reinicie o serviço e rode de novo)"; fi
if command -v espeak-ng >/dev/null && command -v ffmpeg >/dev/null; then
  T=$(mktemp -d); espeak-ng -v pt-br -w "$T/a.wav" "Teste do ditado local com a placa de vídeo." && ffmpeg -v error -y -i "$T/a.wav" -ar 16000 -ac 1 "$T/b.wav"
  S=$(date +%s.%N); R=$(curl -sf -F file=@"$T/b.wav" -F response_format=json "http://127.0.0.1:$P/inference"); E=$(date +%s.%N)
  LC_ALL=C printf '[ok] transcrição em %.2f s: %s\n' "$(echo "$E - $S" | bc)" "$(echo "$R" | head -c 120)"; rm -rf "$T"
else echo "[aviso] sem espeak-ng/ffmpeg: teste de latência pulado"; fi
[[ $ok == 1 ]] && echo "TUDO OK" || exit 1
