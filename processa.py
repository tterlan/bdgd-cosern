#!/usr/bin/env python3
"""
Recorte da BDGD (ANEEL) da Cosern para o cadastro da Guarani Solar.

1. Procura no ArcGIS da ANEEL o File Geodatabase mais recente da Cosern.
2. Se for o mesmo já publicado (estado.json), para (a não ser com --forcar).
3. Baixa, recorta os municípios do config.json e gera um pacote leve:
   meta.json, trafos.json, alimentadores.json, subestacoes.json, mt.ndjson -> saida/pacote.zip
   Transformadores com potência, GD já ligada (limpa), consumidores e energia; rede de MT simplificada.

Uso: python3 processa.py [--forcar] [--gdb caminho.gdb] [--saida pasta]
Precisa do GDAL (ogr2ogr) no PATH. Dados: BDGD/ANEEL, licença ODbL.
"""
import argparse, csv, datetime, glob, io, json, math, os, re, shutil, subprocess, sys, tempfile, urllib.parse, urllib.request, zipfile
from collections import Counter, defaultdict

AQUI = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(AQUI, 'config.json'), encoding='utf-8'))
VERSAO_PACOTE = 2  # 2: rede de MT em mt.ndjson (uma linha por trecho), para o cadastro importar sem carregar tudo na memória
REGRAS_GD = ('POT_INST da BDGD mistura unidades: na BT, valor até 1 é tratado como MW (× 1000); BT acima de 75 kW é descartado; '
             'MT acima de 5.000 kW é descartado; só unidades ativas (SIT_ATIV = AT).')


def log(*a):
    print(datetime.datetime.now().strftime('%H:%M:%S'), *a, flush=True)


def busca_item():
    q = urllib.parse.urlencode({'q': CFG['busca_arcgis'], 'num': 100, 'f': 'json'})
    with urllib.request.urlopen('https://www.arcgis.com/sharing/rest/search?' + q, timeout=60) as r:
        res = json.load(r)['results']
    itens = []
    for it in res:
        m = re.search(r'_40_(\d{4}-\d{2}-\d{2})_V?\w*?_(\d{8}-\d{4})', it['title'])
        if m and it.get('owner') == 'aneel_aneel':
            itens.append((m.group(1), m.group(2), it))
    if not itens:
        raise SystemExit('nenhum File Geodatabase da Cosern encontrado no ArcGIS da ANEEL')
    itens.sort(key=lambda x: (x[0], x[1]))
    ref, carimbo, it = itens[-1]
    return {'item_id': it['id'], 'titulo': it['title'], 'referencia': ref, 'carimbo': carimbo, 'modificado': it.get('modified')}


def baixa(item, pasta):
    url = f"https://www.arcgis.com/sharing/rest/content/items/{item['item_id']}/data"
    destino = os.path.join(pasta, 'bdgd.zip')
    log('baixando', item['titulo'])
    with urllib.request.urlopen(url, timeout=1800) as r, open(destino, 'wb') as f:
        shutil.copyfileobj(r, f, 1 << 20)
    log('baixado', round(os.path.getsize(destino) / 1e6), 'MB')
    with zipfile.ZipFile(destino) as z:
        z.extractall(pasta)
    os.remove(destino)
    g = glob.glob(os.path.join(pasta, '**', '*.gdb'), recursive=True)
    if not g:
        raise SystemExit('o arquivo baixado não tem .gdb')
    return g[0]


def ogr(*args):
    subprocess.run(['ogr2ogr', *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def csv_de(gdb, camada, onde=None, campos=None, xy=False, pasta='.'):
    out = os.path.join(pasta, camada + '.csv')
    a = ['-f', 'CSV', out, gdb, camada]
    if onde:
        a += ['-where', onde]
    if campos:
        a += ['-select', ','.join(campos)]
    if xy:
        a += ['-lco', 'GEOMETRY=AS_XY']
    ogr(*a)
    return list(csv.DictReader(open(out, encoding='utf-8')))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def gd_bt(v):
    return v * 1000 if 0 < v <= 1 else (v if 0 < v <= 75 else None)


def gd_mt(v):
    return v if 0 < v <= 5000 else None


def processa(gdb, item, saida):
    tmp = tempfile.mkdtemp()
    muns = list(CFG['municipios'])
    em = "MUN IN (" + ','.join(f"'{m}'" for m in muns) + ")"
    log('transformadores')
    trs = csv_de(gdb, 'UNTRMT', em, ['COD_ID', 'POT_NOM', 'CTMT', 'POSTO', 'TIP_TRAFO', 'MUN', 'SIT_ATIV'], xy=True, pasta=tmp)
    log(len(trs), 'transformadores; consumidores BT')
    nuc, ene = Counter(), Counter()
    for r in csv_de(gdb, 'UCBT_tab', em, ['UNI_TR_MT', 'SIT_ATIV'] + [f'ENE_{i:02d}' for i in range(1, 13)], pasta=tmp):
        if r['SIT_ATIV'] != 'AT':
            continue
        nuc[r['UNI_TR_MT']] += 1
        ene[r['UNI_TR_MT']] += sum(num(r[f'ENE_{i:02d}']) for i in range(1, 13))
    log('geração distribuída')
    gdt, ngdt, gdc, ngdc = Counter(), Counter(), Counter(), Counter()
    descartadas = 0
    for camada, regra, chave in (('UGBT_tab', gd_bt, 'UNI_TR_MT'), ('UGMT_tab', gd_mt, None)):
        campos = ['CTMT', 'POT_INST', 'SIT_ATIV'] + ([chave] if chave else [])
        for r in csv_de(gdb, camada, em, campos, pasta=tmp):
            if r['SIT_ATIV'] != 'AT':
                continue
            k = regra(num(r['POT_INST']))
            if k is None:
                descartadas += 1
                continue
            gdc[r['CTMT']] += k; ngdc[r['CTMT']] += 1
            if chave:
                gdt[r[chave]] += k; ngdt[r[chave]] += 1
    trafos, xs, ys = [], [], []
    kva_ct, ntr_ct = Counter(), Counter()
    for r in trs:
        if r.get('SIT_ATIV') not in ('AT', '', None):
            continue
        x, y = num(r['X']), num(r['Y'])
        if not (x and y):
            continue
        kva = num(r['POT_NOM'])
        xs.append(x); ys.append(y)
        kva_ct[r['CTMT']] += kva; ntr_ct[r['CTMT']] += 1
        trafos.append({'id': r['COD_ID'], 'lat': round(y, 6), 'lon': round(x, 6), 'kva': kva, 'gd': round(gdt[r['COD_ID']], 1),
                       'ngd': ngdt[r['COD_ID']], 'nuc': nuc[r['COD_ID']], 'dmed': round(ene[r['COD_ID']] / 8760, 1),
                       'ct': r['CTMT'], 'posto': r['POSTO'], 'tipo': r['TIP_TRAFO'], 'mun': r['MUN']})
    m = CFG['margem_graus']
    bbox = [round(min(xs) - m, 4), round(min(ys) - m, 4), round(max(xs) + m, 4), round(max(ys) + m, 4)]
    log('alimentadores e subestações')
    cts = {r['COD_ID']: r for r in csv_de(gdb, 'CTMT', None, ['COD_ID', 'NOME', 'SUB', 'TEN_OPE'] + [f'ENE_{i:02d}' for i in range(1, 13)], pasta=tmp)}
    subs = {}
    out_sub = os.path.join(tmp, 'sub.geojson')
    # alimentadores que atendem os municípios escolhidos (pelos transformadores e pelas GD); a rede de MT vem só deles,
    # o que evita puxar o estado inteiro quando os municípios ficam longe uns dos outros (ex.: Mossoró e Natal)
    cts_sel = sorted(c for c in set(kva_ct) | set(gdc) if c in cts)
    subs_sel = {cts[c]['SUB'] for c in cts_sel}
    ogr('-f', 'GeoJSON', out_sub, gdb, 'SUB', '-dialect', 'sqlite', '-sql', 'SELECT COD_ID, NOME, ST_Centroid(Shape) AS g FROM SUB')
    for f in json.load(open(out_sub, encoding='utf-8'))['features']:
        if f['properties']['COD_ID'] not in subs_sel or not f.get('geometry'):
            continue
        c = f['geometry']['coordinates']
        subs[f['properties']['COD_ID']] = {'cod': f['properties']['COD_ID'], 'nome': f['properties']['NOME'], 'lat': round(c[1], 6), 'lon': round(c[0], 6)}
    log('rede de MT de', len(cts_sel), 'alimentadores')
    cond = {r['COD_ID']: r['R_REGUL'] for r in csv_de(gdb, 'SEGCON', None, ['COD_ID', 'R_REGUL'], pasta=tmp)}
    out_mt = os.path.join(tmp, 'mt.geojson')
    ogr('-f', 'GeoJSON', out_mt, gdb, 'SSDMT', '-where', 'CTMT IN (' + ','.join("'" + c.replace("'", "''") + "'" for c in cts_sel) + ')', '-select', 'CTMT,FAS_CON,TIP_CND',
        '-simplify', str(CFG['simplificar_graus']), '-explodecollections', '-lco', 'COORDINATE_PRECISION=6')
    mt = json.load(open(out_mt, encoding='utf-8'))
    feats, ct_usados = [], set()
    for f in mt['features']:
        p = f['properties']; g = f['geometry']
        if not g or g['type'] != 'LineString' or len(g['coordinates']) < 2:
            continue
        ct_usados.add(p['CTMT'])
        feats.append({'type': 'Feature', 'properties': {'c': p['CTMT'], 'f': p['FAS_CON'], 'k': cond.get(p['TIP_CND'], p['TIP_CND'])},
                      'geometry': {'type': 'LineString', 'coordinates': [[round(a, 6), round(b, 6)] for a, b, *_ in g['coordinates']]}})
    alims = []
    for c in sorted(ct_usados | set(kva_ct) | set(gdc)):
        r = cts.get(c)
        if not r:
            continue
        alims.append({'cod': c, 'nome': r['NOME'], 'sub': r['SUB'], 'sub_nome': subs.get(r['SUB'], {}).get('nome', r['SUB']), 
                      'mwh_ano': round(sum(num(r[f'ENE_{i:02d}']) for i in range(1, 13)) / 1000), 'gd_kw': round(gdc[c]), 'ngd': ngdc[c],
                      'ntr': ntr_ct[c], 'kva_tr': round(kva_ct[c])})
    meta = {'versao_pacote': VERSAO_PACOTE, 'distribuidora': CFG['distribuidora'], 'item_id': item['item_id'], 'titulo': item['titulo'],
            'referencia': item['referencia'], 'carimbo_aneel': item['carimbo'],
            'processado_em': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'municipios': CFG['municipios'], 'bbox': bbox,
            'contagens': {'trafos': len(trafos), 'segmentos_mt': len(feats), 'alimentadores': len(alims), 'subestacoes': len(subs),
                          'gd_unidades': sum(ngdc.values()), 'gd_kw': round(sum(gdc.values())), 'gd_descartadas': descartadas},
            'regras_gd': REGRAS_GD, 'fonte': 'ANEEL – Base de Dados Geográfica da Distribuidora (BDGD), licença ODbL'}
    os.makedirs(saida, exist_ok=True)
    arquivos = {'meta.json': meta, 'trafos.json': trafos, 'alimentadores.json': alims, 'subestacoes.json': list(subs.values()),
                }
    with zipfile.ZipFile(os.path.join(saida, 'pacote.zip'), 'w', zipfile.ZIP_DEFLATED) as z:
        for n, d in arquivos.items():
            z.writestr(n, json.dumps(d, ensure_ascii=False, separators=(',', ':')))
        # uma linha por trecho: [alimentador, fases, condutor, [[lon, lat], ...]]
        z.writestr('mt.ndjson', ''.join(json.dumps([f['properties']['c'], f['properties']['f'], f['properties']['k'], f['geometry']['coordinates']],
                                                   ensure_ascii=False, separators=(',', ':')) + '\n' for f in feats))
    json.dump(meta, open(os.path.join(saida, 'meta.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    shutil.rmtree(tmp, ignore_errors=True)
    log('pacote', round(os.path.getsize(os.path.join(saida, 'pacote.zip')) / 1e6, 2), 'MB', meta['contagens'])
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--forcar', action='store_true')
    ap.add_argument('--gdb')
    ap.add_argument('--saida', default=os.path.join(AQUI, 'saida'))
    a = ap.parse_args()
    item = busca_item()
    estado_f = os.path.join(AQUI, 'estado.json')
    estado = json.load(open(estado_f)) if os.path.exists(estado_f) else {}
    log('mais recente na ANEEL:', item['titulo'])
    novo = estado.get('item_id') != item['item_id']
    if not novo and not a.forcar:
        log('sem novidade; nada a fazer')
        if os.environ.get('GITHUB_OUTPUT'):
            open(os.environ['GITHUB_OUTPUT'], 'a').write('novo=false\n')
        return
    pasta = tempfile.mkdtemp()
    gdb = a.gdb or baixa(item, pasta)
    meta = processa(gdb, item, a.saida)
    json.dump({'item_id': item['item_id'], 'titulo': item['titulo'], 'referencia': item['referencia'], 'processado_em': meta['processado_em']},
              open(estado_f, 'w'), ensure_ascii=False, indent=1)
    if os.environ.get('GITHUB_OUTPUT'):
        open(os.environ['GITHUB_OUTPUT'], 'a').write(f"novo=true\nreferencia={re.sub(r'[^0-9-]', '', item['referencia'])}\ntitulo={re.sub(r'[^A-Za-z0-9_.-]', '', item['titulo'])}\n")
    shutil.rmtree(pasta, ignore_errors=True)


if __name__ == '__main__':
    main()
