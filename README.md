# Monitor de licitações de TI em prefeituras de SC

Coleta, integra e exporta dados públicos para mapear oportunidades de venda de sistemas de gestão
para prefeituras de Santa Catarina: licitações publicadas e em aberto, contratos vigentes e seus
fornecedores, com uma nota de prioridade por oportunidade. Inclui um script separado que mostra onde
um fornecedor (por padrão, a IPM Sistemas) tem contratos no estado.

## Fontes

| Fonte | O que vem dela |
|---|---|
| PNCP, API de consulta (`/v1/contratacoes/publicacao`, `/v1/contratacoes/proposta`, `/v1/contratos`) | Licitações, prazos, valores estimados, contratos, fornecedores, vigências e a plataforma que publicou |
| PNCP, exportação "Contratos/Empenhos" do portal (CSV, opcional) | Órgãos que assinam contratos, para completar a lista de órgãos |
| IBGE, API de localidades | Lista oficial de municípios e códigos |
| IBGE, API de agregados (SIDRA 6579) | População estimada, usada na pontuação |
| IBGE, API de malhas | Desenho dos municípios no mapa (carregado pelo navegador) |

## Fluxo

**coleta (PNCP e IBGE) → tratamento (esfera, filtro de TI, categorias, municípios padronizados) → banco SQLite → exportação (CSV, Excel, dados.js do painel)**

## Regras de negócio

Conferidas contra exportações do portal do PNCP com 207 contratos da IPM e 88 da Betha.

| Regra | Onde | Por quê |
|---|---|---|
| Só órgãos municipais de SC, mais consórcios intermunicipais | `ESFERAS`, `INCLUIR_CONSORCIOS` | O PNCP marca consórcios com esfera "N"; eles compram para os municípios (ex.: CINCATARINA) |
| É TI o objeto com termo de TI (sem acento, início de palavra) ou contrato com categoria "Informática (TIC)" | `TERMOS_TI`, `CATEGORIAS_PROCESSO_TI` | Termos conferidos no texto real dos contratos: "sistema web integrado", "sistema de informática", "SIAFIC", "Atende.Net"... (206 de 207 da IPM e 88 de 88 da Betha) |
| Exclusões por frase, não por palavra solta | `TERMOS_EXCLUIR` | "mobiliário" sozinho derrubava "cadastro mobiliário" (ISS), presente em quase todo ERP |
| O banco guarda tudo, com a marcação `ti` | coluna `ti` | Ajustar os termos e rodar `reclassificar` corrige sem baixar de novo |
| Lista ampla de órgãos | `python main.py orgaos` | Câmaras, fundos e fundações assinam contratos de licitações "multientidade" da prefeitura sem publicar licitação própria; só 55% dos órgãos com contrato da IPM estavam na lista montada pelas licitações |
| Licitação revogada, anulada, suspensa, deserta ou fracassada não é oportunidade | `SITUACOES_FORA` | Continuam no banco, saem da aba de oportunidades |
| Tipo de órgão pelo nome (prefeitura, câmara, fundo, fundação, autarquia, consórcio) | `tratamento.tipo_orgao` | Contratos de consórcio ficam no município sede: aparecem na lista, mas não pintam o mapa |
| Plataforma que publicou no PNCP | coluna `plataforma` | Em geral é o ERP do órgão: 117 de 123 contratos da IPM em SC foram publicados pela plataforma "IPM Sistemas", 84 de 88 da Betha pela "Betha Sistemas" |
| Fornecedor pela raiz do CNPJ (8 dígitos) | `FORNECEDOR_CNPJ_RAIZES` | Inclui filiais; várias raízes cobrem o grupo econômico (ex.: IPM Fintech) |

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
python main.py orgaos               # uma vez: lista ampla de órgãos (licitações dos últimos 365 dias)
python main.py orgaos --csv contratos-ipm.csv contratos-betha.csv   # opcional: órgãos das exportações do portal
python main.py tudo --dias 30       # primeira coleta: últimos 30 dias, e exporta
python main.py tudo                 # nas próximas vezes: incremental
python main.py reclassificar        # depois de ajustar os termos de TI no config
```

Saídas em `saida/`: `oportunidades.csv`, `movimentacoes.csv`, `contratos.csv`,
`monitor_licitacoes.xlsx` e `dados.js`. O `dados.js` também é copiado para `dashboard/`; abra
`dashboard/dashboard.html` no navegador para ver o painel.

Se a API mudar algum nome de campo, o ajuste é feito só em `src/pncp.py` (`_linha_contratacao` e
`_linha_contrato`); o `diagnostico` mostra os campos atuais.

### Execução diária no Windows

Agendador de Tarefas → Criar Tarefa Básica → diária → Iniciar um programa:
programa `.venv\Scripts\python.exe`, argumentos `main.py tudo`, iniciar em: a pasta do projeto.

## Fornecedor no mapa (`fornecedor_no_mapa.py`)

A API do PNCP não filtra contratos por fornecedor, então o script percorre o histórico de contratos de
cada órgão da lista e separa os do fornecedor pela raiz do CNPJ.

```bash
python fornecedor_no_mapa.py --limite 20                         # teste rápido com 20 órgãos
python fornecedor_no_mapa.py                                     # IPM Sistemas (demorado na primeira vez; pode interromper e continuar)
python fornecedor_no_mapa.py --cnpj 00456865 --nome "Betha Sistemas"
python fornecedor_no_mapa.py --cnpj 01258027 44104389 --nome "Grupo IPM"
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
- A vigência é a do registro do contrato: prorrogações por termo aditivo podem não estar refletidas. A API de consulta não traz aditivos em lote.
- O filtro por termos ainda tem falsos positivos em contratos mistos (ex.: serviço de medicina do trabalho que inclui um sistema). Revisar amostras e ajustar o config.
- Próximos passos: mapa de "ERP por município" a partir da coluna `plataforma`; alerta para contratações emergenciais de sistema de gestão (sinal de troca de fornecedor); Planos de Contratações Anuais (PNCP); diários oficiais (Querido Diário); exportação para CRM.
