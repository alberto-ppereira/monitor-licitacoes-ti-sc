"""Monitor de licitações de TI em prefeituras de SC, a partir de dados públicos (PNCP e IBGE).

Uso:
  python main.py diagnostico            # um registro bruto de cada endpoint, para conferir os campos
  python main.py coletar --dias 30      # coleta os últimos 30 dias e grava no SQLite
  python main.py coletar                # coleta incremental desde a última execução
  python main.py exportar               # gera saida/*.csv, saida/monitor_licitacoes.xlsx e saida/dados.js
  python main.py tudo --dias 30         # coletar + exportar
  python main.py reclassificar          # refaz a marcação de TI no banco após ajustar os termos no config
  python main.py orgaos                 # carga ampla da lista de órgãos (licitações dos últimos 365 dias)
  python main.py orgaos --csv a.csv     # ou: órgãos tirados de exportações de contratos do portal do PNCP
"""
import argparse
import json
import logging
import sys
from datetime import date, timedelta

import pandas as pd

import config as C
from src import api, banco, exportacao, ibge, pncp, tratamento

log = logging.getLogger("monitor")


def diagnostico(dias: int) -> None:
    fim = date.today()
    amostra = pncp.amostra_bruta(fim - timedelta(days=dias), fim)
    for nome, registro in amostra.items():
        print(f"\n=== {nome}: {'sem registros no período' if registro is None else 'campos do primeiro registro'} ===")
        if registro:
            print(json.dumps(registro, ensure_ascii=False, indent=2)[:4000])
    print("\nConfira se os campos lidos em src/pncp.py (_linha_contratacao e _linha_contrato) existem acima.")


def coletar(con, dias: int | None) -> None:
    hoje = date.today()
    ultima = banco.ultima_coleta(con)
    completa = dias is not None or not ultima   # com --dias (ou na primeira vez) tudo é consultado do zero
    if completa:
        inicio = hoje - timedelta(days=(dias or 30) - 1)
    else:
        inicio = date.fromisoformat(ultima) - timedelta(days=1)  # um dia de sobreposição por segurança
    log.info("Coletando de %s a %s, modo %s (log também em %s)", inicio, hoje,
             "completo" if completa else "incremental", C.ARQUIVO_LOG)

    municipios = ibge.municipios()
    pop = ibge.populacao()
    if not municipios.empty:
        banco.gravar(con, "municipios", municipios.merge(pop, on="codigo_ibge", how="left"))

    pncp.falhas.clear()

    # 1. Contratações: grava logo, para não perder nada se a etapa de contratos falhar
    publicadas = pncp.contratacoes_publicadas(inicio, hoje)
    if completa or not C.PROPOSTAS_EM_ABERTO_UMA_VEZ_POR_DIA or banco.ler_controle(con, "propostas_em_aberto") != hoje.isoformat():
        log.info("Buscando contratações com propostas em aberto...")
        antes = len(pncp.falhas)
        abertas = pncp.contratacoes_em_aberto(hoje + timedelta(days=60))
        if len(pncp.falhas) == antes:
            banco.definir_controle(con, "propostas_em_aberto", hoje.isoformat())
    else:
        log.info("Propostas em aberto já consultadas hoje: etapa pulada (PROPOSTAS_EM_ABERTO_UMA_VEZ_POR_DIA).")
        abertas = publicadas.iloc[0:0]
    juntas = tratamento.padronizar_municipios(
        pd.concat([publicadas, abertas], ignore_index=True).drop_duplicates(subset="id_pncp", keep="last"), municipios)
    # todos os órgãos vistos (de TI ou não) alimentam a lista usada na coleta de contratos
    orgaos = juntas[["orgao_cnpj", "orgao", "esfera", "codigo_ibge", "municipio", "uf"]].dropna(subset=["orgao_cnpj"])
    banco.gravar(con, "orgaos", orgaos.drop_duplicates(subset="orgao_cnpj", keep="last"))
    # o banco guarda todas as municipais, com a marcação ti = 1 ou 0
    contratacoes = tratamento.marcar_ti(tratamento.filtrar_esfera(juntas))
    n1 = banco.gravar(con, "contratacoes", contratacoes)
    n1_ti = int(contratacoes["ti"].sum()) if n1 else 0
    log.info("Gravadas: %d contratações municipais, %d de TI (de %d baixadas)", n1, n1_ti, len(juntas))

    # 2. Contratos
    if C.CONTRATOS_POR_ORGAO and not banco.ler_controle(con, "carga_orgaos"):
        log.warning("A lista de órgãos ainda não teve carga ampla: câmaras, fundos e fundações que só assinam "
                    "contratos de licitações da prefeitura ficam de fora. Rode uma vez: python main.py orgaos")
    if C.CONTRATOS_POR_ORGAO:
        lista = banco.ler(con, "orgaos")
        lista = lista[tratamento.na_esfera(lista) & (lista["uf"] == C.UF)] if not lista.empty else lista
        lista = lista.drop_duplicates("orgao_cnpj").sort_values("orgao")
        com_novidade = set(juntas["orgao_cnpj"])
        revisar_antes_de = hoje - timedelta(days=C.REVISAR_TODOS_OS_ORGAOS_DIAS)
        alvo = {}
        for cnpj, nome, ate in lista[["orgao_cnpj", "orgao", "contratos_ate"]].values:
            ate = date.fromisoformat(ate) if isinstance(ate, str) else None
            if completa or ate is None:
                alvo[cnpj] = (nome, inicio)
            elif cnpj in com_novidade or ate <= revisar_antes_de:
                alvo[cnpj] = (nome, min(ate - timedelta(days=1), inicio))  # retoma de onde aquele órgão parou
        log.info("Buscando contratos de %d órgãos (%d sem novidade ficam para a revisão periódica), %d por vez...",
                 len(alvo), len(lista) - len(alvo), C.CONSULTAS_PARALELAS)
        brutos, consultados = pncp.contratos_por_orgao(alvo, hoje)
        banco.marcar_contratos_ate(con, consultados, hoje)
    else:
        brutos = pncp.contratos_nacional(inicio, hoje)
    contratos = tratamento.marcar_ti(tratamento.filtrar_esfera(tratamento.padronizar_municipios(brutos, municipios)))
    n2 = banco.gravar(con, "contratos", contratos)
    n2_ti = int(contratos["ti"].sum()) if n2 else 0
    log.info("Gravados: %d contratos municipais, %d de TI (de %d baixados)", n2, n2_ti, len(brutos))

    obs = " | ".join(pncp.falhas + pncp.orgaos_pendentes)
    # falha nas contratações: a data da última coleta não avança e a próxima incremental repete o período.
    # falha em contratos de um órgão: só aquele órgão fica pendente (contratos_ate não avança).
    banco.registrar_execucao(con, inicio, inicio if pncp.falhas else hoje, n1_ti, n2_ti, obs[:2000])
    e = api.ESTATISTICAS
    log.info("Resumo: %d chamadas à API, %d novas tentativas, %.0fs esperando a rede",
             e["chamadas"], e["novas_tentativas"], e["segundos_em_rede"])
    if pncp.falhas:
        log.warning("%d consulta(s) de contratações falharam. A próxima execução repete o período.", len(pncp.falhas))
    if pncp.orgaos_pendentes:
        log.warning("%d órgão(s) com contratos pendentes; serão consultados na próxima execução: %s",
                    len(pncp.orgaos_pendentes), "; ".join(p.split(":")[0] for p in pncp.orgaos_pendentes))


# colunas da exportação "Contratos/Empenhos" do portal do PNCP (CSV separado por ponto e vírgula)
CSV_PORTAL = {"CNPJ do Órgão/Entidade": "orgao_cnpj", "Razão Social do Órgão/Entidade": "orgao",
              "Código da Esfera do Órgão/Entidade": "esfera", "Código IBGE do Município da Unidade Executora": "codigo_ibge",
              "Nome do Município da Unidade Executora": "municipio", "Sigla UF da Unidade Executora": "uf"}


def carregar_orgaos(con, dias: int | None, csvs: list[str] | None) -> None:
    """Amplia a lista de órgãos usada na busca de contratos.

    Sem --csv: percorre as licitações publicadas no período (padrão CARGA_ORGAOS_DIAS) e guarda os órgãos e
    as licitações (com a marcação de TI). Com --csv: lê exportações de contratos do portal do PNCP.
    Os contratos desses órgãos novos são buscados na próxima `coletar`.
    """
    antes = set(banco.ler(con, "orgaos")["orgao_cnpj"])
    if csvs:
        partes = [pd.read_csv(f, sep=";", encoding="utf-8-sig", dtype=str, usecols=list(CSV_PORTAL)).rename(columns=CSV_PORTAL)
                  for f in csvs]
        orgaos = pd.concat(partes, ignore_index=True)
    else:
        hoje = date.today()
        inicio = hoje - timedelta(days=(dias or C.CARGA_ORGAOS_DIAS) - 1)
        log.info("Carga de órgãos: licitações publicadas de %s a %s, %d consultas por vez...", inicio, hoje, C.CONSULTAS_PARALELAS)
        pncp.falhas.clear()
        juntas = tratamento.padronizar_municipios(pncp.contratacoes_publicadas(inicio, hoje), ibge.municipios())
        banco.gravar(con, "contratacoes", tratamento.marcar_ti(tratamento.filtrar_esfera(juntas)))
        orgaos = juntas
        if pncp.falhas:
            log.warning("%d consulta(s) falharam; rode de novo para completar a lista.", len(pncp.falhas))
    orgaos = orgaos[["orgao_cnpj", "orgao", "esfera", "codigo_ibge", "municipio", "uf"]].dropna(subset=["orgao_cnpj"])
    orgaos = orgaos[orgaos["uf"] == C.UF].drop_duplicates("orgao_cnpj", keep="last")
    banco.gravar(con, "orgaos", orgaos)
    banco.definir_controle(con, "carga_orgaos", date.today().isoformat())
    novos = orgaos[~orgaos["orgao_cnpj"].isin(antes) & tratamento.na_esfera(orgaos)]
    log.info("Órgãos: %d lidos, %d novos na lista (municipais e consórcios). Por tipo: %s", len(orgaos), len(novos),
             novos["orgao"].map(tratamento.tipo_orgao).value_counts().to_dict())


def reclassificar(con) -> None:
    """Refaz a marcação de TI e as categorias no banco, depois de ajustar os termos no config."""
    for tabela in ("contratacoes", "contratos"):
        df = banco.ler(con, tabela)
        if df.empty:
            continue
        df = tratamento.marcar_ti(df.drop(columns=["ti", "categoria"]))
        banco.gravar(con, tabela, df[["id_pncp", "ti", "categoria"]])
        log.info("%s: %d de TI em %d registros", tabela, df["ti"].sum(), len(df))


def exportar(con, demo: bool = False) -> None:
    hoje = date.today()
    contratacoes, contratos = banco.ler(con, "contratacoes"), banco.ler(con, "contratos")
    contratacoes, contratos = contratacoes[contratacoes["ti"] == 1], contratos[contratos["ti"] == 1]
    mun = banco.ler(con, "municipios")
    pop = mun[["codigo_ibge", "populacao"]].dropna() if not mun.empty else mun
    tabelas = {
        "oportunidades": exportacao.oportunidades(contratacoes, pop, hoje),
        "movimentacoes": exportacao.movimentacoes(contratacoes, contratos, hoje),
        "contratos": exportacao.contratos_ti(contratos),
    }
    exportacao.exportar(tabelas, C.SAIDA, hoje, demo)
    log.info("Exportado em %s: %s", C.SAIDA, {k: len(v) for k, v in tabelas.items()})


def main(argv=None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    C.ARQUIVO_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(C.ARQUIVO_LOG, mode="w", encoding="utf-8")])
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("comando", choices=["diagnostico", "coletar", "exportar", "tudo", "reclassificar", "orgaos"])
    p.add_argument("--dias", type=int, help="tamanho do período a coletar (padrão: incremental, ou 30 na primeira vez)")
    p.add_argument("--csv", nargs="+", help="com 'orgaos': exportações de contratos do portal do PNCP")
    a = p.parse_args(argv)
    if a.comando == "diagnostico":
        return diagnostico(a.dias or 7)
    con = banco.conectar()
    try:
        if a.comando in ("coletar", "tudo"):
            coletar(con, a.dias)
        if a.comando == "orgaos":
            carregar_orgaos(con, a.dias, a.csv)
        if a.comando == "reclassificar":
            reclassificar(con)
        if a.comando in ("exportar", "tudo", "reclassificar"):
            exportar(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
