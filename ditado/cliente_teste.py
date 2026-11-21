#!/usr/bin/env python3
"""Teste do serviço: cliente_teste.py arquivo.wav  (imprime parciais/final com tempos)."""
import json, socket, sys, time
s = socket.socket(socket.AF_UNIX); s.connect("/tmp/ditado_stream.sock")
t0 = time.time()
s.sendall((json.dumps({"cmd": "file", "path": sys.argv[1]}) + "\n").encode())
f = s.makefile("r", encoding="utf-8"); primeiro = None
for l in f:
    m = json.loads(l); dt = time.time() - t0
    if m["tipo"] == "parcial" and primeiro is None:
        primeiro = dt
    print(f"{dt:6.2f}s {m['tipo']:9} {m.get('texto', m.get('msg', ''))[:90]}")
    if m["tipo"] in ("final", "erro"):
        print("1o_parcial_s", primeiro, "final_ms", m.get("ms")); break
