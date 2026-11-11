"""Onde um fornecedor (por padrão, a IPM Sistemas) tem contratos com órgãos municipais de SC.

A API de consulta do PNCP não filtra contratos por fornecedor. Por isso o script percorre o histórico de
contratos de cada órgão municipal de SC (lista que vem do monitor.db, montada pela coleta principal) e
separa os do fornecedor pela raiz do CNPJ (8 primeiros dígitos, o que inclui as filiais).

Guarda um cache próprio (data/historico_contratos.db) com os contratos do fornecedor e os demais contratos
de TI, e quais períodos de cada órgão já foram consultados: interromper (Ctrl+C) e rodar de novo continua
de onde parou. No fim, gera saida/fornecedor_*.csv e o mapa dashboard/mapa_fornecedor.html.

Uso:
  python fornecedor_no_mapa.py                       # IPM Sistemas, contratos desde FORNECEDOR_DESDE
  python fornecedor_no_mapa.py --limite 20           # teste rápido com os 20 primeiros órgãos
  python fornecedor_no_mapa.py --so-prefeituras      # só os órgãos "Município de ..." (mais rápido)
  python fornecedor_no_mapa.py --desde 2021-04-01    # histórico maior (mais demorado)
  python fornecedor_no_mapa.py --exportar            # só refaz CSV e mapa com o que já está no cache
  python fornecedor_no_mapa.py --cnpj 00456865 --nome "Betha Sistemas"      # outro fornecedor
  python fornecedor_no_mapa.py --cnpj 01258027 44104389 --nome "Grupo IPM"  # grupo econômico (várias raízes)

Limitação importante: o PNCP só tem o que os órgãos publicaram nele (obrigatório com a Lei 14.133;
muitos municípios começaram em 2023/2024). Contratos antigos, assinados pela Lei 8.666 e mantidos por
aditivos, podem não aparecer. A vigência é a do registro do contrato; prorrogações por termo aditivo
podem não estar refletidas.
"""
import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
import requests

import config as C
from src import banco, ibge, tratamento
from src.api import paginar
from src.pncp import COLUNAS_CONTRATOS, _linha_contrato, _tempo, janelas, link_contrato

log = logging.getLogger("fornecedor")
URL = f"{C.PNCP_CONSULTA}/v1/contratos"
janela_atual = C.JANELA_HISTORICO_DIAS   # diminui sozinha se a API recusar o intervalo


# ---------------------------------------------------------------- cache
def abrir_cache() -> sqlite3.Connection:
    con = banco.conectar(C.BANCO_HISTORICO)   # mesmo esquema do monitor.db (usa só a tabela contratos)
    con.execute("CREATE TABLE IF NOT EXISTS consultas (orgao_cnpj TEXT, inicio TEXT, fim TEXT, "
                "registros INTEGER, quando TEXT, PRIMARY KEY (orgao_cnpj, inicio, fim))")
    con.commit()
    return con


def periodos_feitos(con, cnpj: str) -> list[tuple[date, date]]:
    return [(date.fromisoformat(a), date.fromisoformat(b))
            for a, b in con.execute("SELECT inicio, fim FROM consultas WHERE orgao_cnpj = ?", (cnpj,))]


def lacunas(desde: date, ate: date, feitos: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Trechos de [desde, ate] ainda não cobertos pelos períodos já consultados."""
    faltam, atual = [], desde
    for a, b in sorted(feitos):
        if b < atual:
            continue
        if a > atual:
            faltam.append((atual, min(a - timedelta(days=1), ate)))
        atual = max(atual, b + timedelta(days=1))
        if atual > ate:
            break
    if atual <= ate:
        faltam.append((atual, ate))
    return [(a, b) for a, b in faltam if a <= b]


# ---------------------------------------------------------------- consulta
def consultar(cnpj: str, nome: str, a: date, b: date) -> tuple[list[dict], list[tuple[date, date]], str | None]:
    """Contratos do órgão entre a e b. Se a API recusar o intervalo (400/422), divide ao meio e tenta de novo.

    Devolve (linhas, períodos concluídos, erro).
    """
    global janela_atual
    params = {"dataInicial": a.strftime("%Y%m%d"), "dataFinal": b.strftime("%Y%m%d"), "cnpjOrgao": cnpj,
              "tamanhoPagina": C.TAMANHO_PAGINA_CONTRATOS}
    try:
        linhas = [_linha_contrato(it) for it in paginar(URL, params, f"{nome[:45]} {a:%d/%m/%y} a {b:%d/%m/%y}")]
        return [l for l in linhas if l["orgao_cnpj"] == cnpj], [(a, b)], None
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        dias = (b - a).days
        if status in (400, 422) and dias > 31:
            meio = a + timedelta(days=dias // 2)
            janela_atual = min(janela_atual, dias // 2 + 1)
            log.info("   API recusou %d dias para %s; dividindo o período (janela agora %d dias).", dias + 1, nome[:45], janela_atual)
            l1, p1, e1 = consultar(cnpj, nome, a, meio)
            l2, p2, e2 = consultar(cnpj, nome, meio + timedelta(days=1), b)
            return l1 + l2, p1 + p2, e1 or e2
        return [], [], f"{e}"
    except (requests.RequestException, ValueError) as e:
        return [], [], f"{e}"


def consultar_orgao(cnpj: str, nome: str, pendentes: list[tuple[date, date]]):
    linhas, feitos, erros = [], [], []
    for a, b in pendentes:
        l, p, e = consultar(cnpj, nome, a, b)
        linhas += l
        feitos += p
        if e:
            erros.append(e)
    return linhas, feitos, erros


# ---------------------------------------------------------------- coleta
def orgaos_municipais(so_prefeituras: bool) -> pd.DataFrame:
    if not C.BANCO.exists():
        sys.exit(f"Não encontrei {C.BANCO}. Rode antes a coleta principal (python main.py tudo), que monta a lista de órgãos.")
    with sqlite3.connect(C.BANCO) as m:
        org = pd.read_sql_query("SELECT orgao_cnpj, orgao, esfera, municipio FROM orgaos WHERE uf = ?", m, params=(C.UF,))
    org = org[tratamento.na_esfera(org)]
    org["orgao"] = org["orgao"].fillna(org["orgao_cnpj"])
    prefeitura = org["orgao"].str.contains(r"^MUNICIPIO D|PREFEITURA", case=False, regex=True)
    if so_prefeituras:
        org = org[prefeitura]
    # prefeituras primeiro: o mapa já fica útil mesmo se a execução for interrompida
    return org.assign(_p=~prefeitura).sort_values(["_p", "orgao"]).drop(columns="_p").reset_index(drop=True)


def coletar(con, desde: date, raizes: list[str], so_prefeituras: bool, limite: int | None) -> None:
    hoje = date.today()
    org = orgaos_municipais(so_prefeituras)
    if limite:
        org = org.head(limite)
    municipios = ibge.municipios()
    alvo = {}
    for cnpj, nome in org[["orgao_cnpj", "orgao"]].values:
        pend = [j for a, b in lacunas(desde, hoje, periodos_feitos(con, cnpj)) for j in janelas(a, b, janela_atual)]
        if pend:
            alvo[cnpj] = (nome, pend)
    log.info("Fornecedor: CNPJ raiz %s. Período: %s a %s. Órgãos: %d na lista, %d com períodos a consultar, %d por vez.",
             ", ".join(raizes), desde, hoje, len(org), len(alvo), C.CONSULTAS_PARALELAS)
    if not alvo:
        log.info("Tudo já consultado; nada a baixar.")
        return

    t0, n, achados, total = time.monotonic(), len(alvo), 0, 0
    ex = ThreadPoolExecutor(max_workers=max(1, C.CONSULTAS_PARALELAS))
    try:
        futuros = {ex.submit(consultar_orgao, cnpj, nome, pend): cnpj for cnpj, (nome, pend) in alvo.items()}
        for i, futuro in enumerate(as_completed(futuros), 1):
            cnpj = futuros[futuro]
            nome = alvo[cnpj][0]
            linhas, feitos, erros = futuro.result()
            df = pd.DataFrame(linhas, columns=COLUNAS_CONTRATOS).drop_duplicates("id_pncp")
            df = tratamento.marcar_ti(tratamento.padronizar_municipios(df, municipios))
            do_fornecedor = df["fornecedor_cnpj"].fillna("").str.startswith(tuple(raizes)) if len(df) else pd.Series(dtype=bool)
            # guarda os contratos do fornecedor e os demais de TI (para mostrar quem atende onde ele não está)
            banco.gravar(con, "contratos", df[do_fornecedor | (df["ti"] == 1)] if len(df) else df)
            with con:
                con.executemany("INSERT OR REPLACE INTO consultas VALUES (?, ?, ?, ?, datetime('now', 'localtime'))",
                                [(cnpj, str(a), str(b), len(df)) for a, b in feitos])
            n_f = int(do_fornecedor.sum()) if len(df) else 0
            achados += n_f
            total += len(df)
            restante = (time.monotonic() - t0) / i * (n - i)
            log.info("[%d/%d] %s: %d contratos, %d do fornecedor%s | achados %d | faltam ~%s", i, n, nome[:55], len(df), n_f,
                     f" | FALHOU ({erros[0][:80]}); rode de novo para completar" if erros else "", achados, _tempo(restante))
    except KeyboardInterrupt:
        log.warning("Interrompido. O que já foi consultado está salvo; rode de novo para continuar.")
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown()
    log.info("Coleta concluída: %d contratos lidos, %d do fornecedor.", total, achados)


# ---------------------------------------------------------------- exportação
link_pncp = link_contrato   # nome antigo


def exportar(con, raizes: list[str], nome_fornecedor: str, desde: date) -> None:
    hoje = date.today().isoformat()
    ct = banco.ler(con, "contratos")
    if ct.empty:
        log.warning("O cache está vazio; nada a exportar.")
        return
    ct["link"] = ct["id_pncp"].map(link_contrato)
    ct["tipo_orgao"] = ct["orgao"].map(tratamento.tipo_orgao)
    ct["vigente"] = ct["fim_vigencia"].fillna("") >= hoje
    do_f = ct["fornecedor_cnpj"].fillna("").str.startswith(tuple(raizes))
    cols = ["codigo_ibge", "municipio", "orgao", "tipo_orgao", "fornecedor", "fornecedor_cnpj", "objeto", "valor_global",
            "data_assinatura", "inicio_vigencia", "fim_vigencia", "vigente", "link"]
    f = ct[do_f][cols].sort_values(["municipio", "fim_vigencia"], ascending=[True, False])
    outros = ct[~do_f & (ct["ti"] == 1) & ct["vigente"]][cols]

    por_mun = (f.groupby(["codigo_ibge", "municipio"], dropna=False)
                .agg(contratos=("objeto", "size"), vigentes=("vigente", "sum"), valor_total=("valor_global", "sum"),
                     fim_mais_distante=("fim_vigencia", "max"), orgaos=("orgao", lambda s: "; ".join(sorted(set(s.dropna())))))
                .reset_index().sort_values("valor_total", ascending=False))

    slug = re.sub(r"[^a-z0-9]+", "_", tratamento.sem_acento(nome_fornecedor)).strip("_")
    C.SAIDA.mkdir(parents=True, exist_ok=True)
    f.to_csv(C.SAIDA / f"fornecedor_{slug}_contratos.csv", index=False, encoding="utf-8-sig")
    por_mun.to_csv(C.SAIDA / f"fornecedor_{slug}_municipios.csv", index=False, encoding="utf-8-sig")

    total_org = len(orgaos_municipais(False))
    consultados = con.execute("SELECT COUNT(DISTINCT orgao_cnpj) FROM consultas").fetchone()[0]
    registro = lambda df: json.loads(df.to_json(orient="records", force_ascii=False))
    dados = {"fornecedor": nome_fornecedor, "cnpj_raiz": ", ".join(raizes), "raizes": raizes, "desde": str(desde), "gerado_em": hoje,
             "orgaos_consultados": consultados, "orgaos_total": total_org,
             "contratos": registro(f), "outros": registro(outros)}
    C.DASHBOARD.mkdir(parents=True, exist_ok=True)
    (C.DASHBOARD / "dados_fornecedor.js").write_text(
        f"window.FORN = {json.dumps(dados, ensure_ascii=False)};\n", encoding="utf-8")
    log.info("%s: %d contratos em %d municípios (%d com contrato vigente). Arquivos em %s; mapa em %s",
             nome_fornecedor, len(f), por_mun["codigo_ibge"].nunique(), int((por_mun["vigentes"] > 0).sum()),
             C.SAIDA, C.DASHBOARD / "mapa_fornecedor.html")


# ---------------------------------------------------------------- linha de comando
def main(argv=None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    C.ARQUIVO_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(C.RAIZ / "data" / "fornecedor.log", mode="w", encoding="utf-8")])
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cnpj", nargs="+", default=C.FORNECEDOR_CNPJ_RAIZES,
                   help="CNPJ (ou só os 8 primeiros dígitos) do fornecedor; vários = grupo econômico")
    p.add_argument("--nome", default=C.FORNECEDOR_NOME)
    p.add_argument("--desde", default=C.FORNECEDOR_DESDE, help="AAAA-MM-DD")
    p.add_argument("--so-prefeituras", action="store_true", help="só órgãos 'Município de ...' / prefeituras")
    p.add_argument("--limite", type=int, help="consultar só os N primeiros órgãos (teste)")
    p.add_argument("--exportar", action="store_true", help="não consulta a API; só refaz CSV e mapa a partir do cache")
    a = p.parse_args(argv)
    raizes, desde = [re.sub(r"\D", "", c)[:8] for c in a.cnpj], date.fromisoformat(a.desde)
    con = abrir_cache()
    try:
        if not a.exportar:
            coletar(con, desde, raizes, a.so_prefeituras, a.limite)
        exportar(con, raizes, a.nome, desde)
    finally:
        con.close()


if __name__ == "__main__":
    main()
