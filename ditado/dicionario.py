#!/usr/bin/env python3
"""Dicionário do ditado: camadas 0 (dicas ao Whisper) e 1 (variante -> forma certa).

Lê `dicionario.yaml` (mesmo diretório; override: DITADO_DICIONARIO). Recarrega sozinho quando o arquivo muda.
Uso:  from dicionario import corrigir, dicas, glossario_llm
CLI:  python3 dicionario.py "texto com variantes erradas"   -> texto corrigido
"""
import os
import re
import sys
import threading

import yaml

AQUI = os.path.dirname(os.path.abspath(__file__))
ARQ = os.environ.get("DITADO_DICIONARIO") or os.path.join(AQUI, "dicionario.yaml")
_trava = threading.Lock()
_cache = {"mtime": None, "regras": [], "termos": [], "pares": []}


def _carregar():
    """Lê o YAML e devolve (regras regex, termos, pares); variante mais longa primeiro, para não cortar no meio."""
    with open(ARQ, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    termos, pares = [], []
    for t in (d.get("termos") or []) + (d.get("aprendidas") or []):
        c = str(t.get("c", "")).strip()
        if not c:
            continue
        termos.append({"c": c, "dica": t.get("dica", True)})
        for v in t.get("v") or []:
            v = str(v).strip()
            if v and v.lower() != c.lower():
                pares.append((v, c))
    pares.sort(key=lambda p: -len(p[0]))   # variante mais longa primeiro
    regras = []
    for v, c in pares:
        corpo = r"\s+".join(re.escape(p) for p in v.split())
        regras.append((re.compile(rf"(?<!\w){corpo}(?!\w)", re.I), c))
    return regras, termos, pares


def _estado():
    """Cache por mtime: só relê o arquivo quando ele mudou; YAML quebrado mantém o último bom."""
    with _trava:
        try:
            m = os.stat(ARQ).st_mtime
        except OSError:
            return _cache
        if m != _cache["mtime"]:
            try:
                _cache["regras"], _cache["termos"], _cache["pares"] = _carregar()
                _cache["mtime"] = m
            except Exception as e:  # noqa: BLE001  (YAML quebrado: mantém o último bom)
                sys.stderr.write(f"[dicionario] erro ao ler {ARQ}: {e}\n")
                _cache["mtime"] = m
        return _cache


def corrigir(texto):
    """Camada 1: substitui variantes por palavra inteira. Devolve (texto, [(de, para), ...])."""
    out, trocas = texto, []
    for rx, c in _estado()["regras"]:
        def sub(m, c=c):
            """Anota a troca feita e devolve a forma certa."""
            if m.group(0) != c:
                trocas.append((m.group(0), c))
            return c
        out = rx.sub(sub, out)
    return out, trocas


def dicas():
    """Camada 0: termos como vocabulário do Whisper (os mais importantes no fim: o daemon trunca o começo)."""
    return " ".join(t["c"] for t in _estado()["termos"] if t["dica"])


def glossario_llm():
    """Camada 2: linhas 'forma certa: variantes erradas' (formato compacto)."""
    pares = {}
    for v, c in _estado()["pares"]:
        pares.setdefault(c, []).append(v)
    linhas = [f"{c}: {', '.join(v)}" for c, v in pares.items()]
    sem = [t["c"] for t in _estado()["termos"] if t["c"] not in pares]
    if sem:
        linhas.append("Também corretos: " + ", ".join(sem))
    return "\n".join(linhas)


if __name__ == "__main__":
    t, tr = corrigir(" ".join(sys.argv[1:]))
    print(t)
    print(tr, file=sys.stderr)
