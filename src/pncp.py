"""Coleta no PNCP (API de consulta) e conversão para tabelas planas.

Endpoints usados (conferir nomes e parâmetros na documentação Swagger da API de consulta):
  /v1/contratacoes/publicacao  contratações publicadas num período (por modalidade e UF)
  /v1/contratacoes/proposta    contratações com recebimento de propostas em aberto
  /v1/contratos                contratos publicados num período (por órgão, com cnpjOrgao)

A API trabalha com datas AAAAMMDD e limita o tamanho do intervalo: a coleta divide o período em janelas.
Uma falha persistente numa consulta não derruba a coleta: ela é registrada e o resto segue.
  `falhas`             consultas de contratações que falharam (impedem a data da última coleta de avançar)
  `orgaos_pendentes`   órgãos cujos contratos falharam (ficam para a próxima execução, só eles)
Os nomes de campo lidos aqui ficam concentrados nas funções `_linha_*`.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Iterator

import pandas as pd
import requests

import config as C
from src.api import paginar

log = logging.getLogger(__name__)

COLUNAS_CONTRATACOES = [
    "id_pncp", "ano", "orgao_cnpj", "orgao", "esfera", "unidade", "codigo_ibge", "municipio", "uf",
    "modalidade", "objeto", "valor_estimado", "valor_homologado", "situacao",
    "data_publicacao", "data_abertura_proposta", "data_encerramento_proposta", "link", "plataforma",
]
COLUNAS_CONTRATOS = [
    "id_pncp", "id_compra", "orgao_cnpj", "orgao", "esfera", "codigo_ibge", "municipio", "uf",
    "fornecedor_cnpj", "fornecedor", "objeto", "categoria_processo", "valor_inicial", "valor_global",
    "data_assinatura", "inicio_vigencia", "fim_vigencia", "data_publicacao", "tipo", "plataforma",
]
falhas: list[str] = []            # consultas de contratações que falharam mesmo após as novas tentativas
orgaos_pendentes: list[str] = []  # órgãos cujos contratos falharam mesmo após a repassada no fim da etapa


def janelas(inicio: date, fim: date, dias: int = C.JANELA_DIAS) -> Iterator[tuple[date, date]]:
    """Divide [inicio, fim] em intervalos de até `dias` dias, sem sobreposição."""
    atual = inicio
    while atual <= fim:
        ate = min(atual + timedelta(days=dias - 1), fim)
        yield atual, ate
        atual = ate + timedelta(days=1)


def _d(x: date) -> str:
    return x.strftime("%Y%m%d")


def _data(valor) -> str | None:
    """'2026-11-03T10:15:00' ou '2026-11-03' -> '2026-11-03'."""
    return str(valor)[:10] if valor else None


def _linha_contratacao(it: dict) -> dict:
    orgao, unidade = it.get("orgaoEntidade") or {}, it.get("unidadeOrgao") or {}
    return {
        "id_pncp": it.get("numeroControlePNCP"),
        "ano": it.get("anoCompra"),
        "orgao_cnpj": orgao.get("cnpj"),
        "orgao": orgao.get("razaoSocial"),
        "esfera": orgao.get("esferaId"),
        "unidade": unidade.get("nomeUnidade"),
        "codigo_ibge": str(unidade.get("codigoIbge") or "")[:7] or None,
        "municipio": unidade.get("municipioNome"),
        "uf": unidade.get("ufSigla"),
        "modalidade": it.get("modalidadeNome"),
        "objeto": it.get("objetoCompra"),
        "valor_estimado": it.get("valorTotalEstimado"),
        "valor_homologado": it.get("valorTotalHomologado"),
        "situacao": it.get("situacaoCompraNome"),
        "data_publicacao": _data(it.get("dataPublicacaoPncp")),
        "data_abertura_proposta": _data(it.get("dataAberturaProposta")),
        "data_encerramento_proposta": _data(it.get("dataEncerramentoProposta")),
        "link": it.get("linkSistemaOrigem"),
        # sistema que publicou no PNCP (ex.: "IPM Sistemas", "Betha Sistemas"): em geral é o ERP do órgão
        "plataforma": it.get("usuarioNome"),
    }


def _linha_contrato(it: dict) -> dict:
    orgao, unidade = it.get("orgaoEntidade") or {}, it.get("unidadeOrgao") or {}
    return {
        "id_pncp": it.get("numeroControlePNCP"),
        "id_compra": it.get("numeroControlePncpCompra"),
        "orgao_cnpj": orgao.get("cnpj"),
        "orgao": orgao.get("razaoSocial"),
        "esfera": orgao.get("esferaId"),
        "codigo_ibge": str(unidade.get("codigoIbge") or "")[:7] or None,
        "municipio": unidade.get("municipioNome"),
        "uf": unidade.get("ufSigla"),
        "fornecedor_cnpj": it.get("niFornecedor"),
        "fornecedor": it.get("nomeRazaoSocialFornecedor"),
        "objeto": it.get("objetoContrato"),
        "categoria_processo": (it.get("categoriaProcesso") or {}).get("nome"),
        "valor_inicial": it.get("valorInicial"),
        "valor_global": it.get("valorGlobal"),
        "data_assinatura": _data(it.get("dataAssinatura")),
        "inicio_vigencia": _data(it.get("dataVigenciaInicio")),
        "fim_vigencia": _data(it.get("dataVigenciaFim")),
        "data_publicacao": _data(it.get("dataPublicacaoPncp")),
        "tipo": (it.get("tipoContrato") or {}).get("nome"),
        "plataforma": it.get("usuarioNome"),
    }


def link_contrato(id_pncp) -> str:
    """'10479670000196-2-000915/2026' -> página do contrato no portal do PNCP."""
    m = re.match(r"(\d{14})-2-(\d+)/(\d{4})", str(id_pncp or ""))
    return f"https://pncp.gov.br/contratos/{m[1]}/{m[3]}/{int(m[2])}" if m else ""


def _tabela(linhas: list[dict], colunas: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(linhas, columns=colunas)
    return df.drop_duplicates(subset="id_pncp", keep="last").reset_index(drop=True)


def _coletar(url: str, params: dict, conversor, descricao: str, registro: list | None = None) -> list[dict]:
    """Percorre todas as páginas; se a consulta falhar de vez, anota em `registro` e devolve o que conseguiu."""
    linhas = []
    try:
        for it in paginar(url, params, descricao):
            linhas.append(conversor(it))
    except (requests.RequestException, ValueError) as e:
        (falhas if registro is None else registro).append(f"{descricao}: {e}")
        log.error("Falhou %s (%s). Seguindo com %d registros dessa consulta.", descricao, e, len(linhas))
    return linhas


def _em_paralelo(tarefas: list[tuple[str, dict, str]]) -> list[dict]:
    """Executa consultas de contratações (url, params, descrição) em paralelo, na ordem em que terminarem."""
    linhas = []
    with ThreadPoolExecutor(max_workers=max(1, C.CONSULTAS_PARALELAS)) as ex:
        futuros = {ex.submit(_coletar, url, params, _linha_contratacao, desc): desc for url, params, desc in tarefas}
        for futuro in as_completed(futuros):
            novas = futuro.result()
            linhas += novas
            log.info("  %s: %d", futuros[futuro], len(novas))
    return linhas


def contratacoes_publicadas(inicio: date, fim: date, uf: str = C.UF) -> pd.DataFrame:
    """Contratações publicadas no período, em todas as modalidades configuradas."""
    url = f"{C.PNCP_CONSULTA}/v1/contratacoes/publicacao"
    tarefas = [(url, {"dataInicial": _d(a), "dataFinal": _d(b), "codigoModalidadeContratacao": mod, "uf": uf,
                      "tamanhoPagina": C.TAMANHO_PAGINA_CONTRATACOES},
                f"contratações publicadas {a:%d/%m/%y} a {b:%d/%m/%y}, {C.NOMES_MODALIDADES.get(mod, mod)}")
               for a, b in janelas(inicio, fim) for mod in C.MODALIDADES]
    return _tabela(_em_paralelo(tarefas), COLUNAS_CONTRATACOES)


def contratacoes_em_aberto(ate: date, uf: str = C.UF) -> pd.DataFrame:
    """Contratações com recebimento de propostas em aberto até a data informada."""
    url = f"{C.PNCP_CONSULTA}/v1/contratacoes/proposta"
    tarefas = [(url, {"dataFinal": _d(ate), "codigoModalidadeContratacao": mod, "uf": uf,
                      "tamanhoPagina": C.TAMANHO_PAGINA_CONTRATACOES},
                f"propostas em aberto, {C.NOMES_MODALIDADES.get(mod, mod)}") for mod in C.MODALIDADES]
    return _tabela(_em_paralelo(tarefas), COLUNAS_CONTRATACOES)


def _contratos_do_orgao(cnpj: str, nome: str, desde: date, fim: date) -> tuple[list[dict], list[str]]:
    """Contratos de um órgão de `desde` a `fim`. Devolve (linhas, erros)."""
    url = f"{C.PNCP_CONSULTA}/v1/contratos"
    linhas, erros = [], []
    for a, b in janelas(desde, fim):
        params = {"dataInicial": _d(a), "dataFinal": _d(b), "cnpjOrgao": cnpj,
                  "tamanhoPagina": C.TAMANHO_PAGINA_CONTRATOS}
        novas = _coletar(url, params, _linha_contrato, f"contratos {(nome or cnpj)[:50]} {a:%d/%m} a {b:%d/%m}", erros)
        outros = [l for l in novas if l["orgao_cnpj"] != cnpj]
        if outros:  # a API ignorou o filtro: avisar em vez de gravar contratos de outros órgãos
            log.warning("A API devolveu contratos de outros órgãos para %s; descartados %d.", cnpj, len(outros))
        linhas += [l for l in novas if l["orgao_cnpj"] == cnpj]
    return linhas, erros


def contratos_por_orgao(alvo: dict[str, tuple[str, date]], fim: date) -> tuple[pd.DataFrame, list[str]]:
    """Contratos de cada órgão (parâmetro cnpjOrgao), vários órgãos ao mesmo tempo.

    `alvo`: CNPJ -> (nome, data inicial daquele órgão). Os órgãos que falharem são repassados uma vez no
    fim da etapa. Devolve (contratos, CNPJs consultados com sucesso); os que falharam de novo vão para
    `orgaos_pendentes` e ficam para a próxima execução.
    """
    orgaos_pendentes.clear()
    linhas, ok, falhou = [], [], {}
    t0, n = time.monotonic(), len(alvo)
    with ThreadPoolExecutor(max_workers=max(1, C.CONSULTAS_PARALELAS)) as ex:
        futuros = {ex.submit(_contratos_do_orgao, cnpj, nome, desde, fim): cnpj for cnpj, (nome, desde) in alvo.items()}
        for i, futuro in enumerate(as_completed(futuros), 1):
            cnpj = futuros[futuro]
            novas, erros = futuro.result()
            linhas += novas
            if erros:
                falhou[cnpj] = erros
            else:
                ok.append(cnpj)
            restante = (time.monotonic() - t0) / i * (n - i)
            log.info("[%d/%d] %s: %d contratos%s | total %d | faltam ~%s", i, n, (alvo[cnpj][0] or cnpj)[:60],
                     len(novas), " (FALHOU, será repetido)" if erros else "", len(linhas), _tempo(restante))
    if falhou:
        log.info("Repetindo %d órgão(s) que falharam, em %ss...", len(falhou), C.ESPERA_ANTES_DE_REPETIR_S)
        time.sleep(C.ESPERA_ANTES_DE_REPETIR_S)
        for cnpj in list(falhou):
            nome, desde = alvo[cnpj]
            novas, erros = _contratos_do_orgao(cnpj, nome, desde, fim)
            linhas += novas
            if erros:
                orgaos_pendentes.append(f"{nome or cnpj}: {erros[0]}")
                log.error("Falhou de novo: %s. Fica para a próxima execução.", nome or cnpj)
            else:
                ok.append(cnpj)
                log.info("Recuperado na repetição: %s, %d contratos.", nome or cnpj, len(novas))
    return _tabela(linhas, COLUNAS_CONTRATOS), ok


def _tempo(segundos: float) -> str:
    m, s = divmod(int(segundos), 60)
    return f"{m}min{s:02d}s" if m else f"{s}s"


def contratos_nacional(inicio: date, fim: date, uf: str = C.UF) -> pd.DataFrame:
    """Alternativa lenta: percorre os contratos do país no período e filtra a UF."""
    url = f"{C.PNCP_CONSULTA}/v1/contratos"
    linhas = []
    for a, b in janelas(inicio, fim):
        params = {"dataInicial": _d(a), "dataFinal": _d(b), "tamanhoPagina": C.TAMANHO_PAGINA_CONTRATOS}
        novas = [l for l in _coletar(url, params, _linha_contrato, f"contratos {a} a {b}") if l["uf"] == uf]
        linhas += novas
        log.info("  contratos %s a %s (%s): %d", f"{a:%d/%m}", f"{b:%d/%m}", uf, len(novas))
    return _tabela(linhas, COLUNAS_CONTRATOS)


def amostra_bruta(inicio: date, fim: date) -> dict:
    """Um registro bruto de cada endpoint, para conferir os nomes de campo (comando `diagnostico`)."""
    from src.api import get_json
    base = {"dataInicial": _d(inicio), "dataFinal": _d(fim), "pagina": 1}
    pub = get_json(f"{C.PNCP_CONSULTA}/v1/contratacoes/publicacao",
                   {**base, "codigoModalidadeContratacao": 6, "uf": C.UF, "tamanhoPagina": 10})
    con = get_json(f"{C.PNCP_CONSULTA}/v1/contratos", {**base, "tamanhoPagina": 10})
    pegar = lambda corpo: (corpo or {}).get("data", [None])[0] if (corpo or {}).get("data") else None
    return {"contratacoes": pegar(pub), "contratos": pegar(con)}
