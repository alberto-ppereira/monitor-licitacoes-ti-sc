"""Publicação das tabelas do Looker Studio no BigQuery (sandbox sem custo).

Lê os 5 CSVs de saida/looker/, aplica o esquema fixo (STRING/INTEGER/FLOAT/DATE) e carrega cada
tabela com WRITE_TRUNCATE. Antes de publicar, compara as contagens com a última publicação (tabela
`controle` no BigQuery); se qualquer tabela cair mais que BIGQUERY_QUEDA_MAXIMA, aborta para não
zerar o painel por causa de uma coleta ou exportação incompleta.

Pré-requisitos:
  pip install -r requirements.txt                     # inclui google-cloud-bigquery[pandas]
  autenticação: GOOGLE_APPLICATION_CREDENTIALS apontando para a chave JSON da conta de serviço
  (ou config.BIGQUERY_CAMINHO_CHAVE preenchido; a chave fica fora do Git, em credenciais/).

Uso (via main.py):
  python main.py publicar        # publica a partir dos CSVs já exportados
  python main.py tudo            # coleta + aditivos + exportar + publicar
"""
import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

import config as C

try:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
except ImportError:   # ausente até `pip install -r requirements.txt`
    bigquery = None
    NotFound = LookupError   # nunca levantada; só para o `except` abaixo continuar válido

log = logging.getLogger(__name__)

# Nomes das 5 tabelas do Looker (mesmos arquivos .csv em saida/looker/) + controle
NOMES_TABELAS = ["oportunidades", "movimentacoes", "contratos", "municipios", "fornecedores"]


def _s(nome: str, tipo: str):
    """Atalho para bigquery.SchemaField (chamado só depois de `cliente()`, que valida a instalação)."""
    return bigquery.SchemaField(nome, tipo)


def _esquemas() -> dict[str, list]:
    """Esquema fixo de cada tabela. Identificadores (IBGE/CNPJ) em STRING para preservar zeros à esquerda;
    datas em DATE; valores em FLOAT; contagens em INTEGER."""
    return {
        "oportunidades": [
            _s("codigo_ibge", "STRING"), _s("municipio", "STRING"), _s("orgao", "STRING"),
            _s("tipo_orgao", "STRING"), _s("objeto", "STRING"), _s("categoria", "STRING"),
            _s("modalidade", "STRING"), _s("valor_estimado", "FLOAT"),
            _s("data_publicacao", "DATE"), _s("data_abertura", "DATE"),
            _s("situacao", "STRING"), _s("nota", "FLOAT"), _s("link", "STRING"),
            _s("plataforma", "STRING"), _s("local", "STRING"),
            _s("dias_ate_disputa", "INTEGER"), _s("faixa_prazo", "STRING"),
            _s("data_atualizacao", "DATE"),
        ],
        "movimentacoes": [
            _s("data", "DATE"), _s("tipo", "STRING"), _s("codigo_ibge", "STRING"),
            _s("municipio", "STRING"), _s("descricao", "STRING"), _s("fornecedor", "STRING"),
            _s("valor", "FLOAT"), _s("link", "STRING"), _s("local", "STRING"),
            _s("data_atualizacao", "DATE"),
        ],
        "contratos": [
            _s("id_pncp", "STRING"), _s("codigo_ibge", "STRING"), _s("municipio", "STRING"),
            _s("local", "STRING"), _s("orgao", "STRING"), _s("tipo_orgao", "STRING"),
            _s("fornecedor", "STRING"), _s("fornecedor_cnpj", "STRING"), _s("fornecedor_cnpj_raiz", "STRING"),
            _s("objeto", "STRING"), _s("categoria", "STRING"), _s("valor", "FLOAT"),
            _s("data_assinatura", "DATE"), _s("ano_assinatura", "STRING"),
            _s("inicio_vigencia", "DATE"), _s("fim_vigencia", "DATE"),
            _s("fim_vigencia_original", "DATE"), _s("prorrogado", "STRING"),
            _s("mensalidade", "FLOAT"), _s("base_mensalidade", "STRING"),
            _s("situacao", "STRING"), _s("dias_para_vencer", "INTEGER"),
            _s("faixa_vencimento", "STRING"), _s("plataforma", "STRING"), _s("link", "STRING"),
            _s("data_atualizacao", "DATE"),
        ],
        "municipios": [
            _s("codigo_ibge", "STRING"), _s("municipio", "STRING"), _s("populacao", "INTEGER"),
            _s("contratos_vigentes", "INTEGER"), _s("valor_vigente", "FLOAT"),
            _s("mensalidade_vigente", "FLOAT"), _s("entidades_com_contrato", "INTEGER"),
            _s("fornecedores_vigentes", "INTEGER"), _s("proximo_vencimento", "DATE"),
            _s("fornecedor_principal", "STRING"), _s("contratos_de_consorcio", "INTEGER"),
            _s("contratos_total", "INTEGER"), _s("oportunidades_abertas", "INTEGER"),
            _s("valor_oportunidades", "FLOAT"), _s("plataforma_principal", "STRING"),
            _s("situacao_ti", "STRING"), _s("local", "STRING"),
            _s("mensalidade_erp", "FLOAT"),
            _s("data_atualizacao", "DATE"),
        ],
        "fornecedores": [
            _s("fornecedor_cnpj_raiz", "STRING"), _s("fornecedor", "STRING"),
            _s("contratos_total", "INTEGER"), _s("municipios_total", "INTEGER"),
            _s("contratos_vigentes", "INTEGER"), _s("municipios_vigentes", "INTEGER"),
            _s("valor_vigente", "FLOAT"), _s("mensalidade_vigente", "FLOAT"),
            _s("proximo_vencimento", "DATE"),
            _s("data_atualizacao", "DATE"),
        ],
    }


def _esquema_controle() -> list:
    """Tabela de controle: uma linha por publicação, com data/hora e a contagem de cada tabela."""
    return [_s("publicado_em", "TIMESTAMP")] + [_s(nome, "INTEGER") for nome in NOMES_TABELAS]


def cliente():
    """Cliente do BigQuery. Usa GOOGLE_APPLICATION_CREDENTIALS ou config.BIGQUERY_CAMINHO_CHAVE."""
    if bigquery is None:
        raise RuntimeError(
            "A biblioteca google-cloud-bigquery não está instalada. "
            "Rode 'pip install -r requirements.txt' e tente de novo.")
    if C.BIGQUERY_CAMINHO_CHAVE and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = C.BIGQUERY_CAMINHO_CHAVE
    return bigquery.Client(project=C.BIGQUERY_PROJETO)


def _tabela(client, nome: str) -> str:
    return f"{client.project}.{C.BIGQUERY_DATASET}.{nome}"


def _ler_csv(caminho: Path, esquema: list) -> pd.DataFrame:
    """Lê o CSV do Looker e converte para os tipos do esquema fixo."""
    if not caminho.is_file():
        raise FileNotFoundError(
            f"CSV não encontrado: {caminho}. Rode 'python main.py exportar' antes do publicar.")
    # dtype=str + na_values=[""] preserva CNPJs com zero à esquerda e trata células vazias como nulas
    df = pd.read_csv(caminho, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8")
    for campo in esquema:
        if campo.name not in df.columns:
            raise ValueError(
                f"Coluna '{campo.name}' ausente em {caminho.name}. "
                f"O CSV está desatualizado; rode 'python main.py exportar' para regerar.")
        if campo.field_type in ("INTEGER", "FLOAT"):
            df[campo.name] = pd.to_numeric(df[campo.name], errors="coerce")
            if campo.field_type == "INTEGER":
                df[campo.name] = df[campo.name].astype("Int64")
        elif campo.field_type == "DATE":
            df[campo.name] = pd.to_datetime(df[campo.name], errors="coerce").dt.date
        # STRING: vazio fica como NaN, que o pyarrow converte em NULL
    return df[[c.name for c in esquema]]


def _publicar_tabela(client, nome: str, df: pd.DataFrame, esquema: list) -> None:
    """Carrega uma tabela com WRITE_TRUNCATE (o sandbox substitui a tabela inteira a cada publicação)."""
    job_config = bigquery.LoadJobConfig(
        schema=esquema, write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    job = client.load_table_from_dataframe(df, _tabela(client, nome), job_config=job_config)
    job.result()   # aguarda concluir; levanta exceção se falhar


def _ler_controle(client, tabela: str) -> dict | None:
    """Última publicação (uma linha). None se a tabela ainda não existe (primeira publicação)."""
    try:
        rows = list(client.list_rows(tabela, max_results=1))
    except NotFound:
        return None
    # outros erros (rede, permissão) sobem: tratá-los como "primeira publicação" pularia a checagem de queda
    if not rows:
        return None
    return {k: v for k, v in rows[0].items() if k != "publicado_em"}


def publicar_tabelas(pasta_looker: Path) -> None:
    """Publica as 5 tabelas + a de controle no BigQuery. Aborta se as contagens caírem demais."""
    client = cliente()
    esquemas = _esquemas()
    tabelas = {nome: _ler_csv(pasta_looker / f"{nome}.csv", esquemas[nome]) for nome in NOMES_TABELAS}
    contagens = {nome: len(df) for nome, df in tabelas.items()}
    log.info("Contagens a publicar: %s", contagens)

    tabela_controle = _tabela(client, C.BIGQUERY_TABELA_CONTROLE)
    anterior = _ler_controle(client, tabela_controle)
    if anterior:
        quedas = []
        for nome, qtd in contagens.items():
            qtd_ant = int(anterior.get(nome) or 0)
            if qtd_ant > 0 and qtd < qtd_ant * (1 - C.BIGQUERY_QUEDA_MAXIMA):
                quedas.append(f"  {nome}: {qtd_ant} -> {qtd} (-{100 * (1 - qtd / qtd_ant):.1f}%)")
        if quedas:
            raise RuntimeError(
                f"Publicação abortada: contagens caíram mais de {C.BIGQUERY_QUEDA_MAXIMA:.0%} "
                f"vs. a última publicação:\n" + "\n".join(quedas) +
                f"\nSe a queda é esperada, ajuste config.BIGQUERY_QUEDA_MAXIMA ou apague a tabela "
                f"{tabela_controle} no console do BigQuery.")
    else:
        log.info("Primeira publicação: sem tabela controle anterior, checagem de integridade pulada.")
        log.info("Atenção: no sandbox do BigQuery as tabelas expiram em 60 dias sem atualização.")

    for nome, df in tabelas.items():
        _publicar_tabela(client, nome, df, esquemas[nome])
        log.info("Publicado: %s (%d linhas)", _tabela(client, nome), len(df))

    agora = datetime.now()
    df_controle = pd.DataFrame([{"publicado_em": agora, **contagens}])
    _publicar_tabela(client, C.BIGQUERY_TABELA_CONTROLE, df_controle, _esquema_controle())
    log.info("Publicado: %s (publicado_em=%s)", tabela_controle, agora.isoformat(timespec="seconds"))