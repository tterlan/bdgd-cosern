# BDGD Cosern – recorte para o cadastro da Guarani Solar

Rotina automática que acompanha a **Base de Dados Geográfica da Distribuidora (BDGD)** publicada pela ANEEL para a Neoenergia Cosern. Quando sai uma versão nova, a rotina gera um pacote leve com a rede da região de atuação da Guarani Solar.

## O que a rotina faz
- **Quando roda:** todo dia 5 de cada mês, pelo GitHub Actions (`.github/workflows/bdgd.yml`). Também dá para rodar à mão em **Actions → BDGD Cosern → Run workflow**.
- **Como sabe que há base nova:** procura no ArcGIS da ANEEL o File Geodatabase mais recente da Cosern e compara com o `estado.json`.
- **Se for nova:**
  1. baixa o arquivo, de cerca de 470 MB;
  2. recorta os municípios do `config.json` (transformadores e GD dos municípios; rede de MT inteira dos alimentadores que os atendem);
  3. gera `pacote.zip` e `meta.json` e publica numa **release**.
- **O cadastro** consulta `releases/latest/download/meta.json` e baixa o `pacote.zip` quando a referência muda.

## O que vai no pacote
| Arquivo | Conteúdo |
|---|---|
| `meta.json` | Referência da BDGD, item da ANEEL, data do processamento, municípios, contagens e regra de limpeza da GD |
| `trafos.json` | Transformadores: posição, kVA, GD ativa ligada (kW), nº de GD, consumidores BT, demanda média, alimentador, município |
| `alimentadores.json` | Alimentadores: nome, subestação, energia anual, GD ligada, transformadores e kVA |
| `subestacoes.json` | Subestações: código, nome e posição |
| `mt.ndjson` | Trechos de média tensão simplificados, um por linha: `[alimentador, fases, condutor (ex.: AL4AWG), [[lon, lat], ...]]`. Linha a linha para o cadastro importar sem estourar a memória da hospedagem |

## Limpeza da GD
O campo de potência da BDGD (`POT_INST`) mistura unidades. Regras aplicadas:
- **BT com valor até 1:** é MW, multiplica por 1000.
- **BT acima de 75 kW:** descartado.
- **MT acima de 5.000 kW:** descartado.
- Só unidades ativas entram.

## Trocar a região
Edite `municipios` em `config.json` (código IBGE: nome) e rode a rotina com **forcar**.

## Rodar localmente
```
sudo apt-get install gdal-bin
python3 processa.py --forcar            # baixa da ANEEL
python3 processa.py --forcar --gdb arquivo.gdb   # usa um .gdb já baixado
```

Fonte: ANEEL, Base de Dados Geográfica da Distribuidora (BDGD), licença Open Data Commons ODbL. É uma foto anual com finalidade regulatória: não substitui a consulta formal à distribuidora.
