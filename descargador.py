#!/usr/bin/env python3
"""
descargador.py — Cadastre INSPIRE → CSV prospects isolation
Filtre : Residencial + Unifamiliar + annee 1960-2006 + max 2 etages
Adresse complete + surfaces + score combles.

Usage:
    python3 descargador.py "Zamora"
    python3 descargador.py "Zamora" "Morales del Vino" "Villaralbo"
"""

import sys, re, os, zipfile, io, time, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANNEE_MIN, ANNEE_MAX = 1960, 2006
ETAGES_MAX = 2

# Raccourcis province par ville connue (evite de scanner les 56 provinces)
VILLE_PROVINCE = {
    "ZAMORA":             "49", "MORALES DEL VINO":   "49",
    "VILLARALBO":         "49", "ARCENILLAS":         "49",
    "PELEAS DE ABAJO":    "49", "BENAVENTE":          "49",
    "SALAMANCA":          "37", "VALLADOLID":         "47",
    "BURGOS":             "09", "LEON":               "24",
    "AVILA":              "05", "PALENCIA":           "34",
    "SEGOVIA":            "40", "SORIA":              "42",
    "MADRID":             "28", "BARCELONA":          "08",
    "SEVILLA":            "41", "MALAGA":             "29",
    "VALENCIA":           "46", "ZARAGOZA":           "50",
}

ATOM_BU = "https://www.catastro.hacienda.gob.es/INSPIRE/buildings/ES.SDGC.BU.atom.xml"
ATOM_AD = "https://www.catastro.hacienda.gob.es/INSPIRE/addresses/ES.SDGC.AD.atom.xml"
NS_A    = "http://www.w3.org/2005/Atom"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

COLONNES = [
    "Referencia_Catastral", "Calle", "Numero", "CP", "Municipio", "Ano",
    "Surface_M2", "Etages", "Combles_Estimes_M2", "Score", "Statut_Appel",
]

# ─── SCORE ────────────────────────────────────────────────────────────────────

def score(combles: float) -> int:
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


def find_zip(city, atom_url, prov_code=None):
    city_up = city.strip().upper()

    # Utiliser le raccourci province si connu
    if not prov_code:
        prov_code = VILLE_PROVINCE.get(city_up)

    entries = get_entries(atom_url)

    # Chercher directement dans la province connue
    if prov_code:
        print(f"  Recherche dans province {prov_code}...", end=" ", flush=True)
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
                    except Exception:
                        pass
        print("introuvable, scan global...")

    # Scan de toutes les provinces avec barre de progression
    prov_feeds = [(e, href) for e in entries
                  for href, _ in e["links"] if href.endswith(".xml")]

    for _, href in tqdm(prov_feeds, desc="Scan provinces", unit="prov"):
        pcode = None
        m = re.search(r"/(\d{2})/", href)
        if m: pcode = m.group(1)
        try:
            for se in get_entries(href):
                if city_up in se["title"].upper():
                    for sh, st in se["links"]:
                        if sh.endswith(".zip") or st == "application/zip":
                            return se["title"], sh, pcode
        except Exception:
            continue
    return None, None, None


def download_gml(zip_url):
    r = SESSION.get(zip_url, timeout=300, stream=True)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    buf = io.BytesIO()
    with tqdm(total=total, unit="B", unit_scale=True, desc="Download") as pb:
        for chunk in r.iter_content(8192):
            buf.write(chunk); pb.update(len(chunk))
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        gmls = [n for n in z.namelist() if n.lower().endswith(".gml")]
        return z.read(gmls[0]) if gmls else b""


def parse_buildings(gml):
    root = ET.fromstring(gml)
    out = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag not in ("Building", "BuildingPart"):
            continue

        ref = anyo = plantas = 0
        ref_val = ""; superficie = 0.0; usage = ""; nb_log = 0

        for ch in elem.iter():
            t = ch.tag.split("}")[-1]
            if t == "localId":
                ref_val = (ch.text or "").strip()
            elif t in ("yearOfConstruction","beginning"):
                try:
                    v = int((ch.text or "")[:4])
                    if 1800 < v < 2100: anyo = v
                except ValueError: pass
            elif t in ("numberOfFloorsAboveGround","storeysAboveGround"):
                try: plantas = int(ch.text or 0)
                except ValueError: pass
            elif t in ("officialArea","value"):
                try:
                    v = float(ch.text or 0)
                    if v > 0: superficie = v
                except ValueError: pass
            elif t in ("currentUse","usage"):
                usage = (ch.text or ch.get("href","")).lower()
            elif t == "numberOfDwellings":
                try: nb_log = int(ch.text or 0)
                except ValueError: pass

        if not (ANNEE_MIN <= anyo <= ANNEE_MAX): continue
        if plantas > ETAGES_MAX: continue
        if usage and not any(k in usage for k in ("residential","1_","vivienda")): continue
        if nb_log > 1: continue
        if superficie <= 0: superficie = 80.0 * max(plantas, 1)
        if superficie > 550: continue

        out.append({
            "ref": ref_val[:14] if len(ref_val) >= 14 else ref_val,
            "anyo": anyo,
            "plantas": plantas,
            "superficie": round(superficie, 1),
        })
    return out


def parse_addresses(gml):
    root = ET.fromstring(gml)
    NS_GML   = "http://www.opengis.net/gml/3.2"
    NS_XLINK = "http://www.w3.org/1999/xlink"

    streets = {}
    for el in root.iter():
        if el.tag.split("}")[-1] == "ThoroughfareName":
            gid = el.get(f"{{{NS_GML}}}id","")
            code = gid.split(".")[-1]
            for ch in el.iter():
                if ch.tag.split("}")[-1] == "text":
                    streets[code] = (ch.text or "").strip().title()
                    break

    postal = {}
    for el in root.iter():
        if el.tag.split("}")[-1] == "PostalDescriptor":
            gid = el.get(f"{{{NS_GML}}}id","")
            last = gid.split(".")[-1]
            if last.isdigit() and len(last) == 5:
                postal[gid] = last

    addrs = {}
    for el in root.iter():
        if el.tag.split("}")[-1] != "Address": continue
        lid = None
        for ch in el.iter():
            if ch.tag.split("}")[-1] == "localId":
                lid = (ch.text or "").strip(); break
        if not lid: continue
        parts = lid.split(".")
        if len(parts) < 5: continue
        refcat = parts[-1]; numero = parts[-2]; scode = parts[2]
        cp = ""
        for ch in el.iter():
            if ch.tag.split("}")[-1] == "component":
                href = ch.get(f"{{{NS_XLINK}}}href","")
                if "SDGC.PD." in href:
                    pd_id = href.lstrip("#")
                    cp = postal.get(pd_id, pd_id.split(".")[-1])
        addrs[refcat] = {
            "Calle":  streets.get(scode,""),
            "Numero": numero,
            "CP":     cp,
        }
    return addrs

# ─── VILLE ────────────────────────────────────────────────────────────────────

def traiter_ville(city, prov_code=None):
    print(f"\n{'─'*50}")
    print(f"  {city}")
    print(f"{'─'*50}")

    muni_name, bu_url, prov = find_zip(city, ATOM_BU, prov_code)
    if not bu_url:
        print(f"  '{city}' introuvable.")
        return [], prov_code

    muni = re.sub(r'^\d+-','', muni_name).replace(' buildings','').strip().title()
    print(f"  -> {muni} (province {prov})")

    bu_gml  = download_gml(bu_url)
    batiments = parse_buildings(bu_gml)
    print(f"  -> {len(batiments)} batiments qualifies")
    if not batiments:
        return [], prov

    addrs = {}
    _, ad_url, _ = find_zip(city, ATOM_AD, prov)
    if ad_url:
        ad_gml = download_gml(ad_url)
        addrs  = parse_addresses(ad_gml)
        print(f"  -> {len(addrs)} adresses")

    rows = []
    for b in batiments:
        ref  = b["ref"]
        addr = addrs.get(ref, {})
        etages = max(b["plantas"], 1)
        surf   = b["superficie"]
        # Combles = empreinte au sol = surface / etages
        empreinte = round(surf / etages, 1)
        combles   = round(empreinte * 0.85, 1)
        rows.append({
            "Referencia_Catastral": ref,
            "Calle":                addr.get("Calle",""),
            "Numero":               addr.get("Numero",""),
            "CP":                   addr.get("CP",""),
            "Municipio":            muni,
            "Ano":                  b["anyo"],
            "Surface_M2":           surf,
            "Etages":               b["plantas"],
            "Combles_Estimes_M2":   combles,
            "Score":                score(combles),
            "Statut_Appel":         "",
        })
    return rows, prov

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  POSTESPAGNE — Cadastre INSPIRE")
    print("=" * 50)

    villes = sys.argv[1:] if len(sys.argv) > 1 else \
             [v.strip() for v in input("\nVille(s) (ex: Zamora, Morales del Vino)\n> ").split(",") if v.strip()]

    if not villes:
        print("Aucune ville.")
        sys.exit(1)

    tous = []
    prov = None
    for city in villes:
        rows, prov = traiter_ville(city, prov)
        tous.extend(rows)

    if not tous:
        print("\nAucune propriete.")
        sys.exit(0)

    df = pd.DataFrame(tous, columns=COLONNES)
    df.sort_values("Score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    total = len(df)

    print(f"\n{'='*50}")
    print(f"  {total:,} proprietes | {', '.join(villes)}")
    print(f"{'='*50}")
    print(df.head(3).to_string(index=False))

    print("\nRepartition :")
    for s in range(5,0,-1):
        n = (df["Score"]==s).sum()
        print(f"  Score {s} : {n:>5,}  {'█'*min(n*30//max(total,1),30)}")

    print(f"\nCombien exporter ? (Entree = tout {total:,})")
    choix = input("  > ").strip()
    if choix:
        try: df = df.head(max(1, min(int(choix), total)))
        except ValueError: pass

    nom = "_".join(v.lower().replace(" ","-") for v in villes[:3])
    csv = f"{nom}_prospects.csv"
    df.to_csv(csv, index=False)
    print(f"\nCSV : {csv}")
    print("Termine.")


if __name__ == "__main__":
    main()
