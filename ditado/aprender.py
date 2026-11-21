#!/usr/bin/env python3
"""Camada 3 do ditado: aprende com as correções do usuário.

No Enter, compara o texto ENTREGUE (como o ditado deixou) com o ENVIADO (o que o usuário confirmou). Trocas curtas
palavra→palavra viram candidatos (`de => para`); com 2 ocorrências a regra é promovida: entra no bloco APRENDIDAS
do `dicionario.yaml` e passa a valer nas camadas 0 (dica ao Whisper), 1 (substituição) e 2 (glossário do LLM local, se ligado).

Estado dos candidatos (fora do git): ~/.local/state/ditado/candidatos.json  (DITADO_ESTADO muda a pasta).
CLI:  aprender.py "entregue" "enviado"   |   aprender.py --listar   |   aprender.py --propor TERMO [VARIANTE...]
"""
import difflib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dicionario  # noqa: E402

ESTADO = os.environ.get("DITADO_ESTADO") or os.path.expanduser("~/.local/state/ditado")
ARQ_CAND = os.path.join(ESTADO, "candidatos.json")
PROMOVER_EM = 2          # ocorrências para virar regra
MAX_PALAVRAS = 4         # trocas maiores que isso são reescrita, não correção de termo
MAX_MUDADO = 0.4         # se mais de 40 % das palavras mudaram, o usuário reescreveu: não aprende
INI, FIM = "# >>> APRENDIDAS", "# <<< APRENDIDAS <<<"
_TOK = re.compile(r"\w+(?:[-']\w+)*|[^\w\s]", re.U)


def _tokens(t):
    return _TOK.findall(t)


def candidatos_do_diff(entregue, enviado):
    """-> [(de, para)] trocas curtas de palavras (ignora só-caixa, só-pontuação, inserções e remoções)."""
    a, b = _tokens(entregue), _tokens(enviado)
    if not a or not b:
        return []
    sm = difflib.SequenceMatcher(None, [x.lower() for x in a], [x.lower() for x in b], autojunk=False)
    mudado = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
    if mudado / max(len(a), len(b)) > MAX_MUDADO:
        return []
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 > MAX_PALAVRAS or j2 - j1 > MAX_PALAVRAS:
            continue
        de, para = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if not re.search(r"\w", de) or not re.search(r"\w", para):
            continue
        if de.lower() == para.lower() and de == para:
            continue
        out.append((de, para))
    return out


ARQ_HIST = os.path.join(ESTADO, "historico.jsonl")
HIST_MAX = 500           # só os últimos 500 ditados ficam (local, nunca versionado)


def historico_add(texto, bruto=""):
    """Guarda o texto entregue (só texto; o áudio nunca é guardado) para a camada 4."""
    try:
        os.makedirs(ESTADO, exist_ok=True)
        with open(ARQ_HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": int(time.time()), "texto": texto, "bruto": bruto}, ensure_ascii=False) + "\n")
        if os.path.getsize(ARQ_HIST) > HIST_MAX * 600:
            linhas = open(ARQ_HIST, encoding="utf-8").read().splitlines()[-HIST_MAX:]
            open(ARQ_HIST, "w", encoding="utf-8").write("\n".join(linhas) + "\n")
    except OSError:
        pass


def historico_ler(dias=14):
    lim, out = time.time() - dias * 86400, []
    try:
        for linha in open(ARQ_HIST, encoding="utf-8"):
            try:
                h = json.loads(linha)
            except ValueError:
                continue
            if h.get("t", 0) >= lim:
                out.append(h)
    except OSError:
        pass
    return out


def _ler():
    try:
        return json.load(open(ARQ_CAND))
    except (OSError, ValueError):
        return {}


def _gravar(d):
    os.makedirs(ESTADO, exist_ok=True)
    tmp = ARQ_CAND + ".tmp"
    json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, ARQ_CAND)


def promover(de, para, origem="aprendida"):
    """Acrescenta `de` como variante de `para` no bloco APRENDIDAS do dicionario.yaml (reescreve só o bloco)."""
    arq = dicionario.ARQ
    txt = open(arq, encoding="utf-8").read()
    i, f = txt.index(INI), txt.index(FIM)
    ini_fim = txt.index("\n", i) + 1
    bloco = txt[ini_fim:f]
    regras = {}
    for linha in bloco.splitlines():
        m = re.match(r"\s*-\s*(\{.*\})\s*$", linha)
        if m:
            try:
                r = json.loads(m.group(1))
            except ValueError:
                continue
            regras[r["c"]] = r["v"]
    v = regras.setdefault(para, [])
    if de not in v:
        v.append(de)
    novo = "aprendidas:\n" + "".join(
        "  - %s\n" % json.dumps({"c": c, "v": vs}, ensure_ascii=False)
        for c, vs in regras.items()) if regras else "aprendidas: []\n"
    tmp = arq + ".tmp"
    open(tmp, "w", encoding="utf-8").write(txt[:ini_fim] + novo + txt[f:])
    os.replace(tmp, arq)
    sys.stderr.write(f"[aprender] promovida ({origem}): {de!r} => {para!r}\n")


def registrar(entregue, enviado):
    """Chamado no Enter. Devolve a lista de regras promovidas agora."""
    promovidas = []
    cand = _ler()
    agora = int(time.time())
    for de, para in candidatos_do_diff(entregue, enviado):
        k = f"{de} => {para}"
        c = cand.setdefault(k, {"n": 0})
        c["n"] += 1
        c["ult"] = agora
        if c["n"] >= PROMOVER_EM:
            promover(de, para)
            promovidas.append((de, para))
            del cand[k]
    _gravar(cand)
    return promovidas


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--listar"]:
        print(json.dumps(_ler(), ensure_ascii=False, indent=1))
    elif a[:1] == ["--propor"] and len(a) >= 2:       # camada 4: termo novo já validado
        for v in a[2:] or []:
            promover(v, a[1], origem="proposta")
    elif len(a) == 2:
        print(registrar(a[0], a[1]))
    else:
        sys.exit(__doc__)
