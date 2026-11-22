#!/usr/bin/env python3
"""Camada 2 do ditado: polimento por correções num LLM local (texto apenas; o áudio nunca sai da máquina).

Funciona com qualquer endpoint compatível com OpenAI (Ollama, llama.cpp server, vLLM, LM Studio…).

O modelo recebe o texto já corrigido pelas camadas 0-1 + o glossário (dicionario.yaml) e devolve SÓ linhas
"de => para"; as trocas são aplicadas aqui, por palavra inteira, com travas. Não reescreve.
Falha, lentidão ou LLM ocupado -> devolve o texto de entrada (as camadas 0-1 ficam valendo).

Config (env): DITADO_LLM_URL, DITADO_LLM_TOKEN_FILE, DITADO_LLM_MODEL,
DITADO_POLIR_TIMEOUT_S (padrão 4), DITADO_POLIR=0 desliga.
CLI de teste:  python3 polir.py "texto"   -> imprime JSON {texto, correcoes, ms}
"""
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dicionario  # noqa: E402

URL = os.environ.get("DITADO_LLM_URL", "http://127.0.0.1:11434/v1/chat/completions")
TOKEN_ARQ = os.environ.get("DITADO_LLM_TOKEN_FILE", os.path.expanduser("~/.config/ditado/llm_token"))
TIMEOUT = float(os.environ.get("DITADO_POLIR_TIMEOUT_S", "4"))
ATIVO = os.environ.get("DITADO_POLIR", "1").strip().lower() not in ("0", "off", "nao", "não")
MAX_TROCAS = 6

SISTEMA = """Você revisa a transcrição de um ditado por voz em português do Brasil (assunto técnico: firmware, servidores, agentes de IA).
Devolva SOMENTE as correções, uma por linha, no formato: trecho exato do texto => correção. Nada além disso.
Corrija apenas: (1) termo técnico ou nome escrito de forma errada, conforme o GLOSSÁRIO (linha "forma certa: variantes erradas"); (2) erro claro de transcrição, palavra trocada por outra de som parecido que não faz sentido na frase.
PROIBIDO: reescrever, reordenar, resumir, pontuar, mudar o sentido ou corrigir estilo. Em dúvida, não corrija. Sem correções: responda apenas NENHUMA.
GLOSSÁRIO:
{glossario}"""


def _token():
    """Lê o token opcional do arquivo configurado; sem arquivo, a chamada vai sem Authorization."""
    try:
        return open(TOKEN_ARQ).read().strip()
    except OSError:
        return ""


def pedir(texto):
    """Chama o LLM local; devolve (lista_de_correções, ms) ou levanta exceção."""
    corpo = {"model": os.environ.get("DITADO_LLM_MODEL", "local"), "temperature": 0, "max_tokens": 160, "stream": False,
             "messages": [{"role": "system", "content": SISTEMA.replace("{glossario}", dicionario.glossario_llm())},
                          {"role": "user", "content": texto}]}
    req = urllib.request.Request(URL, data=json.dumps(corpo).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + _token()})
    t = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        d = json.load(r)
    ms = int((time.time() - t) * 1000)
    bruto = d["choices"][0]["message"]["content"] or ""
    return _parse(bruto), ms


def _parse(bruto):
    """Linhas 'de => para' (formato curto: gera menos tokens); aceita também o array JSON antigo."""
    m = re.search(r"\[.*\]", bruto, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    out = []
    for linha in bruto.splitlines():
        if "=>" in linha:
            de, para = linha.split("=>", 1)
            out.append({"de": de.strip(" `\"'-*"), "para": para.strip(" `\"'")})
    return out


def aplicar(texto, correcoes):
    """Aplica as trocas com travas; devolve (texto, trocas_aplicadas)."""
    out, ok = texto, []
    for c in (correcoes or [])[:MAX_TROCAS]:
        if not isinstance(c, dict):
            continue
        de, para = str(c.get("de", "")).strip(), str(c.get("para", "")).strip()
        if not de or not para or de == para or len(para) > 3 * len(de) + 20:
            continue
        rx = re.compile(rf"(?<!\w){re.escape(de)}(?!\w)")
        if rx.search(out):
            out = rx.sub(para, out, count=1)
            ok.append((de, para))
    return out, ok


def polir(texto):
    """-> (texto_polido, trocas, ms). Sem exceção: qualquer falha devolve o texto original."""
    if not ATIVO or not texto.strip():
        return texto, [], 0
    try:
        corr, ms = pedir(texto)
        novo, ok = aplicar(texto, corr)
        return novo, ok, ms
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[polir] {type(e).__name__}: {e}\n")
        return texto, [], 0


if __name__ == "__main__":
    t0 = time.time()
    novo, ok, ms = polir(" ".join(sys.argv[1:]))
    print(json.dumps({"texto": novo, "correcoes": ok, "llm_ms": ms, "total_ms": int((time.time() - t0) * 1000)},
                     ensure_ascii=False))
