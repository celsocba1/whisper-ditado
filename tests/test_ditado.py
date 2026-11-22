"""Testes de regressão do whisper-ditado — sem GPU, sem serviços, sem rede.

Roda com pytest puro: os módulos de motor (faster-whisper/whisper.cpp) são
stubados, porque o que se testa aqui é o comportamento do daemon, do
dicionário e do aprendizado — não a inferência.
"""
import os
import re
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "engine"))
sys.path.insert(0, str(RAIZ / "ditado"))

# --- stubs antes de importar o daemon (não temos faster-whisper no ambiente de teste) ---
import types
import unittest.mock as mock

if "faster_whisper" not in sys.modules:
    stub = types.ModuleType("faster_whisper")
    stub.__spec__ = __import__("importlib.machinery", fromlist=["ModuleSpec"]).ModuleSpec("faster_whisper", None)
    stub.WhisperModel = mock.MagicMock()
    stub.BatchedInferencePipeline = mock.MagicMock()
    sys.modules["faster_whisper"] = stub

import whisper_daemon as daemon  # noqa: E402
import dicionario  # noqa: E402
import aprender  # noqa: E402
import ditado_stream as stream  # noqa: E402

DIC_EXEMPLO = """\
termos:
  - {c: Whisper, v: [Wisper, uísper]}
  - {c: RAID, v: [RIDE, Raide]}
  - {c: MikroTik, v: [Micro Tick, Mikrotic], dica: false}
# >>> APRENDIDAS (gerado por aprender.py — não editar à mão) >>>
aprendidas: []
# <<< APRENDIDAS <<<
"""


@pytest.fixture
def dicionario_tmp(tmp_path, monkeypatch):
    """Cada teste começa com um dicionário limpo num tmp_path (sem tocar o do usuário)."""
    arq = tmp_path / "dicionario.yaml"
    arq.write_text(DIC_EXEMPLO, encoding="utf-8")
    monkeypatch.setenv("DITADO_DICIONARIO", str(arq))
    monkeypatch.setattr(dicionario, "ARQ", str(arq))
    dicionario._cache.update(mtime=None, regras=[], termos=[], pares=[])
    return arq


# ---------------------------------------------------------------- dicionário
class TestDicionario:
    def test_corrigir_palavra_inteira(self, dicionario_tmp):
        """Variante errada vira forma certa, por palavra inteira."""
        texto, trocas = dicionario.corrigir("o Wisper local gravou no RIDE")
        assert texto == "o Whisper local gravou no RAID"
        assert ("Wisper", "Whisper") in trocas and ("RIDE", "RAID") in trocas

    def test_corrigir_sem_diferenciar_maiuscula(self, dicionario_tmp):
        """A caixa da variante não importa (WISPER, raide...)."""
        texto, _ = dicionario.corrigir("WISPER e raide")
        assert texto == "Whisper e RAID"

    def test_nao_corrigi_subpalavra(self, dicionario_tmp):
        """Correção não pode mexer dentro de outra palavra."""
        texto, _ = dicionario.corrigir("o uisperinheiro passou")
        assert "uisperinheiro" in texto  # não mexe dentro de palavra

    def test_dicas_só_com_dica_true(self, dicionario_tmp):
        """Só o que tem dica: true vai ao prompt do Whisper."""
        assert "Whisper" in dicionario.dicas()
        assert "RAID" in dicionario.dicas()
        assert "MikroTik" not in dicionario.dicas()  # dica: false

    def test_glossario_llm_formato(self, dicionario_tmp):
        """O glossário da camada 2 sai no formato 'forma: variantes'."""
        g = dicionario.glossario_llm()
        assert "Whisper: Wisper, uísper" in g
        assert "MikroTik" in g  # vai como 'também corretos' pois só tem dica:false? não — tem variantes

    def test_recarrega_quando_arquivo_muda(self, dicionario_tmp):
        """Editar o YAML vale na hora (recarga por mtime)."""
        t1, _ = dicionario.corrigir("Wisper")
        assert t1 == "Whisper"
        dicionario_tmp.write_text(
            DIC_EXEMPLO.replace("{c: Whisper, v: [Wisper, uísper]}", "{c: Whisper, v: [Wisper, uísper, guisper]}"),
            encoding="utf-8",
        )
        os.utime(dicionario_tmp)  # mtime muda
        t2, _ = dicionario.corrigir("guisper")
        assert t2 == "Whisper"


# ---------------------------------------------------------------- aprender
class TestAprender:
    def test_candidatos_de_diff(self):
        """Diff entregue × enviado gera candidato de correção."""
        pares = aprender.candidatos_do_diff("o Wisper gravou", "o Whisper gravou")
        assert ("Wisper", "Whisper") in pares

    def test_rejeita_reescrita_grande(self):
        """Trocar mais de 40 % das palavras é reescrita: não aprende."""
        # mais de 40 % das palavras mudadas: o usuário reescreveu, não corrige
        assert aprender.candidatos_do_diff("um dois tres quatro cinco", "cinco quatro tres dois um") == []

    def test_promover_grava_bloco_aprendidas(self, dicionario_tmp):
        """A regra promovida entra só no bloco APRENDIDAS do YAML."""
        aprender.promover("Wisper", "Whisper")
        texto = dicionario_tmp.read_text(encoding="utf-8")
        assert '"c": "Whisper"' in texto and '"v": ["Wisper"]' in texto

    def test_registrar_promove_apos_2_ocorrencias(self, dicionario_tmp, monkeypatch, tmp_path):
        """A mesma correção enviada 2× vira regra permanente."""
        monkeypatch.setenv("DITADO_ESTADO", str(tmp_path / "estado"))
        aprender._ler.cache_clear() if hasattr(aprender._ler, "cache_clear") else None
        r1 = aprender.registrar("o Wisper gravou", "o Whisper gravou")
        assert r1 == []                      # 1ª: só candidato
        r2 = aprender.registrar("o Wisper tocou", "o Whisper tocou")
        assert ("Wisper", "Whisper") in r2   # 2ª: vira regra


# ---------------------------------------------------------------- daemon
class TestPromptGlossario:
    def setup_method(self):
        """Isola DICAS_BASE: os testes de prompt não dependem do vocabulário do dono."""
        self.dicas_orig = daemon.DICAS_BASE
        daemon.DICAS_BASE = "OpenCode Whisper"

    def teardown_method(self):
        """Restaura DICAS_BASE."""
        daemon.DICAS_BASE = self.dicas_orig

    def test_formato_glossario(self):
        """O formato 'Glossário: a, b.' é o que mediu WER melhor."""
        p = daemon._prompt_glossario("OpenCode CUDA")
        assert p.startswith("Glossário: ") and p.endswith(".")
        assert "OpenCode" in p and "CUDA" in p

    def test_sem_repetir_termo(self):
        """Termo repetido nas dicas entra uma vez só (caixa não conta)."""
        p = daemon._prompt_glossario("whisper opencode")
        assert p.lower().count("whisper") == 1

    def test_trunca_pelo_fim_sem_estourar(self):
        """Prompt nunca passa de PROMPT_MAX caracteres."""
        p = daemon._prompt_glossario("t " * 500)
        assert len(p) <= daemon.PROMPT_MAX

    def test_contexto_nao_influencia_sem_flag(self, monkeypatch):
        """O contexto entre blocos nasce desligado (medido: piorava o WER)."""
        monkeypatch.setattr(daemon, "CONTEXTO_ATIVO", False)
        p = daemon._prompt_com_contexto("Whisper", "contexto anterior aqui")
        assert "contexto anterior" not in p


class TestCorrigirNomes:
    def test_palavra_inteira_com_acento_preservado(self):
        """Correção por palavra inteira, sem mexer em palavras parecidas."""
        assert daemon.corrigir_nomes("o RIDE caiu") == "o RAID caiu"
        assert daemon.corrigir_nomes("o rIDEAço ficou") == "o rIDEAço ficou"

    def test_personalizado_edicao(self, monkeypatch):
        """O usuário edita a lista e a correção acompanha."""
        monkeypatch.setattr(daemon, "_CORRECOES_RE", [(__import__("re").compile(r"(?<!\w)glock(?!\w)", re.I), "Grok")])
        assert daemon.corrigir_nomes("o GLOCK respondeu") == "o Grok respondeu"


class TestMarcasDeNaoFala:
    def test_remove_marcas(self):
        """Legendas inventadas do Whisper ([BLANK_AUDIO] etc.) saem do texto."""
        texto = daemon._MARCAS_RE.sub(" ", "[BLANK_AUDIO] oi [Silêncio] tudo")
        assert "[" not in texto and "*" not in texto


@pytest.mark.skipif(not hasattr(daemon, "_tem_fala"), reason="sem porta de energia")
class TestPortaEnergia:
    def test_silencio_recusa(self):
        """Áudio todo em zero não vira transcrição (VAD de energia)."""
        assert daemon._tem_fala(b"\0" * 3 * 16000 * 2) is False

    def test_pulo_forte_passa(self):
        """Pico de fala forte passa da porta mesmo com piso alto (AGC)."""
        pico = 9000
        pcm = (bytes([pico & 0xFF, (pico >> 8) & 0x7F]) + b"\0" * 3198) * 25
        assert daemon._tem_fala(pcm) is True


# ---------------------------------------------------------------- stream
class TestStreamCorrigir:
    def test_camada1_dicionario(self, dicionario_tmp):
        """A camada 1 do stream é o próprio dicionário determinístico."""
        assert stream.corrigir("Wisper RIDE") == "Whisper RAID"

    def test_reserva_mikrofone(self, dicionario_tmp):
        """Reserva do stream: erros comuns fora do YAML também são corrigidos."""
        assert stream.corrigir("liguei o mikrofone") == "liguei o microfone"
