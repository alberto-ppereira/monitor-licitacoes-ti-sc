# Monitor de licitações de TI em prefeituras de SC

Coleta, integra e exporta dados públicos para mapear oportunidades de venda de sistemas de gestão
para prefeituras de Santa Catarina: licitações publicadas e em aberto, contratos vigentes e seus
fornecedores, com uma nota de prioridade por oportunidade. Inclui um script separado que mostra onde
um fornecedor de software tem contratos no estado.

## Fontes

| Fonte | O que vem dela |
|---|---|
| PNCP, API de consulta (`/v1/contratacoes/publicacao`, `/v1/contratacoes/proposta`, `/v1/contratos`) | Licitações, prazos, valores estimados, contratos, fornecedores, vigências e a plataforma que publicou |
| PNCP, busca textual do portal (`/api/search/`, não documentada) | Carga histórica de contratos por palavra-chave, sem limite de exportação (`python main.py buscar`) |
| PNCP, exportação "Contratos/Empenhos" do portal (CSV) | Carga histórica de contratos e dos órgãos que os assinam (milhares de contratos em segundos; a API exige uma consulta por órgão) |
| IBGE, API de localidades | Lista oficial de municípios e códigos |
| IBGE, API de agregados (SIDRA 6579) | População estimada, usada na pontuação |
| IBGE, API de malhas | Desenho dos municípios no mapa (carregado pelo navegador) |

## Fluxo

**coleta (PNCP e IBGE) → tratamento (esfera, filtro de TI, categorias, municípios padronizados) → banco SQLite → exportação (CSV, Excel, dados.js do painel)**

## Regras de negócio

Conferidas contra exportações do portal do PNCP com contratos de dois grandes fornecedores de ERP municipal em SC (207 e 88 contratos).

| Regra | Onde | Por quê |
|---|---|---|
| Só órgãos municipais de SC, mais consórcios e agências intermunicipais | `ESFERAS`, `INCLUIR_CONSORCIOS` | O PNCP marca consórcios e agências intermunicipais com esfera "N"; eles compram para os municípios |
| É TI o objeto com termo de TI (sem acento, início de palavra) ou contrato com categoria "Informática (TIC)" | `TERMOS_TI`, `CATEGORIAS_PROCESSO_TI` | Termos conferidos no texto real dos contratos: "sistema web integrado", "sistema de informática", "SIAFIC", "Atende.Net"... (206 de 207 e 88 de 88 nos dois fornecedores conferidos) |
| Exclusões por frase, não por palavra solta | `TERMOS_EXCLUIR` | "mobiliário" sozinho derrubava "cadastro mobiliário" (ISS), presente em quase todo ERP |
| Termos fortes vencem exclusões | `TERMOS_TI_FORTES` | "licença de uso", "sistema de informática", "integrado de gestão"... são software mesmo com "bens móveis", "digitalização" ou "controle de acesso" no texto (+64 contratos de TI em 3.800, nenhum perdido) |
| O banco guarda tudo, com a marcação `ti` | coluna `ti` | Ajustar os termos e rodar `reclassificar` corrige sem baixar de novo |
| Lista ampla de órgãos | `python main.py orgaos` | Câmaras, fundos e fundações assinam contratos de licitações "multientidade" da prefeitura sem publicar licitação própria; só 55% dos órgãos com contrato de um grande fornecedor estavam na lista montada pelas licitações |
| Licitação revogada, anulada, suspensa, deserta ou fracassada não é oportunidade | `SITUACOES_FORA` | Continuam no banco, saem da aba de oportunidades |
| Tipo de órgão pelo nome (prefeitura, câmara, fundo, fundação, autarquia, consórcio) | `tratamento.tipo_orgao` | Contratos de consórcio contam no município sede (onde o PNCP os registra) e aparecem marcados como consórcio |
| Vigência prorrogada por termo aditivo | `python main.py aditivos` | Software é serviço contínuo, renovado ano a ano por aditivo; a busca e a API de consulta só trazem a vigência original (ex.: contratos vencidos em 03/2026 e prorrogados até 03/2027). Rescisão encerra o contrato na data do termo |
| Mensalidade estimada por contrato e por município | `banco.mensalidade` | Prorrogado: valor dos termos de prorrogação ÷ meses prorrogados (só o serviço contínuo). Sem aditivo: valor global ÷ meses de vigência (inclui implantação, tende a sair acima). Vigência fora de 6 a 60 meses fica sem estimativa. O município soma os contratos vigentes de todas as entidades |
| Plataforma que publicou no PNCP | coluna `plataforma` | Em geral é o ERP do órgão: 117 de 123 contratos de um fornecedor foram publicados pela plataforma do próprio fornecedor; no outro, 84 de 88 |
| Fornecedor pela raiz do CNPJ (8 dígitos) | `FORNECEDOR_CNPJ_RAIZES` | Inclui filiais; várias raízes cobrem o grupo econômico |

## Robustez da coleta

- Janelas de datas, todas as páginas, pausa entre chamadas e até 6 novas tentativas com espera crescente em erro de rede ou sobrecarga. O erro 400, que o PNCP às vezes devolve por instabilidade, é repetido até 3 vezes.
- Consultas em paralelo (`CONSULTAS_PARALELAS`), com a pausa mínima valendo para o conjunto.
- Falha numa consulta de licitações: a data da última coleta não avança e a próxima execução repete o período. Falha nos contratos de um órgão: ele é repassado no fim da etapa e, se falhar de novo, só ele fica pendente.
- O banco grava pela chave do PNCP: rodar de novo atualiza, sem duplicar.
- Incremental: propostas em aberto uma vez por dia; cada órgão guarda até que data seus contratos foram consultados (`contratos_ate`) e só são reconsultados os órgãos com licitação nova, os pendentes e, a cada 7 dias, todos.
- Log detalhado (cada página, cada órgão, tempo restante) na tela e em `data/coleta.log`.

## Como rodar

Requer Python 3.11 ou mais recente.

```bash
pip install -r requirements.txt
python -m tests.verificacoes        # verificações offline (APIs simuladas)
python main.py diagnostico          # mostra um registro bruto de cada endpoint
python main.py importar --csv "Downloads PNCP\contratos.csv"   # carga histórica rápida: órgãos e contratos exportados do portal
python main.py tudo --dias 7 --sem-contratos                   # licitações da última semana (minutos) e exporta
python main.py buscar                                          # carga histórica: contratos com "sistema" desde 2021 pela busca do portal
python main.py orgaos               # opcional: lista ampla de órgãos (licitações dos últimos 365 dias; demorado)
python main.py orgaos --csv exportacao1.csv exportacao2.csv   # opcional: órgãos das exportações do portal
python main.py tudo --dias 30       # primeira coleta: últimos 30 dias, e exporta
python main.py tudo                 # nas próximas vezes: incremental
python main.py aditivos             # termos aditivos: vigência prorrogada dos contratos de TI vencidos ou vencendo
python main.py reclassificar        # depois de ajustar os termos de TI no config
python main.py publicar             # publica as tabelas do Looker no BigQuery (o `tudo` já faz isso no fim)
```

O log de todas as execuções, com os erros, fica em `data/coleta.log` (ao passar de 5 MB vira `coleta.log.1`,
e são mantidos 5 arquivos antigos). Uma execução interrompida por erro termina com código 1.

Saídas em `saida/`: `oportunidades.csv`, `movimentacoes.csv`, `contratos.csv`,
`monitor_licitacoes.xlsx` e `dados.js`. O `dados.js` também é copiado para `dashboard/`; abra
`dashboard/dashboard.html` no navegador para ver o painel.

Se a API mudar algum nome de campo, o ajuste é feito só em `src/pncp.py` (`_linha_contratacao` e
`_linha_contrato`); o `diagnostico` mostra os campos atuais.

### Tabelas para o Looker Studio

A cada exportação, `saida/looker/` recebe cinco CSVs (UTF-8, vírgula, ponto decimal, datas AAAA-MM-DD), um por fonte de dados:

| Arquivo | Uma linha por | Destaques |
|---|---|---|
| `oportunidades.csv` | licitação de TI aberta | `nota`, `dias_ate_disputa`, `faixa_prazo`, `tipo_orgao`, `plataforma`, `link` |
| `movimentacoes.csv` | evento dos últimos dias | `data`, `tipo` (licitação publicada / contrato assinado) |
| `contratos.csv` | contrato de TI | `mensalidade`, `base_mensalidade`, `prorrogado`, `situacao` (vigente/encerrado), `dias_para_vencer`, `faixa_vencimento`, `fornecedor_cnpj_raiz`, `ano_assinatura` |
| `municipios.csv` | município de SC (295) | `mensalidade_vigente`, `entidades_com_contrato`, `populacao`, `contratos_vigentes`, `fornecedor_principal`, `proximo_vencimento`, `plataforma_principal`, `situacao_ti` |
| `fornecedores.csv` | fornecedor | `mensalidade_vigente`, `municipios_vigentes`, `contratos_vigentes`, `valor_vigente` |

Todas têm a coluna `local` ("Município, SC, Brasil"): no Looker, defina o tipo como **Geográfico → Cidade** para
o mapa. Defina `codigo_ibge` e os CNPJs como **Texto**.

### Publicação no BigQuery (fonte do Looker Studio)

`python main.py publicar` envia as cinco tabelas de `saida/looker/` para o BigQuery (substituindo as anteriores)
e grava a tabela `controle`, com a data e as contagens da publicação. Usa o sandbox sem custo do BigQuery
(as tabelas expiram após 60 dias sem atualização, o que a execução diária evita).

Configuração, uma vez:

1. No Google Cloud, crie um projeto, ative a API do BigQuery e crie o dataset (`BIGQUERY_PROJETO` e
   `BIGQUERY_DATASET` no `config.py`).
2. Crie uma conta de serviço com os papéis **BigQuery Data Editor** e **BigQuery Job User** e baixe a chave JSON.
3. Salve a chave em `credenciais/bigquery-key.json` (a pasta está no `.gitignore`), ou aponte a variável
   `GOOGLE_APPLICATION_CREDENTIALS` para ela.

Proteção: se alguma tabela tiver mais de 20% menos linhas que na publicação anterior
(`BIGQUERY_QUEDA_MAXIMA`), a publicação é cancelada, para que uma coleta incompleta não esvazie o painel.

### Execução diária no Windows

Agendador de Tarefas → Criar Tarefa Básica → diária → Iniciar um programa:
programa `.venv\Scripts\python.exe`, argumentos `main.py tudo`, iniciar em: a pasta do projeto.

## Fornecedor no mapa (`fornecedor_no_mapa.py`)

A API do PNCP não filtra contratos por fornecedor, então o script percorre o histórico de contratos de
cada órgão da lista e separa os do fornecedor pela raiz do CNPJ.

```bash
python fornecedor_no_mapa.py --limite 20                         # teste rápido com 20 órgãos
python fornecedor_no_mapa.py --cnpj 12345678 --nome "Fornecedor X"       # demorado na primeira vez; pode interromper e continuar
python fornecedor_no_mapa.py --cnpj 12345678 87654321 --nome "Grupo X"    # grupo econômico (várias raízes)
python fornecedor_no_mapa.py --exportar                          # só refaz o mapa com o que já foi baixado
python -m tests.verificacoes_fornecedor
```

Resultado: `dashboard/mapa_fornecedor.html` (quatro situações por município: fornecedor com contrato
vigente, só encerrados, outro fornecedor de TI vigente, nada encontrado) e `saida/fornecedor_*.csv`.
O cache fica em `data/historico_contratos.db`.

## Estrutura

| Arquivo | Papel |
|---|---|
| `main.py` | Linha de comando: diagnostico, orgaos, coletar, exportar, tudo, reclassificar |
| `config.py` | Todas as regras ajustáveis: UF, modalidades, esferas, termos de TI, situações, pesos da nota, fornecedor |
| `src/api.py` | Acesso HTTP com pausa, novas tentativas, paginação e estatísticas; seguro para consultas paralelas |
| `src/pncp.py` | Coleta no PNCP e conversão para tabelas planas |
| `src/ibge.py` | Municípios e população |
| `src/tratamento.py` | Esfera, tipo de órgão, situação, filtro de TI, categorias, municípios, pontuação |
| `src/banco.py` | SQLite com upsert por chave, migração de colunas e controle da coleta incremental |
| `src/exportacao.py` | Oportunidades, movimentações e contratos em CSV, Excel e dados.js |
| `dashboard/dashboard.html` | Painel com três abas, lê o dados.js |
| `fornecedor_no_mapa.py` | Onde um fornecedor tem contratos; gera o mapa_fornecedor.html |
| `dashboard/mapa_fornecedor.html` | Mapa do fornecedor, lê o dados_fornecedor.js |
| `tests/` | Verificações de ponta a ponta com APIs simuladas |

## Limitações e próximos passos

- Só aparece o que os órgãos publicaram no PNCP (obrigatório com a Lei 14.133; muitos municípios começaram em 2023/2024). Contratos antigos mantidos por aditivo podem faltar.
- A vigência considera os termos aditivos de prorrogação (`python main.py aditivos`), consultados um contrato por vez na API de integração do PNCP. Contratos ainda não consultados usam a vigência do registro original.
- O filtro por termos ainda tem falsos positivos em contratos mistos (ex.: serviço de medicina do trabalho que inclui um sistema). Revisar amostras e ajustar o config.
- Próximos passos: mapa de "ERP por município" a partir da coluna `plataforma`; alerta para contratações emergenciais de sistema de gestão (sinal de troca de fornecedor); Planos de Contratações Anuais (PNCP); diários oficiais (Querido Diário); exportação para CRM.
