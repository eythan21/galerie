#!/usr/bin/env python3
"""
descargador.py — Cadastre ES → Prospects Isolation
Filtre EXACT : Clase Urbano + Residencial + VIVIENDA Planta=01 Puerta=01

Usage:
    python3 descargador.py "Zamora"
    python3 descargador.py "Zamora" "Morales del Vino" "Villaralbo"
"""

import sys, re, os, zipfile, io, json, time, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANNEE_MIN, ANNEE_MAX = 1960, 2006
ETAGES_MAX = 2
OVC_DELAY  = 2.0   # secondes entre appels OVC

ATOM_BU = "https://www.catastro.hacienda.gob.es/INSPIRE/buildings/ES.SDGC.BU.atom.xml"
ATOM_AD = "https://www.catastro.hacienda.gob.es/INSPIRE/addresses/ES.SDGC.AD.atom.xml"
OVC_URL = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCallejero.asmx/Consulta_DNPRC"
NS_A    = "http://www.w3.org/2005/Atom"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

# Codes province → nom (pour parametre OVC)
PROV_NOM = {
    "05":"Avila","09":"Burgos","24":"Leon","34":"Palencia",
    "37":"Salamanca","40":"Segovia","42":"Soria","47":"Valladolid","49":"Zamora",
    "28":"Madrid","08":"Barcelona","41":"Sevilla","29":"Malaga",
    "46":"Valencia","50":"Zaragoza",
}

VILLE_PROVINCE = {
    "ZAMORA":"49","MORALES DEL VINO":"49","VILLARALBO":"49",
    "ARCENILLAS":"49","PELEAS DE ABAJO":"49","BENAVENTE":"49",
    "SALAMANCA":"37","VALLADOLID":"47","BURGOS":"09","LEON":"24",
    "AVILA":"05","PALENCIA":"34","SEGOVIA":"40","SORIA":"42",
    "MADRID":"28","BARCELONA":"08","SEVILLA":"41","MALAGA":"29",
}

COLONNES = [
    "Referencia_Catastral","Provincia","Municipio","Calle","Numero","CP","Ano",
    "Surf_Total_M2","Surf_VIV_RDC_M2","Surf_VIV_1erEtage_M2",
    "Surf_Garage_M2","Surf_Cave_M2","Combles_Estimes_M2","Score","Statut_Appel",
]

# ─── SCORE ────────────────────────────────────────────────────────────────────

def score(combles):
    if combles >= 100: return 5
    if combles >= 70:  return 4
    if combles >= 45:  return 3
    if combles >= 25:  return 2
    return 1

# ─── OVC API (structure reelle confirmee) ─────────────────────────────────────

def ovc_query(ref, provincia, municipio):
    """
    Appelle OVC avec Provincia + Municipio + RC.
    Structure reponse confirmee :
      cons/lcd = VIVIENDA / APARCAMIENTO / ALMACEN
      cons/dt/lourb/loint/pt = planta
      cons/dt/lourb/loint/pu = puerta
      cons/dfcons/stl = surface m2
    Retourne {} si non qualifie ou erreur.
    """
    time.sleep(OVC_DELAY)
    try:
        r = SESSION.get(OVC_URL, params={"Provincia": provincia, "Municipio": municipio, "RC": ref}, timeout=20)

        if r.status_code == 403 or "limite" in r.text.lower():
            return {"_quota": True}
        if r.status_code != 200:
            return {}

        root = ET.fromstring(r.content)

        def val(tag):
            el = root.find(f".//{tag}")
            return (el.text or "").strip() if el is not None else ""

        # Classe et usage
        cn   = val("cn").upper()    # UR = Urbano
        luso = val("luso").lower()  # "residencial"

        if cn != "UR":              return {}
        if "residencial" not in luso: return {}

        # Adresse
        calle  = val("nv").title()
        numero = val("pnp")
        cp     = val("dp")
        muni   = val("nm").title()

        # Annee + surface totale
        ano = 0
        try: ano = int(val("ant"))
        except: pass

        surf_total = 0.0
        try: surf_total = float(val("sfc") or 0)
        except: pass

        # Construction par planta/puerta
        viv_rdc = viv_e1 = surf_gar = surf_cave = 0.0
        has_viv01 = False

        for cons in root.iter():
            if cons.tag.split("}")[-1] != "cons":
                continue
            lcd = pt = pu = ""
            stl = 0.0
            for ch in cons.iter():
                t = ch.tag.split("}")[-1]
                if   t == "lcd": lcd = (ch.text or "").strip().upper()
                elif t == "pt":  pt  = (ch.text or "").strip()
                elif t == "pu":  pu  = (ch.text or "").strip()
                elif t == "stl":
                    try: stl = float(ch.text or 0)
                    except: pass

            if stl <= 0: continue

            if "VIVIENDA" in lcd:
                if pt in ("00","0","PB","BJ"):
                    viv_rdc += stl
                elif pt == "01":
                    viv_e1 += stl
                    if pu == "01":
                        has_viv01 = True
            elif any(g in lcd for g in ("APARCAMIENTO","GARAJE","GARAGE")):
                surf_gar += stl
            elif any(c in lcd for c in ("ALMACEN","TRASTERO","SOPORT","BODEGA","DEPOSITO")):
                surf_cave += stl

        if not has_viv01:
            return {}

        combles = round(viv_e1 * 0.90, 1)

        return {
            "calle":     calle,
            "numero":    numero,
            "cp":        cp,
            "muni":      muni,
            "ano":       ano,
            "surf_total": round(surf_total, 1),
            "viv_rdc":   round(viv_rdc, 1),
            "viv_e1":    round(viv_e1, 1),
            "surf_gar":  round(surf_gar, 1),
            "surf_cave": round(surf_cave, 1),
            "combles":   combles,
        }
    except Exception:
        return {}

# ─── CHECKPOINT ───────────────────────────────────────────────────────────────

def load_cp(name):
    f = f"cp_{name}.json"
    if os.path.exists(f):
        with open(f) as fp: return json.load(fp)
    return {"done": {}, "rows": []}

def save_cp(name, cp):
    with open(f"cp_{name}.json","w") as fp: json.dump(cp, fp)

# ─── INSPIRE ──────────────────────────────────────────────────────────────────

def get_entries(url):
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"a": NS_A}
    out = []
    for e in root.findall("a:entry", ns):
        t = e.find("a:title", ns)
        out.append({
            "title": t.text if t is not None else "",
            "links": [(l.get("href",""), l.get("type","")) for l in e.findall("a:link", ns)],
        })
    return out

def find_zip(city, atom_url, prov_code=None):
    city_up = city.strip().upper()
    if not prov_code:
        prov_code = VILLE_PROVINCE.get(city_up)
    entries = get_entries(atom_url)

    if prov_code:
        print(f"  Recherche province {prov_code}...", end=" ", flush=True)
        for e in entries:
            for href, _ in e["links"]:
                if f"/{prov_code}/" in href and href.endswith(".xml"):
                    try:
                        for se in get_entries(href):
                            if city_up in se["title"].upper():
                                for sh, st in se["links"]:
                                    if sh.endswith(".zip") or st == "application/zip":
                                        print("OK")
                                        return se["title"], sh, prov_code
                    except: pass
        print("non trouve, scan global...")

    for _, href in tqdm([(e,h) for e in entries for h,_ in e["links"] if h.endswith(".xml")],
                        desc="Scan", unit="prov"):
        pcode = None
        m = re.search(r"/(\d{2})/", href)
        if m: pcode = m.group(1)
        try:
            for se in get_entries(href):
                if city_up in se["title"].upper():
                    for sh, st in se["links"]:
                        if sh.endswith(".zip") or st == "application/zip":
                            return se["title"], sh, pcode
        except: continue
    return None, None, None

def download_gml(url):
    r = SESSION.get(url, timeout=300, stream=True)
    r.raise_for_status()
    total = int(r.headers.get("content-length",0))
    buf = io.BytesIO()
    with tqdm(total=total, unit="B", unit_scale=True, desc="Download") as pb:
        for chunk in r.iter_content(8192):
            buf.write(chunk); pb.update(len(chunk))
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        gmls = [n for n in z.namelist() if n.lower().endswith(".gml")]
        return z.read(gmls[0]) if gmls else b""

def refs_inspire(gml):
    root = ET.fromstring(gml)
    out = []
    for elem in root.iter():
        if elem.tag.split("}")[-1] not in ("Building","BuildingPart"): continue
        ref_val = ""; anyo = 0; plantas = 0; superficie = 0.0; ok = True
        for ch in elem.iter():
            t = ch.tag.split("}")[-1]
            if t == "localId": ref_val = (ch.text or "").strip()
            elif t in ("yearOfConstruction","beginning"):
                try:
                    v = int((ch.text or "")[:4])
                    if 1800 < v < 2100: anyo = v
                except: pass
            elif t in ("numberOfFloorsAboveGround","storeysAboveGround"):
                try: plantas = int(ch.text or 0)
                except: pass
            elif t in ("officialArea","value"):
                try:
                    v = float(ch.text or 0)
                    if v > 0: superficie = v
                except: pass
            elif t in ("currentUse","usage"):
                u = (ch.text or ch.get("href","")).lower()
                if u and not any(k in u for k in ("residential","1_","vivienda")): ok = False
            elif t == "numberOfDwellings":
                try:
                    if int(ch.text or 0) > 1: ok = False
                except: pass
        if not ok: continue
        if not (ANNEE_MIN <= anyo <= ANNEE_MAX): continue
        if plantas > ETAGES_MAX: continue
        if superficie > 550: continue
        # Seulement refs urbaines : les 7 premiers chars sont des chiffres
        # ex: "0179026TL7907N" = urbain  /  "A0CA040TL6955S" = rustic → exclus
        if ref_val and len(ref_val) >= 7 and ref_val[:7].isdigit():
            out.append(ref_val[:14])
    return out

# ─── TRAITEMENT VILLE ─────────────────────────────────────────────────────────

def traiter_ville(city, prov_code=None):
    print(f"\n{'─'*50}")
    print(f"  {city}")
    print(f"{'─'*50}")

    muni_name, bu_url, prov = find_zip(city, ATOM_BU, prov_code)
    if not bu_url:
        print(f"  '{city}' introuvable.")
        return [], prov_code

    muni = re.sub(r'^\d+-','', muni_name.strip()).replace(' buildings','').strip().title()
    prov_nom = PROV_NOM.get(prov, prov or "")
    print(f"  -> {muni} | Province : {prov_nom}")

    bu_gml = download_gml(bu_url)
    refs   = refs_inspire(bu_gml)
    print(f"  -> {len(refs)} candidats INSPIRE")

    if not refs:
        return [], prov

    cp_name = f"{prov}_{muni.lower().replace(' ','_')}"
    cp = load_cp(cp_name)
    deja  = set(cp["done"].keys())
    rows  = cp["rows"]

    a_faire = [r for r in refs if r not in deja]
    duree   = int(len(a_faire) * OVC_DELAY / 60)
    print(f"  -> {len(a_faire)} a interroger via OVC (~{duree} min)")
    print("  Ctrl+C pour interrompre (reprise auto)\n")

    quota_ok = True
    try:
        for ref in tqdm(a_faire, unit="prop", desc="OVC"):
            data = ovc_query(ref, prov_nom, muni)

            if data.get("_quota"):
                print("\n  [!] Quota OVC atteint — change de serveur VPN et relance")
                quota_ok = False
                break

            cp["done"][ref] = 1
            if not data:
                save_cp(cp_name, cp)
                continue

            # Filtre annee (OVC donne l'annee exacte)
            ano = data["ano"]
            if ano and not (ANNEE_MIN <= ano <= ANNEE_MAX):
                save_cp(cp_name, cp)
                continue

            rows.append({
                "Referencia_Catastral":  ref,
                "Provincia":             prov_nom,
                "Municipio":             data["muni"] or muni,
                "Calle":                 data["calle"],
                "Numero":                data["numero"],
                "CP":                    data["cp"],
                "Ano":                   ano,
                "Surf_Total_M2":         data["surf_total"],
                "Surf_VIV_RDC_M2":       data["viv_rdc"],
                "Surf_VIV_1erEtage_M2":  data["viv_e1"],
                "Surf_Garage_M2":        data["surf_gar"],
                "Surf_Cave_M2":          data["surf_cave"],
                "Combles_Estimes_M2":    data["combles"],
                "Score":                 score(data["combles"]),
                "Statut_Appel":          "",
            })
            cp["rows"] = rows
            save_cp(cp_name, cp)

    except KeyboardInterrupt:
        print(f"\n  Interrompu — {len(cp['done'])} traites, {len(rows)} qualifies.")

    return rows, prov

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  CADASTRE ES — VIV Planta 01 Puerta 01")
    print("  Urbano | Residencial | Exact")
    print("=" * 50)

    villes = sys.argv[1:] if len(sys.argv) > 1 else \
             [v.strip() for v in input("\nVille(s) (ex: Zamora)\n> ").split(",") if v.strip()]
    if not villes: sys.exit(1)

    tous = []
    prov = None
    for city in villes:
        rows, prov = traiter_ville(city, prov)
        tous.extend(rows)

    if not tous:
        print("\nAucun resultat.")
        sys.exit(0)

    df = pd.DataFrame(tous, columns=COLONNES)
    df.drop_duplicates("Referencia_Catastral", inplace=True)
    df.sort_values("Score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    total = len(df)

    print(f"\n{'='*50}")
    print(f"  {total:,} maisons VIV Planta01 Puerta01")
    print(f"{'='*50}")
    print(df.head(3).to_string(index=False))
    print("\nRepartition :")
    for s in range(5,0,-1):
        n = (df["Score"]==s).sum()
        print(f"  Score {s} : {n:>5,}  {'█'*min(n*30//max(total,1),30)}")

    nom = "_".join(v.lower().replace(" ","-") for v in villes[:3])
    csv = f"{nom}_vivienda01.csv"
    df.to_csv(csv, index=False)
    print(f"\nCSV : {csv}")
    print("Termine.")

if __name__ == "__main__":
    main()
