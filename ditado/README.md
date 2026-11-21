# `ditado-stream` — serviço de ditado em streaming

Serviço systemd `--user` que grava o microfone e transcreve em duas camadas:

```
clientes (plugin OpenCode, dsh, F9 global…) ──socket──► ditado_stream.py  (parciais: faster-whisper small)
                                                            └── final ──► whisper_daemon.py (turbo)
```

- **Parciais** (opcionais, `DITADO_PREVIA=1`): modelo `small` int8 em janela deslizante, enquanto você fala.
- **Final**: uma passada do `large-v3-turbo` no daemon (`whisper-ditado.service`), com o glossário do dicionário como dicas (hotwords).

## Protocolo (socket Unix `/tmp/ditado_stream.sock`, linhas JSON)

Cliente → serviço:
| Comando | Efeito |
|---|---|
| `{"cmd":"start"}` | grava o microfone (arecord) e emite parciais |
| `{"cmd":"stop"}` | para e emite o final |
| `{"cmd":"cancel"}` | descarta |
| `{"cmd":"file","path":"x.wav"}` | teste: injeta WAV 16 kHz mono em tempo real e para sozinho |
| `{"cmd":"texto","pcm_b64":...}` | transcreve PCM captado pelo próprio cliente (só o final) |
| `{"cmd":"aprender","entregue":...,"enviado":...}` | registra correção feita pelo usuário (camada 3) |
| `{"cmd":"ping"}` / `{"cmd":"estado"}` / `{"cmd":"hb"}` | utilitários |

Serviço → cliente: `{"tipo":"gravando"}` · `{"tipo":"parcial","texto":...}` · `{"tipo":"final","texto":...,"ms":N}` · `{"tipo":"polido","texto":...}` · `{"tipo":"erro","msg":...}` · `{"tipo":"cancelado"}` · `{"tipo":"estado","ativa":bool}` · `{"tipo":"pong"}`.

## Variáveis de ambiente

| Variável | Padrão | Significado |
|---|---|---|
| `DITADO_SOCK` | `/tmp/ditado_stream.sock` | socket do serviço |
| `WHISPER_DITADO_SOCK` | `/tmp/whisper_ditado.sock` | socket do daemon (final) |
| `DITADO_PREVIA` | `0` | `1` liga as parciais ao vivo |
| `DITADO_MAX_S` | `180` | tempo máximo por ditado (o cronômetro do plugin usa este limite) |
| `DITADO_SILENCIO_S` | `8` | para sozinho após N s sem texto novo (só com prévia) |
| `DITADO_DISPOSITIVO` | `auto` | `cuda` · `cpu` (motor das parciais) |
| `DITADO_POLIR` | `1` | `0` desliga o polimento por LLM (camada 2) |
| `DITADO_LLM_URL` | `http://127.0.0.1:11434/v1/chat/completions` | endpoint OpenAI-compatible do LLM local (Ollama etc.) |
| `DITADO_LLM_MODEL` | `local` | nome do modelo no endpoint |
| `DITADO_LLM_TOKEN_FILE` | `~/.config/ditado/llm_token` | token opcional (`Authorization: Bearer`) |

## Arquivos

| Arquivo | Papel |
|---|---|
| `ditado_stream.py` | o serviço (socket, captura, parciais, final) |
| `dicionario.py` / `dicionario.yaml` | camadas 0–1: dicas ao Whisper e substituição determinística |
| `aprender.py` | camada 3: aprende correções do usuário (2 ocorrências viram regra) |
| `polir.py` | camada 2 (opcional): LLM local sugere correções (texto apenas) |
| `ditar.py` + `ditar_janela.py` | ditado global fora do agente: atalho F9, janela flutuante, cola no campo focado |
| `cliente_teste.py` | teste: `python3 cliente_teste.py arquivo.wav` |
| `ditado-stream.service` | unidade systemd --user (instalada pelo `engine/install-engine.sh`) |

## Correção em camadas (por que 4 camadas?)

1. **Camada 0 — dicas ao Whisper:** termos do dicionário entram como hotwords no prompt ("Glossário: a, b." — formato medido: WER 6,8 % contra 13,7 % da lista crua de palavras).
2. **Camada 1 — dicionário determinístico:** substituição variante → forma certa, por palavra inteira, ~0 ms.
3. **Camada 2 — LLM local (opcional):** um LLM recebe o texto + glossário e devolve SÓ correções `de => para`; nunca reescreve. Falha/lento → texto de entrada vale.
4. **Camada 3 — aprendizado:** no Enter, o diff entre o texto entregue e o enviado vira candidato; com 2 ocorrências a regra entra no dicionário.

O áudio **nunca sai da máquina**.
