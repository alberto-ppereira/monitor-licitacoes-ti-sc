"""Tabelas para o Looker Studio (saida/looker/): CSV UTF-8 sem BOM, vírgula como separador, ponto decimal,
datas AAAA-MM-DD e uma coluna `local` ("Município, SC, Brasil") pronta para o tipo geográfico Cidade.

Arquivos (uma fonte de dados cada no Looker):
  oportunidades.csv   licitações de TI com disputa a partir de hoje, com nota e prazo
  movimentacoes.csv   licitações publicadas e contratos assinados nos últimos dias
  contratos.csv       contratos de TI (vigentes e encerrados) com situação e faixa de vencimento
  municipios.csv      uma linha por município de SC: população, contratos vigentes, fornecedor principal,
                      próximo vencimento, plataforma de publicação e oportunidades abertas
  fornecedores.csv    uma linha por fornecedor: municípios atendidos, contratos e valor vigentes
"""
from datetime import date
from pathlib import Path

import pandas as pd

import config as C
from src.exportacao import nome_fornecedor_padrao
from src.pncp import link_contrato
from src.tratamento import dias_ate, tipo_orgao


def _local(municipio: pd.Series) -> pd.Series:
    return municipio.fillna("").astype(str) + f", {C.UF}, Brasil"


def _faixa_vencimento(dias: pd.Series) -> pd.Series:
    rotulos = pd.cut(dias, [-10**6, -1, 90, 180, 365, 10**6],
                     labels=["Encerrado", "Até 90 dias", "91 a 180 dias", "181 a 365 dias", "Mais de 1 ano"])
    return rotulos.astype(str).replace("nan", "Sem data")


def _faixa_prazo(dias: pd.Series) -> pd.Series:
    rotulos = pd.cut(dias, [-1, 7, 15, 30, 10**6], labels=["Até 7 dias", "8 a 15 dias", "16 a 30 dias", "Mais de 30 dias"])
    return rotulos.astype(str).replace("nan", "Sem data")


def contratos(ct: pd.DataFrame, hoje: date) -> pd.DataFrame:
    if ct.empty:
        return pd.DataFrame()
    d = dias_ate(ct["fim_vigencia"], hoje)
    out = pd.DataFrame({
        "id_pncp": ct["id_pncp"], "codigo_ibge": ct["codigo_ibge"], "municipio": ct["municipio"], "local": _local(ct["municipio"]),
        "orgao": ct["orgao"], "tipo_orgao": ct["orgao"].map(tipo_orgao),
        "fornecedor": nome_fornecedor_padrao(ct), "fornecedor_cnpj": ct["fornecedor_cnpj"],
        "fornecedor_cnpj_raiz": ct["fornecedor_cnpj"].fillna("").str[:8],
        "objeto": ct["objeto"], "categoria": ct["categoria"], "valor": ct["valor_global"],
        "data_assinatura": ct["data_assinatura"], "ano_assinatura": ct["data_assinatura"].str[:4],
        "inicio_vigencia": ct["inicio_vigencia"], "fim_vigencia": ct["fim_vigencia"],
        "fim_vigencia_original": ct.get("fim_vigencia_original", ct["fim_vigencia"]),
        "prorrogado": ct.get("prorrogado", pd.Series(False, index=ct.index)).map({True: "Sim", False: "Não"}),
        "mensalidade": ct.get("mensalidade"), "base_mensalidade": ct.get("base_mensalidade"),
        "situacao": (d >= 0).map({True: "Vigente", False: "Encerrado"}).where(d.notna(), "Sem data"),
        "dias_para_vencer": d, "faixa_vencimento": _faixa_vencimento(d),
        "plataforma": ct["plataforma"], "link": ct["id_pncp"].map(link_contrato),
    })
    return out.sort_values(["municipio", "fim_vigencia"]).reset_index(drop=True)


def oportunidades(op: pd.DataFrame, hoje: date) -> pd.DataFrame:
    if op.empty:
        return pd.DataFrame()
    d = dias_ate(op["data_abertura"], hoje)
    return op.assign(local=_local(op["municipio"]), dias_ate_disputa=d, faixa_prazo=_faixa_prazo(d))


def movimentacoes(mv: pd.DataFrame) -> pd.DataFrame:
    return mv.assign(local=_local(mv["municipio"])) if not mv.empty else mv


def municipios(mun: pd.DataFrame, ct: pd.DataFrame, op: pd.DataFrame, contratacoes_todas: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por município: o que o mapa do Looker precisa, já agregado."""
    base = mun[["codigo_ibge", "municipio", "populacao"]].copy() if not mun.empty else pd.DataFrame(
        columns=["codigo_ibge", "municipio", "populacao"])
    vig = ct[ct["situacao"] == "Vigente"] if not ct.empty else ct
    if not vig.empty:
        g = vig.groupby("codigo_ibge")
        agg = pd.DataFrame({
            "contratos_vigentes": g.size(), "valor_vigente": g["valor"].sum(),
            "mensalidade_vigente": g["mensalidade"].sum(min_count=1) if "mensalidade" in vig else None,
            "entidades_com_contrato": g["orgao"].nunique(),
            "fornecedores_vigentes": g["fornecedor"].nunique(), "proximo_vencimento": g["fim_vigencia"].min(),
            "fornecedor_principal": g.apply(lambda x: x.groupby("fornecedor")["valor"].sum().idxmax()),
            "contratos_de_consorcio": g["tipo_orgao"].apply(lambda s: int((s == "Consórcio").sum())),
        }).reset_index()
        base = base.merge(agg, on="codigo_ibge", how="left")
    else:
        base = base.assign(contratos_vigentes=0, valor_vigente=0.0, fornecedores_vigentes=0, proximo_vencimento=None,
                           fornecedor_principal=None, contratos_de_consorcio=0)
    if not ct.empty:
        base = base.merge(ct.groupby("codigo_ibge").size().rename("contratos_total").reset_index(), on="codigo_ibge", how="left")
    if not op.empty:
        o = op.groupby("codigo_ibge").agg(oportunidades_abertas=("objeto", "size"), valor_oportunidades=("valor_estimado", "sum"))
        base = base.merge(o.reset_index(), on="codigo_ibge", how="left")
    if not contratacoes_todas.empty and "plataforma" in contratacoes_todas:
        p = contratacoes_todas.dropna(subset=["plataforma"]).groupby("codigo_ibge")["plataforma"]
        base = base.merge(p.agg(lambda s: s.value_counts().index[0]).rename("plataforma_principal").reset_index(),
                          on="codigo_ibge", how="left")
    for col in ["contratos_vigentes", "fornecedores_vigentes", "contratos_de_consorcio", "contratos_total", "oportunidades_abertas"]:
        if col in base:
            base[col] = base[col].fillna(0).astype(int)
    for col in ["valor_vigente", "valor_oportunidades", "mensalidade_vigente"]:
        if col in base:
            base[col] = base[col].fillna(0.0)
    base["situacao_ti"] = base["contratos_vigentes"].gt(0).map({True: "Com contrato de TI vigente",
                                                               False: "Sem contrato de TI vigente no PNCP"})
    base["local"] = _local(base["municipio"])
    return base.sort_values("populacao", ascending=False, na_position="last").reset_index(drop=True)


def fornecedores(ct: pd.DataFrame) -> pd.DataFrame:
    if ct.empty:
        return pd.DataFrame()
    vig = ct[ct["situacao"] == "Vigente"]
    tot = ct.groupby(["fornecedor_cnpj_raiz", "fornecedor"]).agg(contratos_total=("id_pncp", "size"),
                                                                 municipios_total=("codigo_ibge", "nunique"))
    v = vig.groupby(["fornecedor_cnpj_raiz", "fornecedor"]).agg(
        contratos_vigentes=("id_pncp", "size"), municipios_vigentes=("codigo_ibge", "nunique"),
        valor_vigente=("valor", "sum"), mensalidade_vigente=("mensalidade", "sum"), proximo_vencimento=("fim_vigencia", "min"))
    out = tot.join(v, how="left").reset_index()
    for col in ["contratos_vigentes", "municipios_vigentes"]:
        out[col] = out[col].fillna(0).astype(int)
    out["valor_vigente"] = out["valor_vigente"].fillna(0.0)
    out["mensalidade_vigente"] = out["mensalidade_vigente"].fillna(0.0)
    return out.sort_values(["municipios_vigentes", "valor_vigente"], ascending=False).reset_index(drop=True)


def exportar(op: pd.DataFrame, mv: pd.DataFrame, ct_ti: pd.DataFrame, mun: pd.DataFrame,
             contratacoes_todas: pd.DataFrame, pasta: Path, hoje: date) -> dict[str, int]:
    pasta.mkdir(parents=True, exist_ok=True)
    ct = contratos(ct_ti, hoje)
    opl = oportunidades(op, hoje)
    
    # Gera as 5 tabelas
    tabelas = {"oportunidades": opl, "movimentacoes": movimentacoes(mv), "contratos": ct,
               "municipios": municipios(mun, ct, opl, contratacoes_todas), "fornecedores": fornecedores(ct)}
               
    # Adiciona a data de atualização em TODAS as 5 tabelas (inclusive fornecedores e municipios)
    for nome, df in tabelas.items():
        if not df.empty:
            df["data_atualizacao"] = hoje.isoformat()
            
    # Salva os CSVs
    for nome, df in tabelas.items():
        df.to_csv(pasta / f"{nome}.csv", index=False, encoding="utf-8")
        
    return {k: len(v) for k, v in tabelas.items()}
