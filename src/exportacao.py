"""Exportação: três tabelas de saída (oportunidades, movimentações, contratos) em CSV, Excel e dados.js.

As colunas seguem o formato lido pelo dashboard.html; o mesmo conteúdo serve para Excel, Power BI ou CRM.
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import config as C
from src.pncp import link_contrato
from src.tratamento import dias_ate, pontuar, situacao_encerrada, tipo_orgao

COLS_OPORTUNIDADES = ["codigo_ibge", "municipio", "orgao", "tipo_orgao", "objeto", "categoria", "modalidade",
                      "valor_estimado", "data_publicacao", "data_abertura", "situacao", "nota", "link", "plataforma"]
COLS_MOVIMENTACOES = ["data", "tipo", "codigo_ibge", "municipio", "descricao", "fornecedor", "valor", "link"]
COLS_CONTRATOS = ["codigo_ibge", "municipio", "orgao", "tipo_orgao", "fornecedor", "cnpj", "objeto", "categoria",
                  "valor", "inicio_vigencia", "fim_vigencia", "link", "plataforma"]


def oportunidades(contratacoes: pd.DataFrame, populacao: pd.DataFrame, hoje: date) -> pd.DataFrame:
    """Contratações de TI com disputa (encerramento das propostas) a partir de hoje, pontuadas.

    Ficam fora as revogadas, anuladas, suspensas, desertas ou fracassadas (SITUACOES_FORA).
    """
    if contratacoes.empty:
        return pd.DataFrame(columns=COLS_OPORTUNIDADES)
    df = contratacoes[~contratacoes["situacao"].map(situacao_encerrada)].copy()
    df["tipo_orgao"] = df["orgao"].map(tipo_orgao)
    df["data_abertura"] = df["data_encerramento_proposta"].fillna(df["data_abertura_proposta"])
    df = df[dias_ate(df["data_abertura"], hoje) >= 0].copy()
    df["nota"] = pontuar(df, populacao, hoje)
    return df.sort_values("nota", ascending=False)[COLS_OPORTUNIDADES].reset_index(drop=True)


def movimentacoes(contratacoes: pd.DataFrame, contratos: pd.DataFrame, hoje: date,
                  dias: int = C.DIAS_MOVIMENTACOES) -> pd.DataFrame:
    """Eventos dos últimos `dias` dias: licitações publicadas e contratos assinados."""
    ini = (hoje - timedelta(days=dias - 1)).isoformat()
    partes = []
    if not contratacoes.empty:
        c = contratacoes[contratacoes["data_publicacao"].between(ini, hoje.isoformat())]
        partes.append(pd.DataFrame({
            "data": c["data_publicacao"], "tipo": "Licitação publicada", "codigo_ibge": c["codigo_ibge"],
            "municipio": c["municipio"], "descricao": c["objeto"], "fornecedor": "",
            "valor": c["valor_estimado"], "link": c["link"]}))
    if not contratos.empty:
        data = contratos["data_assinatura"].fillna(contratos["data_publicacao"])
        k = contratos[data.between(ini, hoje.isoformat())]
        partes.append(pd.DataFrame({
            "data": data[k.index], "tipo": "Contrato assinado", "codigo_ibge": k["codigo_ibge"],
            "municipio": k["municipio"], "descricao": k["objeto"], "fornecedor": k["fornecedor"],
            "valor": k["valor_global"], "link": k["id_pncp"].map(link_contrato)}))
    if not partes:
        return pd.DataFrame(columns=COLS_MOVIMENTACOES)
    return pd.concat(partes, ignore_index=True).sort_values("data", ascending=False)[COLS_MOVIMENTACOES]


def contratos_ti(contratos: pd.DataFrame) -> pd.DataFrame:
    if contratos.empty:
        return pd.DataFrame(columns=COLS_CONTRATOS)
    return contratos.rename(columns={"fornecedor_cnpj": "cnpj", "valor_global": "valor"}) \
        .assign(link=contratos["id_pncp"].map(link_contrato), tipo_orgao=contratos["orgao"].map(tipo_orgao))[COLS_CONTRATOS].sort_values(["municipio", "fim_vigencia"]).reset_index(drop=True)


def exportar(tabelas: dict[str, pd.DataFrame], pasta: Path, hoje: date, demo: bool = False) -> None:
    """Grava CSV (UTF-8 com BOM, abre direto no Excel), um Excel com uma aba por tabela e o dados.js."""
    pasta.mkdir(parents=True, exist_ok=True)
    for nome, df in tabelas.items():
        df.to_csv(pasta / f"{nome}.csv", index=False, encoding="utf-8-sig")
    try:
        with pd.ExcelWriter(pasta / "monitor_licitacoes.xlsx") as xl:
            for nome, df in tabelas.items():
                df.to_excel(xl, sheet_name=nome, index=False)
    except ImportError:
        pass  # openpyxl ausente: os CSVs bastam
    dados = {nome: json.loads(df.to_json(orient="records", force_ascii=False)) for nome, df in tabelas.items()}
    dados.update(gerado_em=hoje.isoformat(), uf=C.UF, demo=demo)
    js = f"window.DADOS = {json.dumps(dados, ensure_ascii=False)};\n"
    (pasta / "dados.js").write_text(js, encoding="utf-8")
    if C.DASHBOARD.exists():
        (C.DASHBOARD / "dados.js").write_text(js, encoding="utf-8")
