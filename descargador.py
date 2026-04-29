#!/usr/bin/env python3
"""
descargador.py — Cadastre ES → CSV Prospects Combles
INSPIRE Buildings + Addresses (sans OVC, sans limite de taux)

Usage:
    python3 descargador.py "Zamora"
    python3 descargador.py "Zamora" "Morales del Vino" "Villaralbo"
"""

import sys, re, os, zipfile, io, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANNEE_MIN, ANNEE_MAX = 1960, 2006
ETAGES_MAX = 2

ATOM_BU = "https://www.catastro.hacienda.gob.es/INSPIRE/buildings/ES.SDGC.BU.atom.xml"
ATOM_AD = "https://www.catastro.hacienda.gob.es/INSPIRE/addresses/ES.SDGC.AD.atom.xml"
NS_A    = "http://www.w3.org/2005/Atom"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

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
    "Surface_Totale_M2","Etages","Combles_Estimes_M2","Score","Statut_Appel",
]

# ─── SCORE ────────────────────────────────────────────────────────────────────

def score(combles):
    if combles >= 100: return 5
    if combles >= 70:  return 4
    if combles >= 45:  return 3
    if combles >= 25:  return 2
    return 1

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

def find_zip(city, atom_url, prov_code=None, quiet=False):
    city_up = city.strip().upper()
    if not prov_code:
        prov_code = VILLE_PROVINCE.get(city_up)
    entries = get_entries(atom_url)

    if prov_code:
        if not quiet:
            print(f"  Recherche province {prov_code}...", end=" ", flush=True)
        for e in entries:
            for href, _ in e["links"]:
                if f"/{prov_code}/" in href and href.endswith(".xml"):
                    try:
                        for se in get_entries(href):
                            if city_up in se["title"].upper():
                                for sh, st in se["links"]:
                                    if sh.endswith(".zip") or st == "application/zip":
                                        if not quiet:
                                            print("OK")
                                        return se["title"], sh, prov_code
                    except: pass
        if not quiet:
            print("non trouve, scan global...")

    for _, href in tqdm([(e,h) for e in entries for h,_ in e["links"] if h.endswith(".xml")],
                        desc="Scan", unit="prov", disable=quiet):
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

def download_gml(url, desc="Download"):
    r = SESSION.get(url, timeout=300, stream=True)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    buf = io.BytesIO()
    with tqdm(total=total, unit="B", unit_scale=True, desc=desc) as pb:
        for chunk in r.iter_content(8192):
            buf.write(chunk); pb.update(len(chunk))
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        gmls = [n for n in z.namelist() if n.lower().endswith(".gml")]
        return z.read(gmls[0]) if gmls else b""

def refs_inspire(gml):
    """Parse buildings GML → list de dicts {ref, anyo, plantas, superficie}."""
    root = ET.fromstring(gml)
    out = []
    seen = set()
    for elem in root.iter():
        if elem.tag.split("}")[-1] not in ("Building", "BuildingPart"):
            continue
        ref_val = ""; anyo = 0; plantas = 0; superficie = 0.0; ok = True
        for ch in elem.iter():
            t = ch.tag.split("}")[-1]
            if t == "localId":
                ref_val = (ch.text or "").strip()
            elif t in ("yearOfConstruction", "beginning"):
                try:
                    v = int((ch.text or "")[:4])
                    if 1800 < v < 2100: anyo = v
                except: pass
            elif t in ("numberOfFloorsAboveGround", "storeysAboveGround"):
                try: plantas = int(ch.text or 0)
                except: pass
            elif t in ("officialArea", "value"):
                try:
                    v = float(ch.text or 0)
                    if v > 0: superficie = v
                except: pass
            elif t in ("currentUse", "usage"):
                u = (ch.text or ch.get("href", "")).lower()
                if u and not any(k in u for k in ("residential", "1_", "vivienda")):
                    ok = False
            elif t == "numberOfDwellings":
                try:
                    if int(ch.text or 0) > 1: ok = False
                except: pass
        if not ok: continue
        if not (ANNEE_MIN <= anyo <= ANNEE_MAX): continue
        if plantas > ETAGES_MAX: continue
        if superficie > 550: continue
        # Refs urbaines uniquement (7 premiers chars = chiffres)
        if ref_val and len(ref_val) >= 7 and ref_val[:7].isdigit():
            ref14 = ref_val[:14]
            if ref14 not in seen:
                seen.add(ref14)
                out.append({"ref": ref14, "anyo": anyo, "plantas": plantas, "superficie": superficie})
    return out

def parse_addresses_gml(gml):
    """Parse INSPIRE addresses GML → dict {ref14: {Calle, Numero, CP}}."""
    try:
        root = ET.fromstring(gml)
    except ET.ParseError:
        return {}
    addrs = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag != "Address":
            continue
        ref = calle = numero = cp = ""
        for ch in elem.iter():
            t = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if t == "localId" and not ref:
                v = (ch.text or "").strip()
                if v and len(v) >= 14:
                    ref = v[:14]
            elif t in ("text", "nameValue") and not calle:
                v = (ch.text or "").strip()
                if v and len(v) > 2 and not v.replace(" ", "").isdigit():
                    calle = v.title()
            elif t == "designator" and not numero:
                v = (ch.text or ch.get("value", "")).strip()
                if v:
                    numero = v
            elif t == "postCode" and not cp:
                v = (ch.text or "").strip()
                if v:
                    cp = v
        if ref and ref not in addrs:
            addrs[ref] = {"Calle": calle, "Numero": numero, "CP": cp}
    return addrs

# ─── TRAITEMENT VILLE ─────────────────────────────────────────────────────────

def traiter_ville(city, prov_code=None):
    print(f"\n{'─'*50}")
    print(f"  {city}")
    print(f"{'─'*50}")

    muni_name, bu_url, prov = find_zip(city, ATOM_BU, prov_code)
    if not bu_url:
        print(f"  '{city}' introuvable.")
        return [], prov_code

    muni = re.sub(r'^\d+-', '', muni_name.strip()).replace(' buildings', '').strip().title()
    prov_nom = PROV_NOM.get(prov, prov or "")
    print(f"  -> {muni} | Province : {prov_nom}")

    bu_gml = download_gml(bu_url, "Batiments")
    bats = refs_inspire(bu_gml)
    print(f"  -> {len(bats)} candidats INSPIRE")

    if not bats:
        return [], prov

    # Adresses INSPIRE
    _, ad_url, _ = find_zip(city, ATOM_AD, prov, quiet=True)
    addrs = {}
    if ad_url:
        ad_gml = download_gml(ad_url, "Adresses ")
        addrs = parse_addresses_gml(ad_gml)
        print(f"  -> {len(addrs)} adresses")

    # Construction des lignes
    rows = []
    for bat in bats:
        ref = bat["ref"]
        anyo = bat["anyo"]
        plantas = bat["plantas"]
        sup = bat["superficie"] or 80.0 * max(plantas, 1)

        if plantas > 0:
            empreinte = round(sup / plantas, 1)
        else:
            empreinte = round(sup * 0.60, 1)
        combles = round(empreinte * 0.85, 1)

        addr = addrs.get(ref, {})
        rows.append({
            "Referencia_Catastral": ref,
            "Provincia":            prov_nom,
            "Municipio":            muni,
            "Calle":                addr.get("Calle", ""),
            "Numero":               addr.get("Numero", ""),
            "CP":                   addr.get("CP", ""),
            "Ano":                  anyo,
            "Surface_Totale_M2":    round(sup, 1),
            "Etages":               plantas,
            "Combles_Estimes_M2":   combles,
            "Score":                score(combles),
            "Statut_Appel":         "",
        })

    return rows, prov

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  CADASTRE ES — Prospects Combles Perdus")
    print("  INSPIRE Batiments + Adresses")
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
    print(f"  {total:,} proprietes candidates")
    print(f"{'='*50}")
    print(df.head(5).to_string(index=False))
    print("\nRepartition scores :")
    for s in range(5, 0, -1):
        n = (df["Score"] == s).sum()
        print(f"  Score {s} : {n:>5,}  {'█'*min(n*30//max(total,1),30)}")

    nom = "_".join(v.lower().replace(" ", "-") for v in villes[:3])
    csv = f"{nom}_prospects.csv"
    df.to_csv(csv, index=False)
    print(f"\nCSV : {csv}")
    print("Termine.")

if __name__ == "__main__":
    main()
