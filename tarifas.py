#!/usr/bin/env python3
"""
Tarifas homologadas e bandeiras tarifárias da ANEEL (dados abertos) para o cadastro da Guarani Solar.

Gera saida/tarifas.json com, para cada distribuidora do config.json (chave sig_aneel):
- as tarifas de aplicação (TUSD e TE, por subgrupo, modalidade, classe, subclasse, detalhe e posto) com vigência
  a partir de ANO_INICIAL, e o componente TUSD Fio B de cada uma (para a Lei 14.300);
- o acionamento mensal das bandeiras (cor e adicional em R$/MWh).

Se o conteúdo não mudou desde a última publicação (tarifas_hash.txt), não marca como novo.
Uso: python3 tarifas.py [--forcar] [--saida pasta] [--cache pasta]   (precisa de pandas e pyarrow)
"""
import argparse, datetime, hashlib, json, os, sys, urllib.parse, urllib.request
import io

import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(AQUI, 'config.json'), encoding='utf-8'))
ANO_INICIAL = 2023  # faturas mais antigas que isso não interessam à conferência
VERSAO = 1
CKAN = 'https://dadosabertos.aneel.gov.br/api/3/action/package_show?id='


def log(*a):
    print(datetime.datetime.now().strftime('%H:%M:%S'), *a, flush=True)


def recursos(pacote):
    with urllib.request.urlopen(CKAN + urllib.parse.quote(pacote), timeout=60) as r:
        return {x['name']: x['url'] for x in json.load(r)['result']['resources']}


def baixa(url, cache):
    nome = os.path.join(cache, os.path.basename(urllib.parse.urlparse(url).path))
    if not os.path.exists(nome):
        log('baixando', os.path.basename(nome))
        with urllib.request.urlopen(url, timeout=900) as r, open(nome + '.tmp', 'wb') as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        os.replace(nome + '.tmp', nome)
    return nome


def data(v):
    return v.isoformat() if hasattr(v, 'isoformat') else str(v)[:10]


def num(v):
    return round(float(v), 6) if pd.notna(v) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--forcar', action='store_true')
    ap.add_argument('--saida', default=os.path.join(AQUI, 'saida'))
    ap.add_argument('--cache', default=os.path.join(AQUI, 'cache'))
    a = ap.parse_args()
    os.makedirs(a.saida, exist_ok=True)
    os.makedirs(a.cache, exist_ok=True)
    dists = {k: d for k, d in CFG['distribuidoras'].items() if d.get('sig_aneel')}
    sigs = {d['sig_aneel']: k for k, d in dists.items()}

    # tarifas homologadas
    rt = recursos('tarifas-distribuidoras-energia-eletrica')
    url = next(u for n, u in rt.items() if n.endswith('.parquet'))
    t = pd.read_parquet(baixa(url, a.cache))
    t = t[t.SigAgente.isin(sigs) & (t.DscBaseTarifaria == 'Tarifa de Aplicação')]
    t = t[pd.to_datetime(t.DatFimVigencia) >= pd.Timestamp(ANO_INICIAL, 1, 1)]
    chave = ['SigAgente', 'DatInicioVigencia', 'DscSubGrupo', 'DscModalidadeTarifaria', 'DscClasse', 'DscSubClasse', 'DscDetalhe',
             'NomPostoTarifario', 'DscUnidadeTerciaria', 'SigAgenteAcessante']
    t = t.drop_duplicates(subset=chave + ['VlrTUSD', 'VlrTE'])
    log(len(t), 'tarifas')

    # componente Fio B (TUSD_FioB) dos anos de interesse
    rc = recursos('componentes-tarifarias')
    fio = {}
    for ano in range(ANO_INICIAL - 1, datetime.date.today().year + 1):  # vigência de 2022 cobre o começo de 2023
        u = rc.get(f'componentes-tarifarias-{ano}.parquet')
        if not u:
            continue
        c = pd.read_parquet(baixa(u, a.cache), columns=['SigNomeAgente', 'DatInicioVigencia', 'DscBaseTarifaria', 'DscSubGrupoTarifario',
                                                       'DscModalidadeTarifaria', 'DscClasseConsumidor', 'DscSubClasseConsumidor',
                                                       'DscDetalheConsumidor', 'DscPostoTarifario', 'DscUnidade', 'SigNomeAgenteAcessante',
                                                       'DscComponenteTarifario', 'VlrComponenteTarifario'])
        c = c[c.SigNomeAgente.isin(sigs) & (c.DscBaseTarifaria == 'Tarifa de Aplicação') & (c.DscComponenteTarifario == 'TUSD_FioB')]
        for r in c.itertuples(index=False):
            un = 'kW' if 'kW' in r.DscUnidade and 'kWh' not in r.DscUnidade else 'MWh'
            k = (r.SigNomeAgente, data(r.DatInicioVigencia), r.DscSubGrupoTarifario, r.DscModalidadeTarifaria, r.DscClasseConsumidor,
                 r.DscSubClasseConsumidor, r.DscDetalheConsumidor, r.DscPostoTarifario, un, r.SigNomeAgenteAcessante)
            fio[k] = num(r.VlrComponenteTarifario)
    log(len(fio), 'componentes Fio B')

    saida = {k: {'nome': d['nome'], 'uf': d['uf'], 'sig_aneel': d['sig_aneel'], 'tarifas': []} for k, d in dists.items()}
    sem_fio = 0
    for r in t.sort_values(['SigAgente', 'DatInicioVigencia', 'DscSubGrupo', 'DscModalidadeTarifaria']).itertuples(index=False):
        k = (r.SigAgente, data(r.DatInicioVigencia), r.DscSubGrupo, r.DscModalidadeTarifaria, r.DscClasse, r.DscSubClasse, r.DscDetalhe,
             r.NomPostoTarifario, r.DscUnidadeTerciaria, r.SigAgenteAcessante)
        fb = fio.get(k)
        if fb is None:
            sem_fio += 1
        saida[sigs[r.SigAgente]]['tarifas'].append({
            'reh': r.DscREH, 'inicio': data(r.DatInicioVigencia), 'fim': data(r.DatFimVigencia), 'subgrupo': r.DscSubGrupo,
            'modalidade': r.DscModalidadeTarifaria, 'classe': r.DscClasse, 'subclasse': r.DscSubClasse, 'detalhe': r.DscDetalhe,
            'posto': r.NomPostoTarifario, 'unidade': r.DscUnidadeTerciaria, 'acessante': r.SigAgenteAcessante,
            'tusd': num(r.VlrTUSD), 'te': num(r.VlrTE), 'fio_b': fb})
    log('tarifas sem Fio B encontrado:', sem_fio)

    # bandeiras: acionamento mensal (cor e adicional em R$/MWh)
    rb = recursos('bandeiras-tarifarias')
    url = next(u for n, u in rb.items() if 'Acionamento' in n and u.lower().endswith('.csv'))
    bruto = urllib.request.urlopen(url, timeout=120).read()
    try:
        texto = bruto.decode('utf-8')
    except UnicodeDecodeError:
        texto = bruto.decode('latin-1')
    b = pd.read_csv(io.StringIO(texto), sep=';', dtype=str)
    bandeiras = []
    for r in b.itertuples(index=False):
        comp = str(r.DatCompetencia)[:7]
        if comp < f'{ANO_INICIAL - 1}-01':
            continue
        bandeiras.append({'competencia': comp, 'bandeira': str(r.NomBandeiraAcionada).strip(),
                          'rs_mwh': float(str(r.VlrAdicionalBandeira).replace('.', '').replace(',', '.') or 0)})
    bandeiras.sort(key=lambda x: x['competencia'])
    log(len(bandeiras), 'meses de bandeira; último', bandeiras[-1] if bandeiras else None)

    conteudo = {'versao': VERSAO, 'ano_inicial': ANO_INICIAL, 'distribuidoras': saida, 'bandeiras': bandeiras,
                'fonte': 'ANEEL – dados abertos: tarifas homologadas, componentes tarifárias (TUSD_FioB) e bandeiras tarifárias'}
    h = hashlib.sha256(json.dumps(conteudo, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    conteudo['hash'] = h
    conteudo['gerado_em'] = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    json.dump(conteudo, open(os.path.join(a.saida, 'tarifas.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    hash_f = os.path.join(AQUI, 'tarifas_hash.txt')
    antigo = open(hash_f).read().strip() if os.path.exists(hash_f) else ''
    novo = a.forcar or h != antigo
    if novo:
        open(hash_f, 'w').write(h + '\n')
    log('tarifas.json', round(os.path.getsize(os.path.join(a.saida, 'tarifas.json')) / 1e3), 'kB; hash', h, '(novo)' if novo else '(igual ao publicado)')
    if os.environ.get('GITHUB_OUTPUT'):
        open(os.environ['GITHUB_OUTPUT'], 'a').write(f"novo={'true' if novo else 'false'}\n")


if __name__ == '__main__':
    main()
