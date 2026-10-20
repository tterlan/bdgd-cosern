#!/usr/bin/env python3
"""
Recorte da BDGD (ANEEL) das distribuidoras do config.json para o cadastro da Guarani Solar.

Para cada distribuidora:
1. Procura no ArcGIS da ANEEL o File Geodatabase mais recente.
2. Se for o mesmo já publicado (estado.json), pula (a não ser com --forcar).
3. Baixa, recorta os municípios e gera um pacote leve em saida/<distribuidora>/:
   meta.json, trafos.json, alimentadores.json, subestacoes.json, mt.ndjson -> pacote.zip
   Transformadores com potência, GD já ligada (limpa), consumidores e energia; rede de MT simplificada.
Depois grava saida/indice.json com a situação de todas.

Uso: python3 processa.py [--dist cosern] [--forcar] [--gdb caminho.gdb] [--saida pasta]
Precisa do GDAL (ogr2ogr) no PATH. Dados: BDGD/ANEEL, licença ODbL.
"""
import argparse, csv, datetime, glob, io, json, math, os, re, shutil, subprocess, sys, tempfile, urllib.parse, urllib.request, zipfile
from collections import Counter, defaultdict

AQUI = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(AQUI, 'config.json'), encoding='utf-8'))
VERSAO_PACOTE = 2  # 2: rede de MT em mt.ndjson (uma linha por trecho), para o cadastro importar sem carregar tudo na memória
REGRAS_GD = ('Potência de cada GD pela relação de empreendimentos de GD da ANEEL (código CEG), que é mensal e confiável. '
             'Sem o CEG na relação: se a distribuidora grava kW no POT_INST da BDGD (pot_inst_confiavel), usa-o limpo '
             '(BT até 1 = MW × 1000; BT acima de 75 kW e MT acima de 5.000 kW descartados); senão a unidade é descartada. '
             'Só unidades ativas (SIT_ATIV = AT).')


def log(*a):
    print(datetime.datetime.now().strftime('%H:%M:%S'), *a, flush=True)


def busca_item(dist, d):
    q = urllib.parse.urlencode({'q': f'owner:aneel_aneel AND type:"File Geodatabase" AND title:{d["titulo_arcgis"]}', 'num': 100, 'f': 'json'})
    with urllib.request.urlopen('https://www.arcgis.com/sharing/rest/search?' + q, timeout=60) as r:
        res = json.load(r)['results']
    itens = []
    prefixo = re.escape(d['titulo_arcgis'].lower())
    for it in res:
        m = re.search(prefixo + r'_(\d{4}-\d{2}-\d{2})_V?\w*?_(\d{8}-\d{4})$', it['title'].lower())
        if m and it.get('owner') == 'aneel_aneel':
            itens.append((m.group(1), m.group(2), it))
    if not itens:
        raise RuntimeError(f'nenhum File Geodatabase de {d["nome"]} encontrado no ArcGIS da ANEEL')
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


URL_GD = ('https://dadosabertos.aneel.gov.br/dataset/5e0fafd2-21b9-4d5b-b622-40438d40aba2/resource/'
          'b1bd71e7-d0ad-4214-9053-cbd58e9564a7/download/empreendimento-geracao-distribuida.zip')
_GD_LISTA = {}


def lista_gd(ufs, arquivo=None):
    """Potência instalada (kW) de cada GD pelo código CEG (GD.UF.xxx.xxx.xxx), da relação de empreendimentos de GD da ANEEL (mensal).
    O POT_INST da BDGD não é confiável (a Enel CE, por exemplo, não grava kW nele); o CEG liga as duas bases."""
    falta = [u for u in ufs if u not in _GD_LISTA]
    if falta:
        if not arquivo:
            arquivo = _GD_LISTA.get('_arquivo')
        if not arquivo:  # baixa uma vez por execução e reaproveita para as outras distribuidoras
            arquivo = os.path.join(tempfile.mkdtemp(), 'gd.zip')
            log('baixando a relação de GD da ANEEL')
            with urllib.request.urlopen(URL_GD, timeout=1800) as r, open(arquivo, 'wb') as f:
                shutil.copyfileobj(r, f, 1 << 20)
            _GD_LISTA['_arquivo'] = arquivo
        for u in falta:
            _GD_LISTA[u] = {}
        with zipfile.ZipFile(arquivo) as z:
            nome = next(n for n in z.namelist() if n.lower().endswith('.csv'))
            with z.open(nome) as bruto:
                for r in csv.DictReader(io.TextIOWrapper(bruto, encoding='utf-8', errors='replace'), delimiter=';'):
                    if r.get('SigUF') in _GD_LISTA and r['SigUF'] in falta:
                        try:
                            _GD_LISTA[r['SigUF']][r['CodEmpreendimento'].strip()] = float(r['MdaPotenciaInstaladaKW'].replace('.', '').replace(',', '.'))
                        except (ValueError, AttributeError):
                            pass
        log('relação de GD:', {u: len(_GD_LISTA[u]) for u in falta})
    return {k: v for u in ufs for k, v in _GD_LISTA[u].items()}


def processa(dist, d, gdb, item, saida, gd_arquivo=None):
    tmp = tempfile.mkdtemp()
    muns = list(d['municipios'])
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
    pot_ceg = lista_gd([d['uf']], gd_arquivo)
    gdt, ngdt, gdc, ngdc = Counter(), Counter(), Counter(), Counter()
    descartadas, pela_lista = 0, 0
    for camada, regra, chave in (('UGBT_tab', gd_bt, 'UNI_TR_MT'), ('UGMT_tab', gd_mt, None)):
        campos = ['CTMT', 'POT_INST', 'SIT_ATIV', 'CEG_GD'] + ([chave] if chave else [])
        for r in csv_de(gdb, camada, em, campos, pasta=tmp):
            if r['SIT_ATIV'] != 'AT':
                continue
            k = pot_ceg.get((r.get('CEG_GD') or '').strip())
            if k is not None and k > 0:
                pela_lista += 1
            elif d.get('pot_inst_confiavel'):
                k = regra(num(r['POT_INST']))
            else:
                k = None
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
    # caixas de cada município (pelos transformadores), para cortar a rede de MT que sai para longe
    caixas = defaultdict(lambda: [999, 999, -999, -999])
    for t in trafos:
        c = caixas[t['mun']]
        c[0] = min(c[0], t['lon'] - m); c[1] = min(c[1], t['lat'] - m); c[2] = max(c[2], t['lon'] + m); c[3] = max(c[3], t['lat'] + m)
    caixas = list(caixas.values())
    bbox = [round(min(xs) - m, 4), round(min(ys) - m, 4), round(max(xs) + m, 4), round(max(ys) + m, 4)]
    log('alimentadores e subestações')
    cts = {r['COD_ID']: r for r in csv_de(gdb, 'CTMT', None, ['COD_ID', 'NOME', 'SUB', 'TEN_OPE'] + [f'ENE_{i:02d}' for i in range(1, 13)], pasta=tmp)}
    subs = {}
    out_sub = os.path.join(tmp, 'sub.geojson')
    # alimentadores que atendem os municípios escolhidos (pelos transformadores e pelas GD); a rede de MT vem só deles,
    # o que evita puxar o estado inteiro quando os municípios ficam longe uns dos outros (ex.: Mossoró e Natal)
    # (só pelos transformadores: GD com alimentador de outra região é erro de cadastro e puxaria alimentadores distantes)
    cts_sel = sorted(c for c in set(kva_ct) if c in cts)
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
        if not any(c[0] <= x <= c[2] and c[1] <= y <= c[3] for x, y, *_ in g['coordinates'] for c in caixas):
            continue
        ct_usados.add(p['CTMT'])
        feats.append({'type': 'Feature', 'properties': {'c': p['CTMT'], 'f': p['FAS_CON'], 'k': cond.get(p['TIP_CND'], p['TIP_CND'])},
                      'geometry': {'type': 'LineString', 'coordinates': [[round(a, 6), round(b, 6)] for a, b, *_ in g['coordinates']]}})
    alims = []
    for c in sorted(ct_usados | set(kva_ct)):
        r = cts.get(c)
        if not r:
            continue
        alims.append({'cod': c, 'nome': r['NOME'], 'sub': r['SUB'], 'sub_nome': subs.get(r['SUB'], {}).get('nome', r['SUB']), 
                      'mwh_ano': round(sum(num(r[f'ENE_{i:02d}']) for i in range(1, 13)) / 1000), 'gd_kw': round(gdc[c]), 'ngd': ngdc[c],
                      'ntr': ntr_ct[c], 'kva_tr': round(kva_ct[c])})
    meta = {'versao_pacote': VERSAO_PACOTE, 'distribuidora': dist, 'nome': d['nome'], 'uf': d['uf'], 'item_id': item['item_id'], 'titulo': item['titulo'],
            'referencia': item['referencia'], 'carimbo_aneel': item['carimbo'],
            'processado_em': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'municipios': d['municipios'], 'bbox': bbox,
            'contagens': {'trafos': len(trafos), 'segmentos_mt': len(feats), 'alimentadores': len(alims), 'subestacoes': len(subs),
                          'gd_unidades': sum(ngdc.values()), 'gd_kw': round(sum(gdc.values())), 'gd_descartadas': descartadas,
                          'gd_pela_relacao_aneel': pela_lista},
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
    ap.add_argument('--dist', help='só esta distribuidora (chave do config.json)')
    ap.add_argument('--forcar', action='store_true')
    ap.add_argument('--gdb', help='.gdb já baixado (exige --dist)')
    ap.add_argument('--saida', default=os.path.join(AQUI, 'saida'))
    ap.add_argument('--gd-zip', help='relação de GD da ANEEL já baixada (zip)')
    a = ap.parse_args()
    dists = CFG['distribuidoras']
    if a.dist and a.dist not in dists:
        raise SystemExit(f'distribuidora {a.dist} não está no config.json')
    if a.gdb and not a.dist:
        raise SystemExit('--gdb exige --dist')
    estado_f = os.path.join(AQUI, 'estado.json')
    estado = json.load(open(estado_f)) if os.path.exists(estado_f) else {}
    novos, falhas = [], []
    for dist, d in dists.items():
        if a.dist and dist != a.dist:
            continue
        if not re.fullmatch(r'[a-z0-9-]{2,20}', dist):
            raise SystemExit(f'chave de distribuidora inválida: {dist}')
        try:
            item = busca_item(dist, d)
            log(dist, '- mais recente na ANEEL:', item['titulo'])
            if estado.get(dist, {}).get('item_id') == item['item_id'] and not a.forcar:
                log(dist, '- sem novidade')
                continue
            pasta = tempfile.mkdtemp()
            try:
                gdb = a.gdb or baixa(item, pasta)
                meta = processa(dist, d, gdb, item, os.path.join(a.saida, dist), a.gd_zip)
            finally:
                shutil.rmtree(pasta, ignore_errors=True)
            estado[dist] = {'item_id': item['item_id'], 'titulo': item['titulo'], 'referencia': item['referencia'],
                            'processado_em': meta['processado_em'], 'contagens': meta['contagens']}
            novos.append(dist)
        except Exception as e:  # uma distribuidora com problema não segura as outras
            log(dist, '- FALHOU:', e)
            falhas.append(dist)
    # índice com todas as distribuidoras publicadas (o cadastro lê este arquivo primeiro)
    indice = {'versao_pacote': VERSAO_PACOTE, 'gerado_em': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'distribuidoras': {k: {'nome': dists[k]['nome'], 'uf': dists[k]['uf'], **v} for k, v in estado.items() if k in dists}}
    os.makedirs(a.saida, exist_ok=True)
    json.dump(indice, open(os.path.join(a.saida, 'indice.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if novos:
        json.dump(estado, open(estado_f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    log('novas:', novos or 'nenhuma', '| falhas:', falhas or 'nenhuma')
    if os.environ.get('GITHUB_OUTPUT'):
        open(os.environ['GITHUB_OUTPUT'], 'a').write(f"novos={' '.join(novos)}\nfalhas={' '.join(falhas)}\n")
    if falhas and not novos:
        sys.exit(1)


if __name__ == '__main__':
    main()
