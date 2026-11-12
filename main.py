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
  python main.py importar --csv a.csv   # órgãos e contratos de exportações do portal (carga histórica rápida)
  python main.py tudo --sem-contratos   # coleta só licitações (rápido) e exporta
  python main.py buscar                 # carga histórica: contratos com "sistema" desde 2021, pela busca do portal
"""
import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import config as C
from src import api, banco, exportacao, ibge, looker, pncp, tratamento

log = logging.getLogger("monitor")


def diagnostico(dias: int) -> None:
    fim = date.today()
    amostra = pncp.amostra_bruta(fim - timedelta(days=dias), fim)
    for nome, registro in amostra.items():
        print(f"\n=== {nome}: {'sem registros no período' if registro is None else 'campos do primeiro registro'} ===")
        if registro:
            print(json.dumps(registro, ensure_ascii=False, indent=2)[:4000])
    print("\nConfira se os campos lidos em src/pncp.py (_linha_contratacao e _linha_contrato) existem acima.")


def coletar(con, dias: int | None, com_contratos: bool = True) -> None:
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
    if not com_contratos:
        log.info("Etapa de contratos pulada (--sem-contratos): o painel usa os contratos já no banco.")
        brutos = pd.DataFrame(columns=pncp.COLUNAS_CONTRATOS)
    elif C.CONTRATOS_POR_ORGAO and not banco.ler_controle(con, "carga_orgaos"):
        log.warning("A lista de órgãos ainda não teve carga ampla: câmaras, fundos e fundações que só assinam "
                    "contratos de licitações da prefeitura ficam de fora. Rode uma vez: python main.py orgaos")
    if com_contratos and C.CONTRATOS_POR_ORGAO:
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
    elif com_contratos:
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
    log.info("Resumo: %d chamadas à API, %d novas tentativas (%d pedidos do PNCP para desacelerar), %.0fs esperando a rede",
             e["chamadas"], e["novas_tentativas"], e["desaceleracoes"], e["segundos_em_rede"])
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

    Sem --csv: percorre as licitações publicadas no período (padrão CARGA_ORGAOS_DIAS), mês a mês, e guarda
    os órgãos e as licitações (com a marcação de TI). Cada mês concluído sem falha fica anotado: se a carga
    for interrompida, rodar de novo continua do mês que faltou.
    Com --csv: lê exportações "Contratos/Empenhos" do portal do PNCP.
    Os contratos dos órgãos novos são buscados na próxima `coletar`.
    """
    antes = set(banco.ler(con, "orgaos")["orgao_cnpj"])
    if csvs:
        faltando = [f for f in csvs if not Path(f).is_file()]
        if faltando:
            sys.exit("Arquivo não encontrado: " + "; ".join(faltando) +
                     "\nPasse o caminho do CSV exportado do portal, entre aspas, por exemplo: "
                     "python main.py orgaos --csv \"C:\\Users\\voce\\Downloads\\consulta-contratos-empenhos.csv\"")
        partes = [pd.read_csv(f, sep=";", encoding="utf-8-sig", dtype=str, usecols=list(CSV_PORTAL)).rename(columns=CSV_PORTAL)
                  for f in csvs]
        _gravar_orgaos(con, pd.concat(partes, ignore_index=True), antes)
        return
    hoje = date.today()
    inicio = hoje - timedelta(days=(dias or C.CARGA_ORGAOS_DIAS) - 1)
    meses = list(pncp.janelas(inicio, hoje, 30))
    feitos = {m for m in meses if banco.ler_controle(con, f"carga_orgaos:{m[0]}:{m[1]}")}
    log.info("Carga de órgãos: licitações publicadas de %s a %s em %d blocos de até 30 dias (%d já feitos), %d consultas por vez.",
             inicio, hoje, len(meses), len(feitos), C.CONSULTAS_PARALELAS_LICITACOES)
    municipios, t0, pendentes = ibge.municipios(), time.monotonic(), 0
    a_fazer = [m for m in meses if m not in feitos]
    for n, (a, b) in enumerate(a_fazer, 1):
        pncp.falhas.clear()
        juntas = tratamento.padronizar_municipios(pncp.contratacoes_publicadas(a, b), municipios)
        banco.gravar(con, "contratacoes", tratamento.marcar_ti(tratamento.filtrar_esfera(juntas)))
        _gravar_orgaos(con, juntas, antes, silencioso=True)
        if pncp.falhas:
            pendentes += 1
            log.warning("Bloco %s a %s: %d consulta(s) falharam; ele será refeito na próxima execução.", a, b, len(pncp.falhas))
        else:
            banco.definir_controle(con, f"carga_orgaos:{a}:{b}", hoje.isoformat())
        restante = (time.monotonic() - t0) / n * (len(a_fazer) - n)
        log.info("Bloco %d de %d (%s a %s): %d licitações | faltam ~%s", n, len(a_fazer), f"{a:%d/%m/%y}", f"{b:%d/%m/%y}",
                 len(juntas), pncp._tempo(restante))
    _gravar_orgaos(con, banco.ler(con, "orgaos").iloc[0:0], antes)  # só o resumo
    if pendentes:
        log.warning("%d bloco(s) ficaram incompletos. Rode 'python main.py orgaos' de novo: só eles serão refeitos.", pendentes)


def _gravar_orgaos(con, df: pd.DataFrame, antes: set, silencioso: bool = False) -> None:
    if not df.empty:
        df = df[["orgao_cnpj", "orgao", "esfera", "codigo_ibge", "municipio", "uf"]].dropna(subset=["orgao_cnpj"])
        banco.gravar(con, "orgaos", df[df["uf"] == C.UF].drop_duplicates("orgao_cnpj", keep="last"))
        banco.definir_controle(con, "carga_orgaos", date.today().isoformat())
    if silencioso:
        return
    org = banco.ler(con, "orgaos")
    novos = org[~org["orgao_cnpj"].isin(antes) & tratamento.na_esfera(org) & (org["uf"] == C.UF)]
    log.info("Órgãos: %d novos na lista (municipais e consórcios), %d no total. Novos por tipo: %s", len(novos),
             int((tratamento.na_esfera(org) & (org["uf"] == C.UF)).sum()), novos["orgao"].map(tratamento.tipo_orgao).value_counts().to_dict())


def _data_br(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="%d/%m/%Y", errors="coerce").dt.strftime("%Y-%m-%d")


def _valor_br(s: pd.Series) -> pd.Series:
    """'R$ 40.552,08' -> 40552.08"""
    return pd.to_numeric(s.str.replace(r"[R$\s.]", "", regex=True).str.replace(",", "."), errors="coerce")


def importar(con, csvs: list[str]) -> None:
    """Importa exportações "Contratos/Empenhos" do portal do PNCP: órgãos e contratos.

    Útil para a carga histórica: o portal exporta milhares de contratos em segundos, enquanto a API exige
    uma consulta por órgão. Os contratos passam pelos mesmos filtros (esfera, TI) da coleta pela API.
    """
    carregar_orgaos(con, None, csvs)
    df = pd.concat([pd.read_csv(f, sep=";", encoding="utf-8-sig", dtype=str, keep_default_na=False) for f in csvs],
                   ignore_index=True).drop_duplicates("Id Contrato PNCP")
    df = df.replace("", None)
    contratos = pd.DataFrame({
        "id_pncp": df["Id Contrato PNCP"], "id_compra": df["Id Contratação PNCP"],
        "orgao_cnpj": df["CNPJ do Órgão/Entidade"], "orgao": df["Razão Social do Órgão/Entidade"],
        "esfera": df["Código da Esfera do Órgão/Entidade"], "codigo_ibge": df["Código IBGE do Município da Unidade Executora"],
        "municipio": df["Nome do Município da Unidade Executora"], "uf": df["Sigla UF da Unidade Executora"],
        "fornecedor_cnpj": df["NI do fornecedor (sem máscara)"], "fornecedor": df["Nome do fornecedor"],
        "objeto": df["Objeto do Contrato/Empenho"].str.split().str.join(" "), "categoria_processo": None,
        "valor_inicial": None, "valor_global": _valor_br(df["Valor Global Contratado"]),
        "data_assinatura": _data_br(df["Data de Assinatura"]), "inicio_vigencia": _data_br(df["Data de Início da Vigência"]),
        "fim_vigencia": _data_br(df["Data de Fim da Vigência"]), "data_publicacao": _data_br(df["Data de Publicação no PNCP"]),
        "tipo": df["Nome do Tipo do Contrato/Empenho"], "plataforma": df["Nome da Fonte/Plataforma"],
    })
    contratos = contratos[contratos["uf"] == C.UF]
    contratos = tratamento.marcar_ti(tratamento.filtrar_esfera(tratamento.padronizar_municipios(contratos, ibge.municipios())))
    n = banco.gravar(con, "contratos", contratos)
    log.info("Importados: %d contratos municipais de %s, %d de TI, em %d municípios.", n, C.UF,
             int(contratos["ti"].sum()) if n else 0, contratos["codigo_ibge"].nunique() if n else 0)


def buscar(con, termo: str, desde: date) -> None:
    """Carga histórica pela busca textual do portal do PNCP: contratos com `termo`, trimestre a trimestre.

    Alternativa às exportações manuais em CSV. Cada trimestre concluído (e já encerrado) fica anotado: rodar de
    novo continua do que faltou. Os contratos passam pelos mesmos filtros (esfera, TI) da coleta pela API.
    """
    hoje = date.today()
    municipios = ibge.municipios()
    blocos = list(pncp.janelas(desde, hoje, C.BUSCA_JANELA_DIAS))
    feitos = {b for b in blocos if b[1] < hoje and banco.ler_controle(con, f"busca:{termo}:{b[0]}:{b[1]}")}
    a_fazer = [b for b in blocos if b not in feitos]
    log.info("Busca '%s' no portal do PNCP: contratos de %s assinados de %s a %s, %d blocos (%d já feitos).",
             termo, C.UF, desde, hoje, len(blocos), len(feitos))
    t0, total_ti, incompletos = time.monotonic(), 0, 0
    for n, (a, b) in enumerate(a_fazer, 1):
        brutos, completo = pncp.busca_contratos(termo, a, b)
        contratos = tratamento.marcar_ti(tratamento.filtrar_esfera(tratamento.padronizar_municipios(
            brutos[brutos["uf"] == C.UF] if not brutos.empty else brutos, municipios)))
        banco.gravar(con, "contratos", contratos)
        _gravar_orgaos(con, contratos, set(), silencioso=True)
        total_ti += int(contratos["ti"].sum()) if len(contratos) else 0
        if completo and b < hoje:
            banco.definir_controle(con, f"busca:{termo}:{a}:{b}", hoje.isoformat())
        elif not completo:
            incompletos += 1
        restante = (time.monotonic() - t0) / n * (len(a_fazer) - n)
        log.info("Bloco %d de %d (%s a %s): %d contratos, %d municipais, %d de TI | faltam ~%s", n, len(a_fazer),
                 f"{a:%d/%m/%y}", f"{b:%d/%m/%y}", len(brutos), len(contratos),
                 int(contratos["ti"].sum()) if len(contratos) else 0, pncp._tempo(restante))
    log.info("Busca concluída: %d contratos de TI gravados nesta execução.", total_ti)
    if incompletos:
        log.warning("%d bloco(s) incompletos; rode 'python main.py buscar' de novo para completar.", incompletos)


def aditivos(con, todos: bool = False) -> None:
    """Consulta os termos aditivos dos contratos de TI e grava a vigência prorrogada.

    Alvo: contratos de TI vencidos há até ADITIVOS_VENCIDOS_HA_DIAS ou que vencem nos próximos
    ADITIVOS_VENCENDO_EM_DIAS, ainda não consultados ou consultados há mais de ADITIVOS_REVISAR_DIAS
    (com --todos, ignora esse prazo). Pode ser interrompido: o que já foi consultado fica gravado.
    """
    hoje = date.today()
    ct = banco.ler(con, "contratos")
    ct = ct[ct["ti"] == 1]
    fim = banco.vigencia_efetiva(ct)["fim_vigencia"]
    alvo = ct[fim.between(str(hoje - timedelta(days=C.ADITIVOS_VENCIDOS_HA_DIAS)),
                          str(hoje + timedelta(days=C.ADITIVOS_VENCENDO_EM_DIAS)))]
    if not todos:
        limite = (datetime.now() - timedelta(days=C.ADITIVOS_REVISAR_DIAS)).isoformat(timespec="seconds")
        alvo = alvo[alvo["termos_consultados_em"].isna() | (alvo["termos_consultados_em"] < limite)]
    ids = alvo["id_pncp"].tolist()
    log.info("Termos aditivos: %d contratos de TI para consultar (vencidos há até %d dias ou vencendo em %d).",
             len(ids), C.ADITIVOS_VENCIDOS_HA_DIAS, C.ADITIVOS_VENCENDO_EM_DIAS)
    gravados = prorrogados = 0
    for i in range(0, len(ids), 200):   # grava em blocos: uma interrupção perde no máximo um bloco
        df, falhas = pncp.termos_em_lote(ids[i:i + 200])
        if not df.empty:
            df["termos_consultados_em"] = datetime.now().isoformat(timespec="seconds")
            gravados += banco.gravar(con, "contratos", df)
            prorrogados += df["fim_vigencia_aditivo"].notna().sum()
        if falhas:
            log.warning("%d contrato(s) sem resposta neste bloco; serão consultados na próxima execução.", len(falhas))
    antes = (ct["fim_vigencia"] >= str(hoje)).sum()
    depois = (banco.vigencia_efetiva(banco.ler(con, "contratos").query("ti == 1"))["fim_vigencia"] >= str(hoje)).sum()
    log.info("Termos aditivos: %d contratos consultados, %d com prorrogação. Contratos de TI vigentes: %d -> %d.",
             gravados, prorrogados, antes, depois)


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
    todas, contratos = banco.ler(con, "contratacoes"), banco.ler(con, "contratos")
    contratacoes, contratos = todas[todas["ti"] == 1], banco.vigencia_efetiva(contratos[contratos["ti"] == 1])
    mun = banco.ler(con, "municipios")
    pop = mun[["codigo_ibge", "populacao"]].dropna() if not mun.empty else mun
    tabelas = {
        "oportunidades": exportacao.oportunidades(contratacoes, pop, hoje),
        "movimentacoes": exportacao.movimentacoes(contratacoes, contratos, hoje),
        "contratos": exportacao.contratos_ti(contratos),
    }
    exportacao.exportar(tabelas, C.SAIDA, hoje, demo)
    log.info("Exportado em %s: %s", C.SAIDA, {k: len(v) for k, v in tabelas.items()})
    n = looker.exportar(tabelas["oportunidades"], tabelas["movimentacoes"], contratos, mun, todas, C.SAIDA / "looker", hoje)
    log.info("Tabelas para o Looker Studio em %s: %s", C.SAIDA / "looker", n)


def main(argv=None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    C.ARQUIVO_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(C.ARQUIVO_LOG, mode="w", encoding="utf-8")])
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("comando", choices=["diagnostico", "coletar", "exportar", "tudo", "reclassificar", "orgaos", "importar", "buscar", "aditivos"])
    p.add_argument("--dias", type=int, help="tamanho do período a coletar (padrão: incremental, ou 30 na primeira vez)")
    p.add_argument("--csv", nargs="+", help="com 'orgaos' ou 'importar': exportações de contratos do portal do PNCP")
    p.add_argument("--termo", default="sistema", help="com 'buscar': palavra-chave da busca no portal (padrão: sistema)")
    p.add_argument("--desde", default="2021-01-01", help="com 'buscar': data de assinatura inicial (AAAA-MM-DD)")
    p.add_argument("--sem-contratos", action="store_true",
                   help="com 'coletar'/'tudo': pula a busca de contratos por órgão (usa os já importados)")
    p.add_argument("--todos", action="store_true",
                   help="com 'aditivos': consulta de novo também os contratos consultados há menos de 30 dias")
    a = p.parse_args(argv)
    if a.comando == "diagnostico":
        return diagnostico(a.dias or 7)
    con = banco.conectar()
    try:
        if a.comando in ("coletar", "tudo"):
            coletar(con, a.dias, com_contratos=not a.sem_contratos)
        if a.comando in ("aditivos", "tudo"):
            aditivos(con, todos=a.todos)
        if a.comando == "buscar":
            buscar(con, a.termo, date.fromisoformat(a.desde))
        if a.comando == "importar":
            if not a.csv:
                sys.exit("Uso: python main.py importar --csv \"arquivo.csv\" [outros.csv]")
            importar(con, a.csv)
        if a.comando == "orgaos":
            carregar_orgaos(con, a.dias, a.csv)
        if a.comando == "reclassificar":
            reclassificar(con)
        if a.comando in ("exportar", "tudo", "reclassificar", "importar", "buscar", "aditivos"):
            exportar(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
