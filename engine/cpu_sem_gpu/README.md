# Whisper em CPU (sem GPU utilizável) — faster-whisper int8

Instalação: `bash instalar.sh`. Mesmo daemon e mesmo serviço das variantes com GPU; só o perfil muda (`WHISPER_DITADO_DISPOSITIVO=cpu`, drop-in gerado pelo instalador).

- Latência típica: **~0,2 s por segundo de áudio** (ditado de 10 s ≈ 2 s + carga do modelo). CPU fraca: mais.
- O perfil vulkan e o cuda também usam CPU como reserva (servidor fora / CUDA indisponível): o modelo é carregado sob demanda na primeira falha.
- Diagnóstico: `systemctl --user status whisper-ditado`; teste com `ditar.py --arquivo`.
- Alternativa: modelo menor (`small`/`medium`) no final — não medido para o ditado; edite `MODELOS` no `install-engine.sh`.
