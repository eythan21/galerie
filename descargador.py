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
    "Referencia_Catastral", "Calle", "Numero", "CP", "Municipio", "Ano",
    "Surface_Totale_M2", "Proba_Garage_pct", "Proba_Cave_pct",
    "Deduction_Estimee_M2", "Combles_Nets_M2", "Score", "Statut_Appel"
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


def find_zip_url(city_name, atom_url, province_code=None):
    """
    Parcourt le flux ATOM national puis les flux provinciaux
    pour trouver l'URL du ZIP de la ville demandee.
    Si province_code est fourni, va directement au bon feed (beaucoup plus rapide).
    Retourne (muni_name, zip_url, province_code_trouve).
    """
    city_up = city_name.strip().upper()

    # Niveau 1 : flux national
    entries = get_entries(atom_url)

    # Si on connait deja le code province, aller directement
    if province_code:
        for e in entries:
            for href, typ, _ in e["links"]:
                if f"/{province_code}/" in href and (href.endswith(".xml") or "atom" in href.lower()):
                    try:
                        sub = get_entries(href)
                        for se in sub:
                            if city_up in se["title"].upper():
                                for sh, st, _ in se["links"]:
                                    if sh.endswith(".zip") or st == "application/zip":
                                        return se["title"], sh, province_code
                    except Exception:
                        pass

    # Chercher directement au niveau national
    for e in entries:
        if city_up in e["title"].upper():
            for href, typ, _ in e["links"]:
                if href.endswith(".zip") or typ == "application/zip":
                    return e["title"], href, None

    # Niveau 2 : flux provinciaux (avec extraction du code province)
    province_feeds = []
    for e in entries:
        for href, typ, _ in e["links"]:
            if href.endswith(".xml") or "atom" in href.lower():
                province_feeds.append(href)

    for pf_url in tqdm(province_feeds, desc="Recherche dans les provinces", unit="prov"):
        # Extraire le code province depuis l'URL (ex: .../buildings/49/ES...)
        pcode = None
        m = re.search(r"/(\d{2})/", pf_url)
        if m:
            pcode = m.group(1)
        try:
            sub = get_entries(pf_url)
            for e in sub:
                if city_up in e["title"].upper():
                    for href, typ, _ in e["links"]:
                        if href.endswith(".zip") or typ == "application/zip":
                            return e["title"], href, pcode
        except Exception:
            continue

    return None, None, None

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
        # Annee obligatoire
        if not (ANNEE_MIN <= anyo <= ANNEE_MAX):
            continue
        # Etages : si nil (0) on accepte, si connu et > max on rejette
        if plantas > ETAGES_MAX:
            continue
        # Usage resididentiel
        if usage and not any(k in usage for k in ("residential", "1_", "vivienda", "residencial")):
            continue
        # Unifamiliar : 1 logement (ou non renseigne)
        if nb_logements > 1:
            continue

        # Estimation surface : officialArea si dispo, sinon 80m2/etage
        etages_calc = max(plantas, 1)
        if superficie <= 0:
            superficie = 80.0 * etages_calc

        estimation = round((superficie / etages_calc) * 1.10, 2)

        # Exclure les batiments avec une surface aberrante (> 500m² = pas une maison)
        if estimation > 550:
            continue

        resultats.append({
            "Referencia_Catastral": ref[:14] if len(ref) >= 14 else ref,
            "Ano": anyo,
            "plantas": plantas,
            "Estimation_M2_Combles": estimation,
        })

    return resultats

# ─── PARSE ADRESSES ───────────────────────────────────────────────────────────

def parse_addresses(gml_content):
    """
    Extrait les adresses depuis le GML INSPIRE espagnol.
    localId format: {prov}.{muni}.{street_code}.{numero}.{refcat}
    """
    root = ET.fromstring(gml_content)
    NS_GML   = "http://www.opengis.net/gml/3.2"
    NS_XLINK = "http://www.w3.org/1999/xlink"

    # 1. Rues : TN.{prov}.{muni}.{code} → nom de rue
    street_names = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] == "ThoroughfareName":
            gml_id = elem.get(f"{{{NS_GML}}}id", "")
            code = gml_id.split(".")[-1]
            for child in elem.iter():
                if child.tag.split("}")[-1] == "text":
                    street_names[code] = (child.text or "").strip().title()
                    break

    # 2. Codes postaux : PD.{prov}.{muni}.{cp} → code postal
    postal = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] == "PostalDescriptor":
            gml_id = elem.get(f"{{{NS_GML}}}id", "")
            last = gml_id.split(".")[-1]
            if last.isdigit() and len(last) == 5:
                postal[gml_id] = last

    # 3. Adresses : localId → {Calle, Numero, CP}
    adresses = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "Address":
            continue
        lid = None
        for child in elem.iter():
            if child.tag.split("}")[-1] == "localId":
                lid = (child.text or "").strip()
                break
        if not lid:
            continue
        parts = lid.split(".")
        if len(parts) < 5:
            continue
        refcat      = parts[-1]          # ex: 0902901TM7000S
        numero      = parts[-2]          # ex: S-N ou 5
        street_code = parts[2]           # ex: 1
        calle = street_names.get(street_code, "")
        cp    = ""
        for child in elem.iter():
            if child.tag.split("}")[-1] == "component":
                href = child.get(f"{{{NS_XLINK}}}href", "")
                if "SDGC.PD." in href:
                    pd_id = href.lstrip("#")
                    cp = postal.get(pd_id, pd_id.split(".")[-1])
        adresses[refcat] = {"Calle": calle, "Numero": numero, "CP": cp}

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

# ─── SCORING ─────────────────────────────────────────────────────────────────

def calculer_scoring(superficie: float, anyo: int) -> dict:
    """
    Calcule les probabilites de garage/cave selon l'annee et la surface,
    deduit ces espaces, et retourne un score 1-5 sur les combles nets.

    Probabilites basees sur les patterns de construction espagnols :
    - 1960-1975 : eres des caves (bodega), peu de garages
    - 1976-1990 : transition, cave + garage commencent
    - 1991-2006 : garage generalise, cave rare
    """

    # ── Probabilites garage ──────────────────────────────────────────────────
    if superficie < 70:
        proba_garage = 5    # Trop petit pour un garage
    elif anyo <= 1975:
        proba_garage = 15
    elif anyo <= 1990:
        proba_garage = 45
    else:
        proba_garage = 72

    # ── Probabilites cave ────────────────────────────────────────────────────
    if anyo <= 1975:
        proba_cave = 68
    elif anyo <= 1990:
        proba_cave = 38
    else:
        proba_cave = 15

    # ── Surface déduite (valeur esperee = proba x surface moyenne) ───────────
    surf_garage_moy = 22  # m² moyen d'un garage en Espagne
    surf_cave_moy   = 18  # m² moyen d'une cave/bodega

    deduction = round(
        (proba_garage / 100) * surf_garage_moy +
        (proba_cave   / 100) * surf_cave_moy,
        1
    )

    # ── Combles nets ─────────────────────────────────────────────────────────
    combles_bruts = round((superficie / 1) * 1.10, 1)  # surface totale × 1.10
    combles_nets  = round(max(0, combles_bruts - deduction), 1)

    # ── Score 1-5 ────────────────────────────────────────────────────────────
    if combles_nets >= 100:
        score = 5   # Excellent — gros contrat garanti
    elif combles_nets >= 70:
        score = 4   # Tres bien
    elif combles_nets >= 45:
        score = 3   # Bien
    elif combles_nets >= 25:
        score = 2   # Moyen
    else:
        score = 1   # Faible potentiel

    return {
        "Proba_Garage_pct":   proba_garage,
        "Proba_Cave_pct":     proba_cave,
        "Deduction_Estimee_M2": deduction,
        "Combles_Nets_M2":    combles_nets,
        "Score":              score,
    }


# ─── TRAITEMENT D'UNE VILLE ───────────────────────────────────────────────────

def traiter_ville(city: str) -> list:
    """Telecharge et filtre les proprietes d'une ville. Retourne une liste de dicts."""
    print(f"\n{'─'*55}")
    print(f"  Recherche : {city}")
    print(f"{'─'*55}")

    muni_name, bu_url, prov_code = find_zip_url(city, ATOM_BU)
    if not bu_url:
        print(f"  '{city}' introuvable — ignoree.")
        print("  Conseil : utilisez le nom espagnol (Zamora, Sevilla, Malaga...)")
        return []

    muni_clean = re.sub(r'^\d+-', '', muni_name).replace(' buildings', '').strip().title()
    print(f"  Trouve : {muni_clean} (province {prov_code})")

    print("  Batiments INSPIRE :")
    bu_content = download_gml_from_zip(bu_url)
    buildings  = parse_buildings(bu_content)
    print(f"  -> {len(buildings)} batiments qualifies")

    if not buildings:
        return []

    adresses = {}
    print("  Adresses INSPIRE :")
    _, ad_url, _ = find_zip_url(city, ATOM_AD, province_code=prov_code)
    if ad_url:
        ad_content = download_gml_from_zip(ad_url)
        adresses   = parse_addresses(ad_content)
        print(f"  -> {len(adresses)} adresses chargees")
    else:
        print("  -> Adresses non disponibles")

    rows = []
    for b in buildings:
        ref     = b["Referencia_Catastral"]
        addr    = adresses.get(ref, {})
        surf    = b["Estimation_M2_Combles"] / 1.10
        scoring = calculer_scoring(surf, b["Ano"])
        rows.append({
            "Referencia_Catastral":  ref,
            "Calle":                 addr.get("Calle", ""),
            "Numero":                addr.get("Numero", ""),
            "CP":                    addr.get("CP", ""),
            "Municipio":             muni_clean,
            "Ano":                   b["Ano"],
            "Surface_Totale_M2":     round(surf, 1),
            "Proba_Garage_pct":      scoring["Proba_Garage_pct"],
            "Proba_Cave_pct":        scoring["Proba_Cave_pct"],
            "Deduction_Estimee_M2":  scoring["Deduction_Estimee_M2"],
            "Combles_Nets_M2":       scoring["Combles_Nets_M2"],
            "Score":                 scoring["Score"],
            "Statut_Appel":          "",
        })
    return rows


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  POSTESPAGNE — Telechargement Automatique")
    print("  Cadastre INSPIRE -> Google Sheets")
    print("=" * 55)

    # Accepte plusieurs villes en arguments
    if len(sys.argv) > 1:
        villes = sys.argv[1:]
    else:
        saisie = input("\nVille(s) espagnole(s) separees par virgule\n  ex: Zamora, Morales del Vino, Villaralbo\n  > ").strip()
        villes = [v.strip() for v in saisie.split(",") if v.strip()]

    if not villes:
        print("Erreur: aucune ville saisie.")
        sys.exit(1)

    # Traiter chaque ville et fusionner
    tous_rows = []
    for city in villes:
        rows = traiter_ville(city)
        tous_rows.extend(rows)
        print(f"  Total cumule : {len(tous_rows)} proprietes")

    if not tous_rows:
        print("\nAucune propriete trouvee.")
        sys.exit(0)

    df = pd.DataFrame(tous_rows, columns=COLONNES_SORTIE)
    df.sort_values("Score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    total = len(df)

    print(f"\n{'='*55}")
    print(f"  TOTAL : {total:,} proprietes qualifiees")
    print(f"  Villes : {', '.join(villes)}")
    print(f"{'='*55}")
    print("\nApercu des 3 meilleures :")
    print(df.head(3).to_string(index=False))

    print("\nRepartition des scores :")
    for s in range(5, 0, -1):
        n = (df["Score"] == s).sum()
        bar = "█" * min(n * 30 // max(total, 1), 30)
        print(f"  Score {s} : {n:>5}  {bar}")

    # Combien exporter
    print(f"\nCombien exporter ? (max {total:,} — Entree = tout)")
    choix = input("  Nombre : ").strip()
    if choix:
        try:
            df = df.head(max(1, min(int(choix), total)))
        except ValueError:
            pass

    # Export CSV
    nom = "_".join(v.lower().replace(" ", "-") for v in villes[:3])
    csv_out = f"{nom}_cadastre.csv"
    df.to_csv(csv_out, index=False)
    print(f"\nCSV cree : {csv_out}")
    print("-> Importe dans Google Sheets : Fichier > Importer")

    # Google Sheets optionnel
    creds_file = CREDENTIALS or "credentials.json"
    if os.path.isfile(creds_file):
        sheet_input = SHEET_URL or input("\nURL Google Sheet (Entree pour ignorer) : ").strip()
        if sheet_input:
            export_to_sheets(df, sheet_input, creds_file)

    print("\nTermine.")
    choix = input("  Nombre : ").strip()

    if choix:
        try:
            n = int(choix)
            n = max(1, min(n, total))
            df = df.head(n)
            print(f"-> Export limite a {n:,} proprietes")
        except ValueError:
            print("-> Valeur invalide, export de toutes les proprietes")
    else:
        print(f"-> Export de toutes les {total:,} proprietes")

    # 6. Export CSV (toujours) + Google Sheets (si credentials dispo)
    csv_out = city.lower().replace(" ", "_") + "_cadastre.csv"
    df.to_csv(csv_out, index=False)
    print(f"\nFichier CSV cree : {csv_out}")
    print("-> Glisse ce fichier dans Google Sheets (Fichier > Importer)")

    # Google Sheets optionnel
    creds_file = CREDENTIALS or "credentials.json"
    if os.path.isfile(creds_file):
        sheet_input = SHEET_URL or input("\nURL Google Sheet (ou Entree pour ignorer) : ").strip()
        if sheet_input:
            export_to_sheets(df, sheet_input, creds_file)

    print("\nTermine.")


if __name__ == "__main__":
    main()
