"""Armazenamento em SQLite com atualização por chave (rodar de novo não duplica)."""
import sqlite3
from contextlib import closing
from datetime import datetime

import pandas as pd

import config as C

ESQUEMA = """
CREATE TABLE IF NOT EXISTS contratacoes (
  id_pncp TEXT PRIMARY KEY, ano INTEGER, orgao_cnpj TEXT, orgao TEXT, esfera TEXT, unidade TEXT, codigo_ibge TEXT,
  municipio TEXT, uf TEXT, modalidade TEXT, objeto TEXT, categoria TEXT, valor_estimado REAL,
  valor_homologado REAL, situacao TEXT, data_publicacao TEXT, data_abertura_proposta TEXT,
  data_encerramento_proposta TEXT, link TEXT, plataforma TEXT, ti INTEGER, atualizado_em TEXT);
CREATE TABLE IF NOT EXISTS contratos (
  id_pncp TEXT PRIMARY KEY, id_compra TEXT, orgao_cnpj TEXT, orgao TEXT, esfera TEXT, codigo_ibge TEXT,
  municipio TEXT, uf TEXT, fornecedor_cnpj TEXT, fornecedor TEXT, objeto TEXT, categoria_processo TEXT,
  categoria TEXT, valor_inicial REAL,
  valor_global REAL, data_assinatura TEXT, inicio_vigencia TEXT, fim_vigencia TEXT, data_publicacao TEXT,
  tipo TEXT, plataforma TEXT, ti INTEGER, atualizado_em TEXT);
CREATE TABLE IF NOT EXISTS orgaos (
  orgao_cnpj TEXT PRIMARY KEY, orgao TEXT, esfera TEXT, codigo_ibge TEXT, municipio TEXT, uf TEXT,
  contratos_ate TEXT, atualizado_em TEXT);
CREATE TABLE IF NOT EXISTS controle (chave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS municipios (codigo_ibge TEXT PRIMARY KEY, municipio TEXT, populacao INTEGER);
CREATE TABLE IF NOT EXISTS execucoes (
  quando TEXT, inicio TEXT, fim TEXT, contratacoes INTEGER, contratos INTEGER, observacao TEXT);
CREATE INDEX IF NOT EXISTS ix_contratos_ibge ON contratos(codigo_ibge);
CREATE INDEX IF NOT EXISTS ix_contratacoes_ibge ON contratacoes(codigo_ibge);
"""


# Colunas acrescentadas depois da primeira versão: bancos antigos recebem a coluna automaticamente.
# `ti` entra como 1 nos registros antigos, porque as versões anteriores só gravavam registros de TI.
NOVAS_COLUNAS = {"contratacoes": {"esfera": "TEXT", "ti": "INTEGER DEFAULT 1", "plataforma": "TEXT"},
                 "contratos": {"esfera": "TEXT", "categoria_processo": "TEXT", "ti": "INTEGER DEFAULT 1",
                               "plataforma": "TEXT"},
                 "orgaos": {"contratos_ate": "TEXT"}}


def conectar(caminho=None) -> sqlite3.Connection:
    caminho = caminho or C.BANCO
    caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho)
    con.executescript(ESQUEMA)
    for tabela, colunas in NOVAS_COLUNAS.items():
        existentes = {r[1] for r in con.execute(f"PRAGMA table_info({tabela})")}
        for col, tipo in colunas.items():
            if col not in existentes:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN {col} {tipo}")
    con.commit()
    return con


def gravar(con: sqlite3.Connection, tabela: str, df: pd.DataFrame) -> int:
    """Insere ou atualiza pela chave primária (upsert). Só grava as colunas que existem na tabela.

    Numa linha que já existe, só as colunas presentes no `df` são atualizadas: as outras (por exemplo,
    `contratos_ate` dos órgãos) ficam como estavam.
    """
    if df.empty:
        return 0
    info = list(con.execute(f"PRAGMA table_info({tabela})"))
    colunas, chave = [r[1] for r in info], [r[1] for r in info if r[5]]
    df = df.assign(atualizado_em=datetime.now().isoformat(timespec="seconds")) if "atualizado_em" in colunas else df
    df = df[[c for c in colunas if c in df.columns]]
    df = df.astype(object).where(df.notna(), None)
    sql = f"INSERT INTO {tabela} ({', '.join(df.columns)}) VALUES ({', '.join('?' * len(df.columns))})"
    if chave:
        resto = [c for c in df.columns if c not in chave]
        sql += f" ON CONFLICT({', '.join(chave)}) DO " + (
            "UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in resto) if resto else "NOTHING")
    with con:
        con.executemany(sql, df.itertuples(index=False, name=None))
    return len(df)


def ler(con: sqlite3.Connection, tabela: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {tabela}", con)


def registrar_execucao(con, inicio, fim, n_contratacoes, n_contratos, obs=""):
    with con:
        con.execute("INSERT INTO execucoes VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(timespec="seconds"), str(inicio), str(fim),
                     n_contratacoes, n_contratos, obs))


def ultima_coleta(con) -> str | None:
    linha = con.execute("SELECT MAX(fim) FROM execucoes").fetchone()
    return linha[0] if linha else None


def marcar_contratos_ate(con, cnpjs: list[str], data) -> None:
    """Registra até que data os contratos de cada órgão foram consultados com sucesso."""
    with con:
        con.executemany("UPDATE orgaos SET contratos_ate = ? WHERE orgao_cnpj = ?", [(str(data), c) for c in cnpjs])


def ler_controle(con, chave: str) -> str | None:
    linha = con.execute("SELECT valor FROM controle WHERE chave = ?", (chave,)).fetchone()
    return linha[0] if linha else None


def definir_controle(con, chave: str, valor: str) -> None:
    with con:
        con.execute("INSERT INTO controle VALUES (?, ?) ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
                    (chave, valor))


__all__ = ["conectar", "gravar", "ler", "registrar_execucao", "ultima_coleta", "closing"]
