#!/usr/bin/env python3
"""
descargador.py — Telechargement automatique Cadastre ES -> Google Sheets
Recherche une ville espagnole, telecharge les donnees INSPIRE,
filtre les maisons individuelles et exporte vers Google Sheets.

Usage:
    python3 descargador.py
    python3 descargador.py "Malaga"
    python3 descargador.py "Valencia"
"""

import sys, re, os, zipfile, io, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm

# ─── CONFIG RAPIDE ────────────────────────────────────────────────────────────
SHEET_URL   = ""   # ex: "https://docs.google.com/spreadsheets/d/..."
CREDENTIALS = ""   # ex: "credentials.json"

ANNEE_MIN, ANNEE_MAX = 1960, 2006
ETAGES_MAX = 2
COLONNES_SORTIE = [
    "Referencia_Catastral", "Calle", "Numero", "CP",
    "Municipio", "Ano", "Estimation_M2_Combles", "Statut_Appel"
]

# ─── INSPIRE CATASTRO ─────────────────────────────────────────────────────────
ATOM_BU = "https://www.catastro.hacienda.gob.es/INSPIRE/buildings/ES.SDGC.BU.atom.xml"
ATOM_AD = "https://www.catastro.hacienda.gob.es/INSPIRE/addresses/ES.SDGC.AD.atom.xml"
NS_A    = "http://www.w3.org/2005/Atom"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

# ─── RECHERCHE VILLE ──────────────────────────────────────────────────────────

def get_entries(url):
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"a": NS_A}
    entries = []
    for e in root.findall("a:entry", ns):
        title_el = e.find("a:title", ns)
        links = e.findall("a:link", ns)
        entries.append({
            "title": title_el.text if title_el is not None else "",
            "links": [(l.get("href", ""), l.get("type", ""), l.get("rel", "")) for l in links],
        })
    return entries


def find_zip_url(city_name, atom_url):
    """
    Parcourt le flux ATOM national puis les flux provinciaux
    pour trouver l'URL du ZIP de la ville demandee.
    """
    city_up = city_name.strip().upper()

    # Niveau 1 : flux national
    entries = get_entries(atom_url)

    # Chercher directement au niveau national
    for e in entries:
        if city_up in e["title"].upper():
            for href, typ, _ in e["links"]:
                if href.endswith(".zip") or typ == "application/zip":
                    return e["title"], href

    # Niveau 2 : flux provinciaux
    province_feeds = []
    for e in entries:
        for href, typ, _ in e["links"]:
            if href.endswith(".xml") or "atom" in href.lower():
                province_feeds.append(href)

    for pf_url in tqdm(province_feeds, desc="Recherche dans les provinces", unit="prov"):
        try:
            sub = get_entries(pf_url)
            for e in sub:
                if city_up in e["title"].upper():
                    for href, typ, _ in e["links"]:
                        if href.endswith(".zip") or typ == "application/zip":
                            return e["title"], href
        except Exception:
            continue

    return None, None

# ─── TELECHARGEMENT ───────────────────────────────────────────────────────────

def download_gml_from_zip(zip_url):
    """Telecharge un ZIP et retourne le contenu du premier fichier GML."""
    r = SESSION.get(zip_url, timeout=300, stream=True)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    buf = io.BytesIO()
    with tqdm(total=total, unit="B", unit_scale=True, desc="Download") as pbar:
        for chunk in r.iter_content(8192):
            buf.write(chunk)
            pbar.update(len(chunk))
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        gml_names = [n for n in z.namelist() if n.lower().endswith(".gml")]
        if not gml_names:
            raise ValueError("Pas de fichier GML dans le ZIP")
        return z.read(gml_names[0])

# ─── PARSE BATIMENTS ──────────────────────────────────────────────────────────

def parse_buildings(gml_content):
    """Extrait les batiments qualifies depuis le GML INSPIRE."""
    root = ET.fromstring(gml_content)
    resultats = []

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag not in ("Building", "BuildingPart"):
            continue

        # Reference cadastrale
        ref = None
        for child in elem.iter():
            if child.tag.split("}")[-1] == "localId":
                ref = (child.text or "").strip()
                break
        if not ref:
            continue

        # Annee de construction
        anyo = 0
        for child in elem.iter():
            cn = child.tag.split("}")[-1]
            if cn in ("yearOfConstruction", "beginning"):
                try:
                    anyo = int((child.text or "")[:4])
                    if 1800 < anyo < 2100:
                        break
                except ValueError:
                    pass

        # Etages
        plantas = 0
        for child in elem.iter():
            if child.tag.split("}")[-1] in ("numberOfFloorsAboveGround", "storeysAboveGround"):
                try:
                    plantas = int(child.text or 0)
                    break
                except ValueError:
                    pass

        # Surface officielle
        superficie = 0.0
        for child in elem.iter():
            if child.tag.split("}")[-1] in ("officialArea", "value"):
                try:
                    v = float(child.text or 0)
                    if v > 0:
                        superficie = v
                        break
                except ValueError:
                    pass

        # Usage
        usage = ""
        for child in elem.iter():
            if child.tag.split("}")[-1] in ("currentUse", "usage"):
                usage = (child.text or child.get("href", "")).lower()
                break

        # Nombre de logements (unifamiliar = 1)
        nb_logements = 0
        for child in elem.iter():
            if child.tag.split("}")[-1] == "numberOfDwellings":
                try:
                    nb_logements = int(child.text or 0)
                    break
                except ValueError:
                    pass

        # ── FILTRES ──────────────────────────────────────────────────────────
        if not (ANNEE_MIN <= anyo <= ANNEE_MAX):
            continue
        if plantas == 0 or plantas > ETAGES_MAX:
            continue
        if usage and not any(k in usage for k in ("residential", "1_", "vivienda", "residencial")):
            continue
        if nb_logements > 1:
            continue

        if superficie <= 0:
            superficie = 80.0 * plantas  # estimation si surface inconnue

        estimation = round((superficie / plantas) * 1.10, 2)

        resultats.append({
            "Referencia_Catastral": ref[:14] if len(ref) >= 14 else ref,
            "Ano": anyo,
            "plantas": plantas,
            "Estimation_M2_Combles": estimation,
        })

    return resultats

# ─── PARSE ADRESSES ───────────────────────────────────────────────────────────

def parse_addresses(gml_content):
    """Extrait les adresses depuis le GML INSPIRE."""
    root = ET.fromstring(gml_content)
    adresses = {}

    for elem in root.iter():
        if elem.tag.split("}")[-1] != "Address":
            continue

        ref = None
        for child in elem.iter():
            if child.tag.split("}")[-1] == "localId":
                ref = (child.text or "").strip()[:14]
                break
        if not ref:
            continue

        calle, numero, cp = "", "", ""
        for child in elem.iter():
            cn = child.tag.split("}")[-1]
            if cn in ("thoroughfareName", "ThoroughfareName"):
                calle = (child.text or "").strip().title()
            elif cn in ("designator", "locatorDesignator") and not numero:
                numero = (child.text or "").strip()
            elif cn == "postCode":
                cp = (child.text or "").strip()

        adresses[ref] = {"Calle": calle, "Numero": numero, "CP": cp}

    return adresses

# ─── EXPORT GOOGLE SHEETS ─────────────────────────────────────────────────────

def export_to_sheets(df, url, creds):
    import pygsheets
    print("\nConnexion a Google Sheets...")
    gc = pygsheets.authorize(service_file=creds)
    sid = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    sh = gc.open_by_key(sid.group(1) if sid else url)
    ws = sh.sheet1
    existantes = ws.get_all_values(include_tailing_empty=False)
    if not existantes or existantes == [[]]:
        ws.update_row(1, COLONNES_SORTIE)
        debut = 2
    else:
        debut = len(existantes) + 1
    ws.update_values(crange=f"A{debut}", values=df.fillna("").astype(str).values.tolist())
    print(f"OK {len(df)} lignes exportees (ligne {debut})")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  POSTESPAGNE — Telechargement Automatique")
    print("  Cadastre INSPIRE -> Google Sheets")
    print("=" * 55)

    city = sys.argv[1] if len(sys.argv) > 1 else input("\nVille espagnole (ex: Malaga) : ").strip()
    if not city:
        print("Erreur: nom de ville manquant.")
        sys.exit(1)

    # 1. Trouver l'URL buildings
    print(f"\nRecherche de '{city}' dans le cadastre...")
    muni_name, bu_url = find_zip_url(city, ATOM_BU)
    if not bu_url:
        print(f"\nVille '{city}' introuvable.")
        print("Conseil : utilisez le nom espagnol (ex: Malaga, Valencia, Sevilla, Alicante)")
        sys.exit(1)
    print(f"Trouve : {muni_name}")

    # 2. Batiments
    print("\nBatiments INSPIRE :")
    bu_content = download_gml_from_zip(bu_url)
    buildings = parse_buildings(bu_content)
    print(f"-> {len(buildings)} batiments qualifies")

    if not buildings:
        print("Aucun batiment ne correspond aux filtres pour cette ville.")
        sys.exit(0)

    # 3. Adresses
    adresses = {}
    print("\nAdresses INSPIRE :")
    _, ad_url = find_zip_url(city, ATOM_AD)
    if ad_url:
        ad_content = download_gml_from_zip(ad_url)
        adresses = parse_addresses(ad_content)
        print(f"-> {len(adresses)} adresses chargees")
    else:
        print("-> Adresses non disponibles pour cette ville")

    # 4. DataFrame
    rows = []
    for b in buildings:
        ref  = b["Referencia_Catastral"]
        addr = adresses.get(ref, {})
        rows.append({
            "Referencia_Catastral":  ref,
            "Calle":                 addr.get("Calle", ""),
            "Numero":                addr.get("Numero", ""),
            "CP":                    addr.get("CP", ""),
            "Municipio":             muni_name,
            "Ano":                   b["Ano"],
            "Estimation_M2_Combles": b["Estimation_M2_Combles"],
            "Statut_Appel":          "",
        })

    df = pd.DataFrame(rows, columns=COLONNES_SORTIE)
    print(f"\n{len(df):,} proprietes qualifiees a {muni_name}")
    print("\nApercu :")
    print(df.head(3).to_string(index=False))

    # 5. Export
    sheet_input = SHEET_URL or input("\nURL Google Sheet : ").strip()
    creds_file  = CREDENTIALS or input("credentials.json : ").strip() or "credentials.json"

    if os.path.isfile(creds_file):
        export_to_sheets(df, sheet_input, creds_file)
    else:
        csv_out = city.lower().replace(" ", "_") + "_cadastre.csv"
        df.to_csv(csv_out, index=False)
        print(f"\nPas de credentials -> CSV cree : {csv_out}")

    print("\nTermine.")


if __name__ == "__main__":
    main()
