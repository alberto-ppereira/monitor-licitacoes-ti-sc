"""Tratamento: filtro de TI, categorias, padronização de municípios e pontuação. Funções puras."""
import math
import re
import unicodedata
from datetime import date

import pandas as pd

import config as C


def sem_acento(texto) -> str:
    """'Gestão Pública' -> 'gestao publica'."""
    t = unicodedata.normalize("NFD", str(texto or ""))
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def _regex(termos: list[str]) -> re.Pattern:
    # \b evita casar "erp" dentro de outras palavras
    return re.compile("|".join(rf"\b{re.escape(sem_acento(t).strip())}" for t in termos))


_TI, _EXCLUIR = _regex(C.TERMOS_TI), _regex(C.TERMOS_EXCLUIR)
_PROCESSO_TI = _regex(C.CATEGORIAS_PROCESSO_TI)
_CATEGORIAS = {cat: _regex(termos) for cat, termos in C.CATEGORIAS.items()}


def eh_ti(objeto) -> bool:
    t = sem_acento(objeto)
    return bool(_TI.search(t)) and not _EXCLUIR.search(t)


def categoria(objeto) -> str:
    t = sem_acento(objeto)
    return next((cat for cat, rx in _CATEGORIAS.items() if rx.search(t)), C.CATEGORIA_PADRAO)


def marcar_ti(df: pd.DataFrame) -> pd.DataFrame:
    """Acrescenta as colunas `ti` (1 ou 0) e `categoria`, sem descartar nada.

    É TI se o objeto tiver um termo de TI ou se a categoria do processo no PNCP for de informática
    (essa categoria só existe nos contratos). Termos de exclusão valem nos dois casos.
    O banco guarda tudo com a marcação: ao ajustar os termos no config, `python main.py reclassificar`
    refaz a marcação sem baixar nada de novo.
    """
    if df.empty:
        return df.assign(ti=pd.Series(dtype=int), categoria=pd.Series(dtype=str))
    ti = df["objeto"].map(eh_ti)
    if "categoria_processo" in df.columns:
        por_processo = df["categoria_processo"].map(lambda c: bool(_PROCESSO_TI.search(sem_acento(c))))
        ti = ti | (por_processo & ~df["objeto"].map(lambda o: bool(_EXCLUIR.search(sem_acento(o)))))
    out = df.copy()
    out["ti"] = ti.astype(int)
    out["categoria"] = out["objeto"].map(categoria)
    return out.reset_index(drop=True)


def filtrar_ti(df: pd.DataFrame) -> pd.DataFrame:
    """Mantém só os registros de TI (com a categoria)."""
    out = marcar_ti(df)
    return out[out["ti"] == 1].drop(columns="ti").reset_index(drop=True)


TIPOS_ORGAO = [  # (tipo, padrão no nome sem acento); a primeira regra que casar vence
    ("Consórcio", r"\bconsorcio"), ("Câmara", r"\bcamara\b"), ("Fundo", r"\bfundo\b"),
    ("Fundação", r"\bfundacao\b"),
    ("Autarquia", r"\b(instituto|autarquia|servico (autonomo|municipal|intermunicipal)|samae|semae|previdencia|"
                  r"agencia|companhia|empresa municipal|faculdade|aguas de)"),
    ("Prefeitura", r"\b(municipio|prefeitura|secretaria)\b"),
]
_TIPOS_ORGAO = [(t, re.compile(p)) for t, p in TIPOS_ORGAO]


def tipo_orgao(nome) -> str:
    """'FUNDO MUNICIPAL DE SAUDE DE PENHA' -> 'Fundo'. Usado para separar prefeitura, câmara, fundos..."""
    t = sem_acento(nome)
    return next((tipo for tipo, rx in _TIPOS_ORGAO if rx.search(t)), "Outro")


_SITUACOES_FORA = _regex(C.SITUACOES_FORA)


def situacao_encerrada(situacao) -> bool:
    """Revogada, anulada, suspensa, deserta ou fracassada: deixa de ser oportunidade."""
    return bool(_SITUACOES_FORA.search(sem_acento(situacao)))


def na_esfera(df: pd.DataFrame, esferas=C.ESFERAS) -> pd.Series:
    """Máscara: órgão das esferas configuradas (M = municipal) ou consórcio (esfera N), se INCLUIR_CONSORCIOS."""
    if not esferas:
        return pd.Series(True, index=df.index)
    ok = df["esfera"].isin(esferas)
    if C.INCLUIR_CONSORCIOS and "orgao" in df.columns:
        ok |= (df["esfera"] == "N") & df["orgao"].map(lambda o: "consorcio" in sem_acento(o))
    return ok


def filtrar_esfera(df: pd.DataFrame, esferas=C.ESFERAS) -> pd.DataFrame:
    """Mantém só órgãos das esferas configuradas (e consórcios, ver na_esfera)."""
    if df.empty or not esferas:
        return df
    return df[na_esfera(df, esferas)].reset_index(drop=True)


def padronizar_municipios(df: pd.DataFrame, municipios: pd.DataFrame) -> pd.DataFrame:
    """Completa o codigo_ibge pelo nome quando faltar e usa o nome oficial do IBGE."""
    if df.empty or municipios.empty:
        return df
    por_nome = dict(zip(municipios["municipio"].map(sem_acento), municipios["codigo_ibge"]))
    oficial = dict(zip(municipios["codigo_ibge"], municipios["municipio"]))
    out = df.copy()
    faltando = out["codigo_ibge"].isna() | (out["codigo_ibge"] == "")
    out.loc[faltando, "codigo_ibge"] = out.loc[faltando, "municipio"].map(lambda n: por_nome.get(sem_acento(n)))
    out["municipio"] = out["codigo_ibge"].map(oficial).fillna(out["municipio"])
    return out


def dias_ate(datas: pd.Series, hoje: date) -> pd.Series:
    return (pd.to_datetime(datas, errors="coerce") - pd.Timestamp(hoje)).dt.days


def pontuar(oport: pd.DataFrame, populacao: pd.DataFrame, hoje: date) -> pd.Series:
    """Nota de 0 a 10: porte do município, valor, proximidade da disputa e categoria estratégica."""
    if oport.empty:
        return pd.Series(dtype=float)
    pop = oport["codigo_ibge"].map(dict(zip(populacao["codigo_ibge"], populacao["populacao"]))) \
        if not populacao.empty else pd.Series(float("nan"), index=oport.index)
    escala = lambda s, lo, hi: ((s.map(lambda v: math.log10(v) if v and v > 0 else lo)) - lo).clip(0, hi - lo) / (hi - lo)
    porte = escala(pop.astype(float), 3, 6).fillna(0.3)               # 1 mil a 1 milhão de habitantes
    valor = escala(oport["valor_estimado"].astype(float), 4, 7).fillna(0.3)  # R$ 10 mil a R$ 10 milhões
    d = dias_ate(oport["data_encerramento_proposta"].fillna(oport["data_abertura_proposta"]), hoje)
    prazo = (1 - d.clip(0, 30) / 30).where(d >= 0, 0).fillna(0)
    cat = oport["categoria"].isin(C.CATEGORIAS_ESTRATEGICAS).astype(float)
    P = C.PESOS
    nota = 10 * (P["porte"] * porte + P["valor"] * valor + P["prazo"] * prazo + P["categoria"] * cat) / sum(P.values())
    return nota.round(1)
