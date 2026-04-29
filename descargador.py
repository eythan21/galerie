#!/usr/bin/env python3
"""
descargador.py — Cadastre Espagne → Prospection Isolation
Trouve par province les maisons avec VIVIENDA au 1er etage (Planta 01)
= 2 etages confirmes = combles perdus garantis sous le toit.

Usage:
    python3 descargador.py 49          # Province Zamora
    python3 descargador.py 49 37 47    # Zamora + Salamanca + Valladolid
    python3 descargador.py CYL         # Toute Castille-et-Leon

Provinces CyL : 05=Avila 09=Burgos 24=Leon 34=Palencia
                37=Salamanca 40=Segovia 42=Soria 47=Valladolid 49=Zamora
"""

import sys, re, os, zipfile, io, json, time, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANNEE_MIN, ANNEE_MAX = 1960, 2006
OVC_DELAY    = 3.5   # secondes entre appels OVC (quota ~1000/h)
CHECKPOINT   = "progress_{prov}.json"

PROVINCES_CYL = {"05","09","24","34","37","40","42","47","49"}
NOMS_PROVINCES = {
    "05":"Avila","09":"Burgos","24":"Leon","34":"Palencia",
    "37":"Salamanca","40":"Segovia","42":"Soria","47":"Valladolid","49":"Zamora",
}

ATOM_BU  = "https://www.catastro.hacienda.gob.es/INSPIRE/buildings/ES.SDGC.BU.atom.xml"
OVC_URL  = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCallejero.asmx/Consulta_DNPRC"
NS_A     = "http://www.w3.org/2005/Atom"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

COLONNES = [
    "Referencia_Catastral","Provincia","Municipio",
    "Calle","Numero","CP","Ano",
    "Surface_Totale_M2","Surface_VIV_RDC_M2","Surface_VIV_Etage_M2",
    "Surface_Annexe_M2","Combles_Estimes_M2","Score","Statut_Appel",
]

# ─── SCORING ──────────────────────────────────────────────────────────────────

def score(combles: float) -> int:
    if combles >= 100: return 5
    if combles >= 70:  return 4
    if combles >= 45:  return 3
    if combles >= 25:  return 2
    return 1

# ─── OVC API ──────────────────────────────────────────────────────────────────

_ovc_paused = False

def appeler_ovc(ref: str) -> dict:
    """
    Interroge l'API OVC pour une reference cadastrale.
    Retourne : adresse + construction par planta/puerta.
    """
    global _ovc_paused
    if _ovc_paused:
        time.sleep(60)
        _ovc_paused = False

    time.sleep(OVC_DELAY)
    try:
        r = SESSION.get(OVC_URL, params={"RefCatastral": ref}, timeout=20)

        if r.status_code == 403 or "limite" in r.text.lower():
            print("\n  [!] Quota OVC atteint — pause 5 min...")
            time.sleep(300)
            _ovc_paused = True
            return {}

        if r.status_code != 200:
            return {}

        root = ET.fromstring(r.content)

        # ── Adresse ───────────────────────────────────────────────────────────
        def txt(tag):
            el = root.find(f".//{tag}")
            return (el.text or "").strip() if el is not None else ""

        calle  = txt("nv").title()
        numero = txt("pnp")
        cp     = txt("cp")
        muni   = txt("nm").title()
        ano    = 0
        try:
            ano = int(txt("ant"))
        except ValueError:
            pass

        sfc_total = 0.0
        try:
            sfc_total = float(txt("sfc") or 0)
        except ValueError:
            pass

        # ── Construction par planta ───────────────────────────────────────────
        viv_rdc   = 0.0   # Planta 00
        viv_etage = 0.0   # Planta 01+
        annexe    = 0.0   # ALMACEN, GARAJE, TRASTERO

        VIV  = {"VIV","VV","VI","VT","VP","V"}
        ANNE = {"ALM","GAR","TRS","BOD","TRO","GR","PAR"}

        # Chercher tous les elements 'cons'
        for cons in root.iter():
            if cons.tag.split("}")[-1] != "cons":
                continue

            planta = ""
            lcd    = ""
            scd    = 0.0

            for ch in cons.iter():
                t = ch.tag.split("}")[-1]
                if t == "pt":
                    planta = (ch.text or "").strip()
                elif t == "lcd":
                    lcd = (ch.text or "").strip().upper()
                elif t == "scd":
                    try:
                        scd = float(ch.text or 0)
                    except ValueError:
                        pass

            if scd <= 0:
                continue

            if lcd in VIV:
                if planta in ("00", "BJ", "PB", "0"):
                    viv_rdc += scd
                elif planta not in ("", "-1", "SB", "SS"):
                    viv_etage += scd
            elif lcd in ANNE:
                annexe += scd

        return {
            "calle":      calle,
            "numero":     numero,
            "cp":         cp,
            "muni":       muni,
            "ano":        ano,
            "sfc_total":  sfc_total,
            "viv_rdc":    round(viv_rdc, 1),
            "viv_etage":  round(viv_etage, 1),
            "annexe":     round(annexe, 1),
            "raw_xml":    r.text if (viv_rdc == 0 and viv_etage == 0) else "",
        }

    except Exception:
        return {}

# ─── INSPIRE : references de la province ──────────────────────────────────────

def get_entries(url: str) -> list:
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


def download_gml(zip_url: str) -> bytes:
    r = SESSION.get(zip_url, timeout=300, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(8192):
        buf.write(chunk)
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        gmls = [n for n in z.namelist() if n.lower().endswith(".gml")]
        return z.read(gmls[0]) if gmls else b""


def refs_inspire(gml: bytes) -> list:
    """Extrait les references de batiments residentiels (filtre annee + usage)."""
    try:
        root = ET.fromstring(gml)
    except ET.ParseError:
        return []
    refs = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag not in ("Building", "BuildingPart"):
            continue
        ref = anyo = 0
        ref_val = ""
        for ch in elem.iter():
            t = ch.tag.split("}")[-1]
            if t == "localId":
                ref_val = (ch.text or "").strip()
            elif t in ("yearOfConstruction","beginning"):
                try:
                    anyo = int((ch.text or "")[:4])
                except ValueError:
                    pass
            elif t in ("currentUse","usage"):
                u = (ch.text or ch.get("href","")).lower()
                if u and not any(k in u for k in ("residential","1_","vivienda","residencial")):
                    ref_val = ""  # exclure non-residentiels
        if ref_val and (ANNEE_MIN <= anyo <= ANNEE_MAX):
            refs.append(ref_val[:14])
    return refs


def refs_province(prov_code: str) -> list:
    """Recupere toutes les references de batiments qualifies pour une province."""
    print(f"\nINSPIRE : recherche feed province {prov_code}...")
    national = get_entries(ATOM_BU)

    prov_feed = None
    for e in national:
        for href, _ in e["links"]:
            m = re.search(r"/(\d{2})/", href)
            if m and m.group(1) == prov_code and href.endswith(".xml"):
                prov_feed = href
                break

    if not prov_feed:
        print(f"  Feed introuvable pour province {prov_code}")
        return []

    munis = get_entries(prov_feed)
    munis_zip = []
    for e in munis:
        for href, typ in e["links"]:
            if href.endswith(".zip") or typ == "application/zip":
                munis_zip.append((e["title"], href))
                break

    print(f"  {len(munis_zip)} municipalites a scanner...")
    all_refs = []
    for muni_title, zip_url in tqdm(munis_zip, desc="INSPIRE", unit="muni"):
        try:
            gml = download_gml(zip_url)
            refs = refs_inspire(gml)
            all_refs.extend(refs)
        except Exception:
            continue

    print(f"  -> {len(all_refs)} candidats trouves")
    return all_refs

# ─── CHECKPOINT ───────────────────────────────────────────────────────────────

def load_cp(prov: str) -> dict:
    f = CHECKPOINT.format(prov=prov)
    if os.path.exists(f):
        with open(f) as fp:
            return json.load(fp)
    return {"done": [], "rows": []}


def save_cp(prov: str, cp: dict):
    with open(CHECKPOINT.format(prov=prov), "w") as fp:
        json.dump(cp, fp)

# ─── TRAITEMENT PROVINCE ──────────────────────────────────────────────────────

def traiter_province(prov_code: str) -> str:
    prov_name = NOMS_PROVINCES.get(prov_code, f"Province-{prov_code}")
    csv_out   = f"province_{prov_code}_{prov_name.lower()}.csv"

    print(f"\n{'='*55}")
    print(f"  {prov_name} (province {prov_code})")
    print(f"{'='*55}")

    cp = load_cp(prov_code)
    deja = set(cp["done"])
    rows = cp["rows"]

    if not deja:
        refs = refs_province(prov_code)
    else:
        print(f"  Reprise : {len(deja)} refs deja traitees, {len(rows)} trouvees.")
        refs = refs_province(prov_code)

    refs_a_traiter = [r for r in refs if r not in deja]
    total = len(refs_a_traiter)
    duree = round(total * OVC_DELAY / 60, 0)
    print(f"\nEnrichissement OVC : {total} proprietes (~{int(duree)} min)")
    print("Ctrl+C pour interrompre (reprise automatique au prochain lancement)\n")

    _debug_printed = False

    try:
        for i, ref in enumerate(tqdm(refs_a_traiter, unit="prop")):
            data = appeler_ovc(ref)
            cp["done"].append(ref)

            if not data:
                save_cp(prov_code, cp)
                continue

            # Debug : afficher le XML brut des premieres reponses vides
            if not _debug_printed and data.get("raw_xml"):
                print(f"\n[DEBUG XML — structure reponse OVC]:\n{data['raw_xml'][:600]}\n")
                _debug_printed = True

            # Filtre : doit avoir VIVIENDA au 1er etage
            if data["viv_etage"] <= 0:
                save_cp(prov_code, cp)
                continue

            # Filtre annee (OVC donne l'annee exacte)
            ano = data["ano"]
            if ano and not (ANNEE_MIN <= ano <= ANNEE_MAX):
                save_cp(prov_code, cp)
                continue

            # Combles = surface du dernier etage habitable
            combles = round(data["viv_etage"] * 0.90, 1)

            rows.append({
                "Referencia_Catastral": ref,
                "Provincia":            prov_name,
                "Municipio":            data["muni"],
                "Calle":                data["calle"],
                "Numero":               data["numero"],
                "CP":                   data["cp"],
                "Ano":                  ano,
                "Surface_Totale_M2":    data["sfc_total"],
                "Surface_VIV_RDC_M2":   data["viv_rdc"],
                "Surface_VIV_Etage_M2": data["viv_etage"],
                "Surface_Annexe_M2":    data["annexe"],
                "Combles_Estimes_M2":   combles,
                "Score":                score(combles),
                "Statut_Appel":         "",
            })
            cp["rows"] = rows
            save_cp(prov_code, cp)

            # Sauvegarder CSV toutes les 50 proprietes
            if len(rows) % 50 == 0:
                pd.DataFrame(rows, columns=COLONNES).sort_values("Score", ascending=False).to_csv(csv_out, index=False)

    except KeyboardInterrupt:
        print(f"\n  Interrompu. Progression sauvegardee ({len(cp['done'])} refs traitees).")

    # Export final
    if rows:
        df = pd.DataFrame(rows, columns=COLONNES)
        df.sort_values("Score", ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df.to_csv(csv_out, index=False)

        total_t = len(df)
        print(f"\n{'='*55}")
        print(f"  {total_t:,} maisons avec VIVIENDA au 1er etage")
        print(f"  Province : {prov_name}")
        print(f"{'='*55}")
        print("\nRepartition :")
        for s in range(5, 0, -1):
            n = (df["Score"] == s).sum()
            bar = "█" * min(n * 30 // max(total_t, 1), 30)
            print(f"  Score {s} : {n:>5}  {bar}")
        print(f"\nCSV : {csv_out}")
    else:
        print("  Aucune propriete qualifiee.")

    return csv_out

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  POSTESPAGNE — Vivienda 1er Etage → Combles")
    print("  Filtre : VIVIENDA Planta 01 + adresse complete")
    print("=" * 55)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if not args:
        print("\nUsage : python3 descargador.py <code_province>")
        print("  Exemple : python3 descargador.py 49")
        print("  CyL     : python3 descargador.py CYL")
        print("\nProvince codes :")
        for k, v in sorted(NOMS_PROVINCES.items()):
            print(f"  {k} = {v}")
        sys.exit(0)

    if "CYL" in [a.upper() for a in args]:
        provs = sorted(PROVINCES_CYL)
    else:
        provs = [a.zfill(2) for a in args if a.isdigit()]

    if not provs:
        print("Aucun code province valide.")
        sys.exit(1)

    for prov in provs:
        if prov not in NOMS_PROVINCES:
            print(f"Province {prov} inconnue.")
            continue
        traiter_province(prov)

    print("\nTermine.")


if __name__ == "__main__":
    main()
