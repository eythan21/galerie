#!/usr/bin/env python3
"""
descargador.py — Cadastre INSPIRE + enrichissement OVC exact → CSV / Google Sheets
Filtre les maisons individuelles et calcule les combles exacts par soustraction.

Usage:
    python3 descargador.py "Zamora"
    python3 descargador.py "Zamora" "Morales del Vino" "Villaralbo"
"""

import sys, re, os, zipfile, io, time, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SHEET_URL   = ""
CREDENTIALS = ""

ANNEE_MIN, ANNEE_MAX = 1960, 2006
ETAGES_MAX  = 2
OVC_WORKERS = 8   # Requetes paralleles vers l'API Catastro

COLONNES_SORTIE = [
    "Referencia_Catastral", "Calle", "Numero", "CP", "Municipio", "Ano",
    "Surface_Totale_M2", "Surface_Habitable_M2", "Surface_Garage_M2",
    "Surface_Cave_M2", "Combles_Nets_M2", "Score", "Statut_Appel",
]

# ─── INSPIRE URLS ─────────────────────────────────────────────────────────────
ATOM_BU = "https://www.catastro.hacienda.gob.es/INSPIRE/buildings/ES.SDGC.BU.atom.xml"
ATOM_AD = "https://www.catastro.hacienda.gob.es/INSPIRE/addresses/ES.SDGC.AD.atom.xml"
NS_A    = "http://www.w3.org/2005/Atom"

# ─── OVC API ──────────────────────────────────────────────────────────────────
OVC_URL = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCallejero.asmx/Consulta_DNPRC"

# Codes usage du cadastre espagnol
USOS_HABITABLE = {"VIV", "VV", "VI", "VT", "VP", "V"}
USOS_GARAGE    = {"GAR", "GA", "GR", "PAR", "G"}
USOS_CAVE      = {"TRS", "ALM", "BOD", "TRO", "DEP", "TR", "AL"}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

# ─── SCORING ──────────────────────────────────────────────────────────────────

def calculer_score(combles: float) -> int:
    if combles >= 100: return 5
    if combles >= 70:  return 4
    if combles >= 45:  return 3
    if combles >= 25:  return 2
    return 1

# ─── OVC ENRICHISSEMENT EXACT ─────────────────────────────────────────────────

def get_exact_ovc(ref: str) -> dict:
    """Interroge l'API OVC pour obtenir les surfaces exactes d'une propriete."""
    try:
        r = SESSION.get(OVC_URL, params={"RefCatastral": ref}, timeout=15)
        if r.status_code != 200:
            return {}
        root = ET.fromstring(r.content)
        surf_hab = surf_gar = surf_cave = 0.0
        for elem in root.iter():
            if elem.tag.split("}")[-1] != "cons":
                continue
            lcd = scd = None
            for ch in elem.iter():
                t = ch.tag.split("}")[-1]
                if t == "lcd":
                    lcd = (ch.text or "").strip().upper()
                elif t == "scd":
                    try:
                        scd = float(ch.text or 0)
                    except ValueError:
                        pass
            if lcd and scd and scd > 0:
                if lcd in USOS_HABITABLE:
                    surf_hab += scd
                elif lcd in USOS_GARAGE:
                    surf_gar += scd
                elif lcd in USOS_CAVE:
                    surf_cave += scd
        return {
            "surf_hab":  round(surf_hab, 1),
            "surf_gar":  round(surf_gar, 1),
            "surf_cave": round(surf_cave, 1),
        }
    except Exception:
        return {}


def enrichir_exact(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pour chaque propriete du DataFrame, appelle l'API OVC en parallele
    et recalcule les combles exacts = total - habitable - garage - cave.
    Proprietes avec combles = 0 sont conservees mais avec Score 1.
    """
    refs = df["Referencia_Catastral"].tolist()
    resultats = {}

    print(f"\nEnrichissement exact via OVC ({len(refs)} proprietes, {OVC_WORKERS} connexions)...")
    print("Cela prend ~2-4 minutes...")

    with ThreadPoolExecutor(max_workers=OVC_WORKERS) as executor:
        futures = {executor.submit(get_exact_ovc, ref): ref for ref in refs}
        for future in tqdm(as_completed(futures), total=len(refs), desc="OVC API", unit="prop"):
            ref = futures[future]
            try:
                data = future.result()
                if data:
                    resultats[ref] = data
            except Exception:
                pass

    ok = len(resultats)
    print(f"-> {ok}/{len(refs)} proprietes enrichies avec donnees exactes")

    rows = []
    for _, row in df.iterrows():
        ref  = row["Referencia_Catastral"]
        data = resultats.get(ref)
        row  = row.copy()
        surf = row["Surface_Totale_M2"]

        if data:
            s_hab  = data["surf_hab"]
            s_gar  = data["surf_gar"]
            s_cave = data["surf_cave"]
            combles = round(max(0, surf - s_hab - s_gar - s_cave), 1)
            # Si OVC ne retourne aucun usage → fallback 20%
            if s_hab == 0 and s_gar == 0 and s_cave == 0:
                combles = round(surf * 0.20, 1)
        else:
            # OVC indisponible → estimation conservative 15%
            s_hab = s_gar = s_cave = 0.0
            combles = round(surf * 0.15, 1)

        row["Surface_Habitable_M2"] = s_hab
        row["Surface_Garage_M2"]   = s_gar
        row["Surface_Cave_M2"]     = s_cave
        row["Combles_Nets_M2"]     = combles
        row["Score"]               = calculer_score(combles)
        rows.append(row)

    return pd.DataFrame(rows, columns=COLONNES_SORTIE)

# ─── INSPIRE : RECHERCHE VILLE ────────────────────────────────────────────────

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
    city_up = city_name.strip().upper()
    entries = get_entries(atom_url)

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

    for e in entries:
        if city_up in e["title"].upper():
            for href, typ, _ in e["links"]:
                if href.endswith(".zip") or typ == "application/zip":
                    return e["title"], href, None

    province_feeds = []
    for e in entries:
        for href, typ, _ in e["links"]:
            if href.endswith(".xml") or "atom" in href.lower():
                province_feeds.append(href)

    for pf_url in tqdm(province_feeds, desc="Recherche dans les provinces", unit="prov"):
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

# ─── INSPIRE : TELECHARGEMENT ─────────────────────────────────────────────────

def download_gml_from_zip(zip_url):
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

# ─── INSPIRE : PARSE BATIMENTS ────────────────────────────────────────────────

def parse_buildings(gml_content):
    root = ET.fromstring(gml_content)
    resultats = []

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag not in ("Building", "BuildingPart"):
            continue

        ref = None
        for child in elem.iter():
            if child.tag.split("}")[-1] == "localId":
                ref = (child.text or "").strip()
                break
        if not ref:
            continue

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

        plantas = 0
        for child in elem.iter():
            if child.tag.split("}")[-1] in ("numberOfFloorsAboveGround", "storeysAboveGround"):
                try:
                    plantas = int(child.text or 0)
                    break
                except ValueError:
                    pass

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

        usage = ""
        for child in elem.iter():
            if child.tag.split("}")[-1] in ("currentUse", "usage"):
                usage = (child.text or child.get("href", "")).lower()
                break

        nb_logements = 0
        for child in elem.iter():
            if child.tag.split("}")[-1] == "numberOfDwellings":
                try:
                    nb_logements = int(child.text or 0)
                    break
                except ValueError:
                    pass

        if not (ANNEE_MIN <= anyo <= ANNEE_MAX):
            continue
        if plantas > ETAGES_MAX:
            continue
        if usage and not any(k in usage for k in ("residential", "1_", "vivienda", "residencial")):
            continue
        if nb_logements > 1:
            continue

        etages_calc = max(plantas, 1)
        if superficie <= 0:
            superficie = 80.0 * etages_calc
        if superficie > 550:
            continue

        resultats.append({
            "Referencia_Catastral": ref[:14] if len(ref) >= 14 else ref,
            "Ano":           anyo,
            "Surface_Totale_M2": round(superficie, 1),
        })

    return resultats

# ─── INSPIRE : PARSE ADRESSES ─────────────────────────────────────────────────

def parse_addresses(gml_content):
    root = ET.fromstring(gml_content)
    NS_GML   = "http://www.opengis.net/gml/3.2"
    NS_XLINK = "http://www.w3.org/1999/xlink"

    street_names = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] == "ThoroughfareName":
            gml_id = elem.get(f"{{{NS_GML}}}id", "")
            code = gml_id.split(".")[-1]
            for child in elem.iter():
                if child.tag.split("}")[-1] == "text":
                    street_names[code] = (child.text or "").strip().title()
                    break

    postal = {}
    for elem in root.iter():
        if elem.tag.split("}")[-1] == "PostalDescriptor":
            gml_id = elem.get(f"{{{NS_GML}}}id", "")
            last = gml_id.split(".")[-1]
            if last.isdigit() and len(last) == 5:
                postal[gml_id] = last

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
        refcat      = parts[-1]
        numero      = parts[-2]
        street_code = parts[2]
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

# ─── TRAITEMENT D'UNE VILLE ───────────────────────────────────────────────────

def traiter_ville(city: str, prov_code: str = None) -> tuple:
    """Retourne (liste de rows, province_code_trouve)."""
    print(f"\n{'─'*55}")
    print(f"  Recherche : {city}")
    print(f"{'─'*55}")

    muni_name, bu_url, prov_code_found = find_zip_url(city, ATOM_BU, province_code=prov_code)
    if not bu_url:
        print(f"  '{city}' introuvable — ignoree.")
        return [], prov_code

    muni_clean = re.sub(r'^\d+-', '', muni_name).replace(' buildings', '').strip().title()
    print(f"  Trouve : {muni_clean} (province {prov_code_found})")

    print("  Batiments INSPIRE :")
    bu_content = download_gml_from_zip(bu_url)
    buildings  = parse_buildings(bu_content)
    print(f"  -> {len(buildings)} batiments qualifies")

    if not buildings:
        return [], prov_code_found

    adresses = {}
    print("  Adresses INSPIRE :")
    _, ad_url, _ = find_zip_url(city, ATOM_AD, province_code=prov_code_found)
    if ad_url:
        ad_content = download_gml_from_zip(ad_url)
        adresses   = parse_addresses(ad_content)
        print(f"  -> {len(adresses)} adresses chargees")

    rows = []
    for b in buildings:
        ref  = b["Referencia_Catastral"]
        addr = adresses.get(ref, {})
        rows.append({
            "Referencia_Catastral":  ref,
            "Calle":                 addr.get("Calle", ""),
            "Numero":                addr.get("Numero", ""),
            "CP":                    addr.get("CP", ""),
            "Municipio":             muni_clean,
            "Ano":                   b["Ano"],
            "Surface_Totale_M2":     b["Surface_Totale_M2"],
            "Surface_Habitable_M2":  0.0,
            "Surface_Garage_M2":     0.0,
            "Surface_Cave_M2":       0.0,
            "Combles_Nets_M2":       0.0,
            "Score":                 0,
            "Statut_Appel":          "",
        })
    return rows, prov_code_found

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  POSTESPAGNE — Cadastre Exact")
    print("  INSPIRE + OVC API -> Combles Reels")
    print("=" * 55)

    if len(sys.argv) > 1:
        villes = sys.argv[1:]
    else:
        saisie = input("\nVille(s) separees par virgule (ex: Zamora, Morales del Vino)\n  > ").strip()
        villes = [v.strip() for v in saisie.split(",") if v.strip()]

    if not villes:
        print("Erreur: aucune ville saisie.")
        sys.exit(1)

    # ── Phase 1 : telecharger les batiments INSPIRE ───────────────────────────
    tous_rows = []
    prov_code = None
    for city in villes:
        rows, prov_code = traiter_ville(city, prov_code)
        tous_rows.extend(rows)
        print(f"  Total cumule : {len(tous_rows)} proprietes")

    if not tous_rows:
        print("\nAucune propriete trouvee.")
        sys.exit(0)

    df = pd.DataFrame(tous_rows, columns=COLONNES_SORTIE)

    # ── Phase 2 : enrichissement exact via OVC API ────────────────────────────
    df = enrichir_exact(df)

    # ── Filtrer les Score 0 (OVC absent + pas de combles estimes) ─────────────
    df = df[df["Score"] >= 1].copy()

    df.sort_values("Score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    total = len(df)

    print(f"\n{'='*55}")
    print(f"  TOTAL : {total:,} proprietes")
    print(f"  Villes : {', '.join(villes)}")
    print(f"{'='*55}")
    print("\nApercu des 3 meilleures :")
    print(df.head(3).to_string(index=False))

    print("\nRepartition des scores :")
    for s in range(5, 0, -1):
        n = (df["Score"] == s).sum()
        bar = "█" * min(n * 30 // max(total, 1), 30)
        print(f"  Score {s} : {n:>5}  {bar}")

    sans_combles = (df["Combles_Nets_M2"] == 0).sum()
    print(f"\n  Toit plat / sans combles detectes : {sans_combles:,} proprietes")

    # ── Export ────────────────────────────────────────────────────────────────
    print(f"\nCombien exporter ? (max {total:,} — Entree = tout)")
    choix = input("  Nombre : ").strip()
    if choix:
        try:
            df = df.head(max(1, min(int(choix), total)))
        except ValueError:
            pass

    nom = "_".join(v.lower().replace(" ", "-") for v in villes[:3])
    csv_out = f"{nom}_exact.csv"
    df.to_csv(csv_out, index=False)
    print(f"\nCSV cree : {csv_out}")

    creds_file = CREDENTIALS or "credentials.json"
    if os.path.isfile(creds_file):
        sheet_input = SHEET_URL or input("\nURL Google Sheet (Entree pour ignorer) : ").strip()
        if sheet_input:
            export_to_sheets(df, sheet_input, creds_file)

    print("\nTermine.")


if __name__ == "__main__":
    main()
