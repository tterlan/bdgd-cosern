# BDGD rede – dados da ANEEL para o cadastro da Guarani Solar

Rotina automática que acompanha a **Base de Dados Geográfica da Distribuidora (BDGD)** publicada pela ANEEL. Quando sai uma versão nova de uma das distribuidoras do `config.json`, a rotina gera um pacote leve com a rede dos municípios de interesse.

## Distribuidoras e municípios
Ficam em `config.json`, em `distribuidoras`. Cada uma tem:
- `nome` e `uf`;
- `titulo_arcgis`: começo do nome do arquivo na ANEEL. Exemplos: `Cosern_40` e `Enel_CE_39`; o número é o código da distribuidora na ANEEL;
- `municipios`: código IBGE e nome.

**Para acrescentar uma distribuidora ou um município:**
1. edite o `config.json`;
2. rode **Actions → BDGD rede → Run workflow**, informando a distribuidora e marcando **forcar**.

O cadastro descobre sozinho as distribuidoras novas.

## O que a rotina faz
- **Quando roda:** todo dia 5, pelo GitHub Actions (`.github/workflows/bdgd.yml`). Também dá para rodar à mão.
- **Para cada distribuidora:**
  1. procura no ArcGIS da ANEEL o File Geodatabase mais recente e compara com o `estado.json`;
  2. se for novo, baixa o arquivo (de 0,5 a 1 GB);
  3. recorta transformadores e GD dos municípios e a rede de MT inteira dos alimentadores que os atendem;
  4. atualiza a **release com o nome da distribuidora** (`cosern`, `enel-ce`…) com `pacote.zip` e `meta.json`.
- **Índice:** a release `indice` guarda `indice.json` com todas as distribuidoras publicadas, a referência e a data de processamento.
- **No cadastro:** ele lê `releases/download/indice/indice.json` e baixa `releases/download/<distribuidora>/pacote.zip` quando muda.
- **Falhas:** uma distribuidora com problema não impede as outras. O passo "Falhas" deixa a execução vermelha para avisar.

## O que vai no pacote (versão 2)
Arquivos e campos novos entram sem mudar a versão: o cadastro ignora o que não conhece (`gdmt.json` desde out/2026, lido pelo cadastro 1.38.2).
| Arquivo | Conteúdo |
|---|---|
| `meta.json` | Distribuidora, referência da BDGD, item da ANEEL, data do processamento, municípios, contagens e regra de limpeza da GD |
| `trafos.json` | Transformadores: posição, kVA, GD ativa ligada em BT (kW), nº de GD, consumidores BT, demanda média, alimentador e município; `gdmt`/`ngdmt`: minigeração em MT associada (transformador particular) |
| `gdmt.json` | Minigerações em média tensão (camada UGMT): posição do ponto de conexão, kW (pela relação de GD da ANEEL), alimentador, CEG e o transformador sem consumidor de BT mais próximo, até 150 m (`tr`, `tr_m`). A BDGD liga a minigeração ao alimentador, não a um transformador; sem isso, o transformador particular da usina aparecia com 0 kW de GD |
| `mercado.json` | Consumidores por município e classe e lista de prospecção (ver Mercado) |
| `alimentadores.json` | Alimentadores: nome, subestação, energia anual, GD ligada, transformadores e kVA |
| `subestacoes.json` | Subestações: código, nome e posição |
| `mt.ndjson` | Trechos de média tensão simplificados, um por linha: `[alimentador, fases, condutor (ex.: AL4AWG), [[lon, lat], ...]]`. Fica linha a linha para o cadastro importar sem estourar a memória da hospedagem |

## Tarifas e bandeiras (`tarifas.py`, workflow "Tarifas e bandeiras")
- **Quando roda:** todo dia. Baixa dos dados abertos da ANEEL as tarifas homologadas, o componente **TUSD Fio B** (componentes tarifárias) e o acionamento das **bandeiras**.
- **Filtro:** só as distribuidoras com `sig_aneel` no `config.json` (sigla da ANEEL: `COSERN`, `ENEL CE`), com vigência a partir de 2023.
- **Publicação:** só quando algo muda, na release `tarifas` (`tarifas.json`, cerca de 500 kB). O cadastro baixa esse arquivo uma vez por dia.
- **Conteúdo de cada tarifa:** resolução, vigência, subgrupo, modalidade, classe, subclasse, detalhe (ex.: SCEE), posto e unidade, com TUSD, TE e Fio B em R$/MWh ou R$/kW, sem tributos.
- **Conteúdo das bandeiras:** mês, cor e adicional em R$/MWh.

## Mercado
- **`mercado_gd.py`** roda junto com as tarifas, todo dia, mas só trabalha quando a ANEEL publica uma relação de GD nova (mensal).
  - Soma, por município das UFs do config.json: usinas, kW, classe, porte, modalidade, ano de conexão e últimos 12 meses.
  - Publica na release `mercado` (`mercado_gd.json`).
- **`processa.py`** (BDGD) também gera `mercado.json` dentro do `pacote.zip` de cada distribuidora:
  - consumidores por município e classe, com e sem GD;
  - lista de prospecção: Grupo A sem GD e BT sem GD com 12 MWh/ano ou mais, com localização, bairro, CEP, CNAE, classe, demanda e consumo anual.
  - A BDGD não traz nome nem documento.

## Potência da GD
A potência de cada GD vem da **relação de empreendimentos de GD da ANEEL** (dados abertos, atualizada todo mês), ligada à BDGD pelo código CEG (`GD.UF.xxx.xxx.xxx`).
- O campo `POT_INST` da BDGD não é confiável. A Enel CE, por exemplo, não grava kW nele: uma usina de 15 kW aparece como 1,258.
- **GD sem o CEG na relação:**
  - se a distribuidora tem `pot_inst_confiavel: true` no `config.json` (Cosern), usa o `POT_INST` limpo: BT até 1 é MW (×1000); BT acima de 75 kW e MT acima de 5.000 kW são descartados;
  - senão, a unidade é descartada.
- Só entram unidades ativas.
- A relação (cerca de 110 MB) é baixada uma vez por execução.

## Rodar localmente
```
sudo apt-get install gdal-bin
python3 processa.py                                  # todas, só as que mudaram
python3 processa.py --dist enel-ce --forcar          # uma, mesmo sem novidade
python3 processa.py --dist cosern --gdb arquivo.gdb --gd-zip gd.zip  # usa arquivos já baixados
pip install pandas pyarrow && python3 tarifas.py --forcar   # tarifas e bandeiras
```

**Fonte:** ANEEL, Base de Dados Geográfica da Distribuidora (BDGD), licença Open Data Commons ODbL. É uma foto anual com finalidade regulatória e não substitui a consulta formal à distribuidora.
