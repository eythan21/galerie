#!/usr/bin/env python3
"""
castillaleon.py — Scan complet Castille-et-León
Trouve toutes les maisons unifamiliales avec combles perdus potentiels.
Reprend automatiquement la ou il s'est arrete si interrompu.

Usage:
    python3 castillaleon.py
    python3 castillaleon.py --reset   # recommencer depuis le debut
"""

import sys, re, os, zipfile, io, json, time, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm
from datetime import datetime

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANNEE_MIN, ANNEE_MAX = 1960, 2006
ETAGES_MAX   = 2
CHECKPOINT   = "castillaleon_progress.json"
CSV_OUT      = "castillaleon_combles.csv"
DELAY_REQ    = 0.3   # secondes entre requetes INSPIRE

# Provinces Castille-et-León
PROVINCES_CYL = {
    "05": "Avila",
    "09": "Burgos",
    "24": "Leon",
    "34": "Palencia",
    "37": "Salamanca",
    "40": "Segovia",
    "42": "Soria",
    "47": "Valladolid",
    "49": "Zamora",
}

ATOM_BU = "https://www.catastro.hacienda.gob.es/INSPIRE/buildings/ES.SDGC.BU.atom.xml"
NS_A    = "http://www.w3.org/2005/Atom"

COLONNES = [
    "Referencia_Catastral", "Provincia", "Municipio", "Ano",
    "Surface_Totale_M2", "Etages", "Empreinte_M2",
    "Combles_Estimes_M2", "Score",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

# ─── COMBLES & SCORE ──────────────────────────────────────────────────────────

def estimer_combles(superficie: float, plantas: int) -> tuple:
    """
    Estime la surface de combles perdus a partir de l'empreinte au sol.
    L'empreinte = surface_totale / nb_etages.
    Le toit en pente couvre toute l'empreinte → combles ≈ empreinte × 0.85.
    """
    if plantas > 0:
        empreinte = round(superficie / plantas, 1)
    else:
        # Etages inconnus → estimation 60% de la surface (mix 1-2 etages)
        empreinte = round(superficie * 0.60, 1)
    combles = round(empreinte * 0.85, 1)
    return empreinte, combles


def calculer_score(combles: float) -> int:
    if combles >= 100: return 5
    if combles >= 70:  return 4
    if combles >= 45:  return 3
    if combles >= 25:  return 2
    return 1

# ─── CHECKPOINT ───────────────────────────────────────────────────────────────

def charger_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT) as f:
            return json.load(f)
    return {"completed_munis": [], "total_found": 0, "started": datetime.now().isoformat()}


def sauver_checkpoint(cp: dict):
    with open(CHECKPOINT, "w") as f:
        json.dump(cp, f, indent=2)

# ─── INSPIRE ──────────────────────────────────────────────────────────────────

def get_entries(url: str) -> list:
    time.sleep(DELAY_REQ)
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"a": NS_A}
    entries = []
    for e in root.findall("a:entry", ns):
        t = e.find("a:title", ns)
        entries.append({
            "title": t.text if t is not None else "",
            "links": [(l.get("href", ""), l.get("type", "")) for l in e.findall("a:link", ns)],
        })
    return entries


def download_gml(zip_url: str) -> bytes:
    time.sleep(DELAY_REQ)
    r = SESSION.get(zip_url, timeout=300, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(8192):
        buf.write(chunk)
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        gml_names = [n for n in z.namelist() if n.lower().endswith(".gml")]
        if not gml_names:
            raise ValueError("Pas de GML dans le ZIP")
        return z.read(gml_names[0])


def parser_batiments(gml_content: bytes, provincia: str, municipio: str) -> list:
    """Parse le GML et retourne les maisons qualifiees avec estimation des combles."""
    try:
        root = ET.fromstring(gml_content)
    except ET.ParseError:
        return []

    rows = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag not in ("Building", "BuildingPart"):
            continue

        # Reference cadastrale
        ref = None
        for ch in elem.iter():
            if ch.tag.split("}")[-1] == "localId":
                ref = (ch.text or "").strip()
                break
        if not ref:
            continue

        # Annee de construction
        anyo = 0
        for ch in elem.iter():
            if ch.tag.split("}")[-1] in ("yearOfConstruction", "beginning"):
                try:
                    anyo = int((ch.text or "")[:4])
                    if 1800 < anyo < 2100:
                        break
                except ValueError:
                    pass
        if not (ANNEE_MIN <= anyo <= ANNEE_MAX):
            continue

        # Nombre d'etages
        plantas = 0
        for ch in elem.iter():
            if ch.tag.split("}")[-1] in ("numberOfFloorsAboveGround", "storeysAboveGround"):
                try:
                    plantas = int(ch.text or 0)
                    break
                except ValueError:
                    pass
        if plantas > ETAGES_MAX:
            continue

        # Surface
        superficie = 0.0
        for ch in elem.iter():
            if ch.tag.split("}")[-1] in ("officialArea", "value"):
                try:
                    v = float(ch.text or 0)
                    if v > 0:
                        superficie = v
                        break
                except ValueError:
                    pass
        if superficie <= 0:
            superficie = 80.0 * max(plantas, 1)
        if superficie > 550:
            continue

        # Usage resididentiel
        usage = ""
        for ch in elem.iter():
            if ch.tag.split("}")[-1] in ("currentUse", "usage"):
                usage = (ch.text or ch.get("href", "")).lower()
                break
        if usage and not any(k in usage for k in ("residential", "1_", "vivienda", "residencial")):
            continue

        # Logements : unifamiliar = 1
        nb_log = 0
        for ch in elem.iter():
            if ch.tag.split("}")[-1] == "numberOfDwellings":
                try:
                    nb_log = int(ch.text or 0)
                    break
                except ValueError:
                    pass
        if nb_log > 1:
            continue

        empreinte, combles = estimer_combles(superficie, plantas)
        rows.append({
            "Referencia_Catastral": ref[:14] if len(ref) >= 14 else ref,
            "Provincia":            provincia,
            "Municipio":            municipio,
            "Ano":                  anyo,
            "Surface_Totale_M2":    round(superficie, 1),
            "Etages":               plantas,
            "Empreinte_M2":         empreinte,
            "Combles_Estimes_M2":   combles,
            "Score":                calculer_score(combles),
        })
    return rows

# ─── SCAN PROVINCE ────────────────────────────────────────────────────────────

def scanner_province(prov_code: str, prov_name: str, prov_feed_url: str, checkpoint: dict) -> int:
    """Telecharge et traite toutes les municipalites d'une province."""
    print(f"\n{'='*55}")
    print(f"  Province : {prov_name} ({prov_code})")
    print(f"{'='*55}")

    try:
        munis = get_entries(prov_feed_url)
    except Exception as e:
        print(f"  Erreur feed province : {e}")
        return 0

    # Filtrer les entries qui ont un ZIP
    munis_zip = []
    for e in munis:
        for href, typ in e["links"]:
            if href.endswith(".zip") or typ == "application/zip":
                muni_name = re.sub(r'^\d+-', '', e["title"]).replace(' buildings', '').strip().title()
                munis_zip.append((muni_name, href))
                break

    print(f"  {len(munis_zip)} municipalites trouvees")
    total_prov = 0

    for muni_name, zip_url in tqdm(munis_zip, desc=f"  {prov_name}", unit="muni"):
        muni_key = f"{prov_code}/{muni_name}"
        if muni_key in checkpoint["completed_munis"]:
            continue

        try:
            gml = download_gml(zip_url)
            rows = parser_batiments(gml, prov_name, muni_name)

            if rows:
                df_muni = pd.DataFrame(rows, columns=COLONNES)
                write_header = not os.path.exists(CSV_OUT) or os.path.getsize(CSV_OUT) == 0
                df_muni.to_csv(CSV_OUT, mode="a", header=write_header, index=False)
                total_prov += len(rows)
                checkpoint["total_found"] += len(rows)

            checkpoint["completed_munis"].append(muni_key)
            sauver_checkpoint(checkpoint)

        except KeyboardInterrupt:
            raise
        except Exception:
            # Municipalite en erreur → on continue
            checkpoint["completed_munis"].append(muni_key)
            sauver_checkpoint(checkpoint)
            continue

    return total_prov

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  CASTILLE-ET-LEON — Scan Combles Perdus")
    print("  9 provinces | ~2200 municipalites")
    print("=" * 55)

    if "--reset" in sys.argv:
        for f in [CHECKPOINT, CSV_OUT]:
            if os.path.exists(f):
                os.remove(f)
        print("Progression reinitalisee.")

    checkpoint = charger_checkpoint()
    deja_faites = len(checkpoint["completed_munis"])
    if deja_faites > 0:
        print(f"\nReprise : {deja_faites} municipalites deja traitees.")
        print(f"Total trouve jusqu'ici : {checkpoint['total_found']:,} proprietes.")

    # Recuperer le flux national pour trouver les feeds provinciaux CyL
    print("\nRecuperation du flux INSPIRE national...")
    try:
        national = get_entries(ATOM_BU)
    except Exception as e:
        print(f"Erreur flux national : {e}")
        sys.exit(1)

    # Trouver les URLs des feeds provinciaux pour CyL
    prov_feeds = {}
    for entry in national:
        for href, _ in entry["links"]:
            m = re.search(r"/(\d{2})/", href)
            if m and m.group(1) in PROVINCES_CYL and href.endswith(".xml"):
                prov_feeds[m.group(1)] = href

    if not prov_feeds:
        print("Feeds provinciaux non trouves dans le flux national.")
        sys.exit(1)

    print(f"{len(prov_feeds)}/9 provinces CyL trouvees dans le flux INSPIRE.")

    # Scanner chaque province
    total_global = checkpoint["total_found"]
    debut = datetime.now()

    try:
        for prov_code in sorted(prov_feeds):
            prov_name = PROVINCES_CYL[prov_code]
            found = scanner_province(prov_code, prov_name, prov_feeds[prov_code], checkpoint)
            total_global = checkpoint["total_found"]
            elapsed = (datetime.now() - debut).seconds / 60
            print(f"  -> {found} nouvelles proprietes | Total : {total_global:,} | {elapsed:.0f} min")

    except KeyboardInterrupt:
        print("\n\nInterrompu. Progression sauvegardee.")
        print(f"Relancez la commande pour reprendre (deja : {len(checkpoint['completed_munis'])} municipalites).")
        sys.exit(0)

    # ── Resultats finaux ──────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  SCAN TERMINE")
    print(f"  {total_global:,} maisons qualifiees en Castille-et-Leon")
    print(f"{'='*55}")

    if not os.path.exists(CSV_OUT):
        print("Aucune donnee trouvee.")
        sys.exit(0)

    df = pd.read_csv(CSV_OUT)
    df.sort_values("Score", ascending=False, inplace=True)
    df.to_csv(CSV_OUT, index=False)

    print("\nRepartition des scores :")
    total = len(df)
    for s in range(5, 0, -1):
        n = (df["Score"] == s).sum()
        bar = "█" * min(n * 30 // max(total, 1), 30)
        print(f"  Score {s} : {n:>7,}  {bar}")

    print(f"\nRepartition par province :")
    for prov in sorted(df["Provincia"].unique()):
        n = (df["Provincia"] == prov).sum()
        print(f"  {prov:<12} : {n:>6,} proprietes")

    print(f"\nCSV : {CSV_OUT}")
    print("\nPour les adresses d'une ville specifique, utilisez :")
    print('  python3 descargador.py "NomVille"')
    print("\nTermine.")


if __name__ == "__main__":
    main()
