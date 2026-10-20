#!/usr/bin/env python3
"""
Mercado de GD por município, da relação de empreendimentos de geração distribuída da ANEEL (dados abertos, mensal).

Para cada UF das distribuidoras do config.json, soma por município: usinas e kW instalados, por classe de consumo, porte
(micro/mini), modalidade (própria UC, remota, compartilhada) e ano de conexão, e o que entrou nos últimos 12 meses.
Gera saida/mercado_gd.json. Só marca como novo quando a ANEEL publicou um arquivo diferente (mercado_hash.txt).
Uso: python3 mercado_gd.py [--forcar] [--zip arquivo.zip] [--saida pasta]
"""
import argparse, csv, datetime, hashlib, io, json, os, urllib.request, zipfile
from collections import defaultdict

AQUI = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(AQUI, 'config.json'), encoding='utf-8'))
CKAN = 'https://dadosabertos.aneel.gov.br/api/3/action/package_show?id=relacao-de-empreendimentos-de-geracao-distribuida'
VERSAO = 1
ANO_INICIAL = 2016


def log(*a):
    print(datetime.datetime.now().strftime('%H:%M:%S'), *a, flush=True)


def recurso_zip():
    with urllib.request.urlopen(CKAN, timeout=60) as r:
        for x in json.load(r)['result']['resources']:
            if x['url'].lower().endswith('empreendimento-geracao-distribuida.zip'):
                return x['url'], x.get('last_modified') or x.get('metadata_modified') or ''
    raise SystemExit('relação de GD não encontrada no CKAN da ANEEL')


def num(v):
    try:
        return float(str(v).replace('.', '').replace(',', '.'))
    except ValueError:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--forcar', action='store_true')
    ap.add_argument('--zip')
    ap.add_argument('--saida', default=os.path.join(AQUI, 'saida'))
    a = ap.parse_args()
    ufs = sorted({d['uf'] for d in CFG['distribuidoras'].values()})
    url, modif = recurso_zip()
    chave = hashlib.sha256(json.dumps([modif, ufs, VERSAO]).encode()).hexdigest()[:16]
    hash_f = os.path.join(AQUI, 'mercado_hash.txt')
    antigo = open(hash_f).read().strip() if os.path.exists(hash_f) else ''
    if chave == antigo and not a.forcar:
        log('relação de GD sem mudança desde a última publicação (', modif, ')')
        if os.environ.get('GITHUB_OUTPUT'):
            open(os.environ['GITHUB_OUTPUT'], 'a').write('novo=false\n')
        return
    arq = a.zip
    if not arq:
        os.makedirs(os.path.join(AQUI, 'cache'), exist_ok=True)
        arq = os.path.join(AQUI, 'cache', 'gd.zip')
        log('baixando a relação de GD da ANEEL')
        with urllib.request.urlopen(url, timeout=1800) as r, open(arq, 'wb') as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
    hoje = datetime.date.today()
    corte12 = (hoje - datetime.timedelta(days=365)).isoformat()
    mun = {}
    ref = ''
    novo = lambda: {'n': 0, 'kw': 0.0}
    with zipfile.ZipFile(arq) as z:
        nome = next(n for n in z.namelist() if n.lower().endswith('.csv'))
        with z.open(nome) as bruto:
            for r in csv.DictReader(io.TextIOWrapper(bruto, encoding='utf-8', errors='replace'), delimiter=';'):
                uf = r.get('SigUF')
                if uf not in ufs:
                    continue
                ref = ref or r.get('DatGeracaoConjuntoDados', '')
                cod = (r.get('CodMunicipioIbge') or '').strip()
                if not cod:
                    continue
                kw = num(r.get('MdaPotenciaInstaladaKW'))
                if kw <= 0 or kw > 5000:
                    continue
                m = mun.setdefault(cod, {'nome': r.get('NomMunicipio', '').strip(), 'uf': uf, 'n': 0, 'kw': 0.0, 'n12': 0, 'kw12': 0.0,
                                         'classe': defaultdict(novo), 'porte': defaultdict(novo), 'modalidade': defaultdict(novo),
                                         'ano': defaultdict(novo), 'faixa': defaultdict(int)})
                m['n'] += 1
                m['kw'] += kw
                data = (r.get('DthAtualizaCadastralEmpreend') or '')[:10]
                if data >= corte12:
                    m['n12'] += 1
                    m['kw12'] += kw
                for k, v in (('classe', r.get('DscClasseConsumo') or '—'), ('porte', r.get('DscPorte') or '—'),
                             ('modalidade', r.get('DscModalidadeHabilitado') or '—'), ('ano', max(data[:4], str(ANO_INICIAL)) if data[:4].isdigit() else '—')):
                    m[k][v.strip()]['n'] += 1
                    m[k][v.strip()]['kw'] += kw
                faixa = '≤10' if kw <= 10 else '10–30' if kw <= 30 else '30–75' if kw <= 75 else '75–500' if kw <= 500 else '>500'
                m['faixa'][faixa] += 1
    for m in mun.values():
        m['kw'] = round(m['kw'], 1)
        m['kw12'] = round(m['kw12'], 1)
        for k in ('classe', 'porte', 'modalidade', 'ano'):
            m[k] = {x: {'n': v['n'], 'kw': round(v['kw'], 1)} for x, v in sorted(m[k].items())}
        m['faixa'] = dict(m['faixa'])
    saida = {'versao': VERSAO, 'referencia': ref, 'arquivo_aneel': modif, 'ufs': ufs, 'municipios': mun,
             'gerado_em': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ'),
             'fonte': 'ANEEL – relação de empreendimentos de geração distribuída (dados abertos)'}
    os.makedirs(a.saida, exist_ok=True)
    json.dump(saida, open(os.path.join(a.saida, 'mercado_gd.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    open(hash_f, 'w').write(chave + '\n')
    log(len(mun), 'municípios;', sum(m['n'] for m in mun.values()), 'usinas; arquivo', round(os.path.getsize(os.path.join(a.saida, 'mercado_gd.json')) / 1e3), 'kB')
    if os.environ.get('GITHUB_OUTPUT'):
        open(os.environ['GITHUB_OUTPUT'], 'a').write('novo=true\n')


if __name__ == '__main__':
    main()
