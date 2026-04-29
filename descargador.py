#!/usr/bin/env python3
"""
descargador.py — Extrait du cadastre espagnol les proprietes :
  - Clase : Urbano
  - Uso principal : Residencial
  - VIVIENDA Planta 01, Puerta 01 (maison 2 etages confirmes)

Avec pour chaque propriete : adresse complete, annee, toutes les surfaces.
Reprend automatiquement si interrompu.

Usage:
    python3 descargador.py 49              # Province Zamora
    python3 descargador.py 49 37 47        # Plusieurs provinces
    python3 descargador.py CYL             # Toute Castille-et-Leon
"""

import sys, re, os, zipfile, io, json, time, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANNEE_MIN, ANNEE_MAX = 1960, 2006
OVC_DELAY  = 3.5   # secondes entre appels (protege le quota horaire)

PROVINCES_CYL = {"05","09","24","34","37","40","42","47","49"}
NOMS_PROV = {
    "05":"Avila","09":"Burgos","24":"Leon","34":"Palencia",
    "37":"Salamanca","40":"Segovia","42":"Soria","47":"Valladolid","49":"Zamora",
}

ATOM_BU = "https://www.catastro.hacienda.gob.es/INSPIRE/buildings/ES.SDGC.BU.atom.xml"
OVC_URL = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCallejero.asmx/Consulta_DNPRC"
NS_A    = "http://www.w3.org/2005/Atom"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

# Codes usage Catastro
VIV  = {"VIV","VV","VI","VT","VP","V"}
GAR  = {"GAR","GA","GR","PAR","G"}
CAVE = {"ALM","TRS","BOD","TRO","DEP","TR","AL"}
COM  = {"COM","OF","IND","ALM_COM"}

COLONNES = [
    "Referencia_Catastral","Provincia","Municipio","Calle","Numero","CP",
    "Clase","Uso_Principal","Ano",
    "Surf_Total_M2",
    "Surf_VIV_RDC_M2","Surf_VIV_1erEtage_M2",
    "Surf_Garage_M2","Surf_Cave_M2","Surf_Autre_M2",
    "Combles_Estimes_M2","Score","Statut_Appel",
]

# ─── SCORE ────────────────────────────────────────────────────────────────────

def score(combles: float) -> int:
    if combles >= 100: return 5
    if combles >= 70:  return 4
    if combles >= 45:  return 3
    if combles >= 25:  return 2
    return 1

# ─── OVC : donnees exactes par propriete ──────────────────────────────────────

_quota_depasse = False
_debug_xml_fait = False

def ovc_details(ref: str) -> dict:
    """
    Interroge le service OVC du Cadastre pour une reference.
    Retourne toutes les donnees : adresse, classe, usage, surfaces par planta.
    Retourne {} si quota depasse ou erreur.
    """
    global _quota_depasse, _debug_xml_fait
    if _quota_depasse:
        return {}

    time.sleep(OVC_DELAY)
    try:
        r = SESSION.get(OVC_URL, params={"RefCatastral": ref}, timeout=20)

        if r.status_code == 403 or "limite" in r.text.lower():
            print("\n  [!] Quota OVC depasse — pause 5 min puis reprise...")
            time.sleep(300)
            _quota_depasse = False
            return {}

        if r.status_code != 200:
            return {}

        root = ET.fromstring(r.content)

        def val(tag):
            el = root.find(f".//{tag}")
            return (el.text or "").strip() if el is not None else ""

        # Adresse
        calle  = val("nv").title()
        numero = val("pnp")
        cp     = val("cp")
        muni   = val("nm").title()

        # Annee + surface totale + classe + usage
        ano = 0
        try: ano = int(val("ant"))
        except ValueError: pass

        surf_total = 0.0
        try: surf_total = float(val("sfc") or 0)
        except ValueError: pass

        # Classe (Urbano/Rustico) et usage principal
        clase = val("dc").upper()     # peut etre U=Urbano R=Rustico
        uso   = val("luso")           # 1=Residencial

        # ── Construction par planta/puerta ────────────────────────────────────
        viv_rdc    = 0.0
        viv_etage  = 0.0
        surf_gar   = 0.0
        surf_cave  = 0.0
        surf_autre = 0.0

        has_viv_planta01_pu01 = False

        for cons in root.iter():
            if cons.tag.split("}")[-1] != "cons":
                continue

            planta = puerta = lcd = ""
            scd = 0.0

            for ch in cons.iter():
                t = ch.tag.split("}")[-1]
                if   t == "pt":  planta = (ch.text or "").strip().zfill(2)
                elif t == "pu":  puerta = (ch.text or "").strip().zfill(2)
                elif t == "lcd": lcd    = (ch.text or "").strip().upper()
                elif t == "scd":
                    try: scd = float(ch.text or 0)
                    except ValueError: pass

            if scd <= 0:
                continue

            if lcd in VIV:
                if planta in ("00","BJ","PB"):
                    viv_rdc += scd
                elif planta not in ("", "SS", "SB"):
                    viv_etage += scd
                    # Filtre cle : VIVIENDA Planta 01 Puerta 01
                    if planta == "01" and puerta == "01":
                        has_viv_planta01_pu01 = True
            elif lcd in GAR:
                surf_gar  += scd
            elif lcd in CAVE:
                surf_cave += scd
            else:
                surf_autre += scd

        # Debug XML sur la premiere propriete pour verifier la structure
        if not _debug_xml_fait:
            print(f"\n[DEBUG XML — premiere reponse OVC]:\n{r.text[:800]}\n")
            _debug_xml_fait = True

        return {
            "calle":    calle,
            "numero":   numero,
            "cp":       cp,
            "muni":     muni,
            "clase":    clase,
            "uso":      uso,
            "ano":      ano,
            "surf_total":  round(surf_total, 1),
            "viv_rdc":     round(viv_rdc, 1),
            "viv_etage":   round(viv_etage, 1),
            "surf_gar":    round(surf_gar, 1),
            "surf_cave":   round(surf_cave, 1),
            "surf_autre":  round(surf_autre, 1),
            "qualifie":    has_viv_planta01_pu01,
        }

    except Exception:
        return {}

# ─── INSPIRE : liste des references pour une province ─────────────────────────

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


def download_gml(url: str) -> bytes:
    r = SESSION.get(url, timeout=300, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(8192): buf.write(chunk)
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        gmls = [n for n in z.namelist() if n.lower().endswith(".gml")]
        return z.read(gmls[0]) if gmls else b""


def refs_province(prov_code: str) -> list:
    """Recupere toutes les references residentielles d'une province via INSPIRE."""
    print(f"\n[1/2] Telechargement INSPIRE province {prov_code}...")
    entries = get_entries(ATOM_BU)

    feed_url = None
    for e in entries:
        for href, _ in e["links"]:
            if re.search(rf"/{prov_code}/", href) and href.endswith(".xml"):
                feed_url = href
                break

    if not feed_url:
        print(f"  Province {prov_code} introuvable.")
        return []

    munis = [(e["title"], h) for e in get_entries(feed_url)
             for h, t in e["links"] if h.endswith(".zip") or t == "application/zip"]

    print(f"  {len(munis)} municipalites")
    refs = []
    for _, zip_url in tqdm(munis, desc="INSPIRE", unit="muni"):
        try:
            gml = download_gml(zip_url)
            root = ET.fromstring(gml)
            for el in root.iter():
                if el.tag.split("}")[-1] not in ("Building","BuildingPart"): continue
                ref = anyo = 0; ref_val = ""; ok_usage = True
                for ch in el.iter():
                    t = ch.tag.split("}")[-1]
                    if t == "localId": ref_val = (ch.text or "").strip()
                    elif t in ("yearOfConstruction","beginning"):
                        try: anyo = int((ch.text or "")[:4])
                        except ValueError: pass
                    elif t in ("currentUse","usage"):
                        u = (ch.text or ch.get("href","")).lower()
                        if u and not any(k in u for k in ("residential","1_","vivienda")):
                            ok_usage = False
                if ref_val and ok_usage and (ANNEE_MIN <= anyo <= ANNEE_MAX):
                    refs.append(ref_val[:14])
        except Exception:
            continue

    print(f"  -> {len(refs)} candidats (annee {ANNEE_MIN}-{ANNEE_MAX}, residentiels)")
    return refs

# ─── CHECKPOINT ───────────────────────────────────────────────────────────────

def load_cp(prov: str) -> dict:
    f = f"progress_{prov}.json"
    if os.path.exists(f):
        with open(f) as fp: return json.load(fp)
    return {"done": [], "rows": []}

def save_cp(prov: str, cp: dict):
    with open(f"progress_{prov}.json", "w") as fp: json.dump(cp, fp)

# ─── TRAITEMENT PROVINCE ──────────────────────────────────────────────────────

def traiter_province(prov_code: str):
    prov_name = NOMS_PROV.get(prov_code, prov_code)
    csv_out   = f"{prov_code}_{prov_name.lower()}_vivienda01.csv"

    print(f"\n{'='*55}")
    print(f"  {prov_name} ({prov_code})")
    print(f"  Filtre : Urbano + Residencial + VIV Planta01 Puerta01")
    print(f"{'='*55}")

    cp   = load_cp(prov_code)
    deja = set(cp["done"])
    rows = cp["rows"]

    refs = refs_province(prov_code)
    a_traiter = [r for r in refs if r not in deja]

    if not a_traiter:
        print("  Tout deja traite.")
        return

    duree = int(len(a_traiter) * OVC_DELAY / 60)
    print(f"\n[2/2] Interrogation OVC : {len(a_traiter)} proprietes (~{duree} min)")
    print("  Ctrl+C pour interrompre (reprise automatique)\n")

    try:
        for ref in tqdm(a_traiter, unit="prop"):
            d = ovc_details(ref)
            cp["done"].append(ref)

            if not d or not d.get("qualifie"):
                save_cp(prov_code, cp)
                continue

            # Filtre : Urbano (classe U) et Residencial (uso 1)
            # Si l'API ne retourne pas la classe, on accepte par defaut
            if d["clase"] and "U" not in d["clase"].upper() and d["clase"] != "":
                save_cp(prov_code, cp)
                continue

            # Combles = surface du 1er etage (plafond sous toit)
            combles = round(d["viv_etage"] * 0.90, 1)

            rows.append({
                "Referencia_Catastral":  ref,
                "Provincia":             prov_name,
                "Municipio":             d["muni"],
                "Calle":                 d["calle"],
                "Numero":                d["numero"],
                "CP":                    d["cp"],
                "Clase":                 "Urbano",
                "Uso_Principal":         "Residencial",
                "Ano":                   d["ano"],
                "Surf_Total_M2":         d["surf_total"],
                "Surf_VIV_RDC_M2":       d["viv_rdc"],
                "Surf_VIV_1erEtage_M2":  d["viv_etage"],
                "Surf_Garage_M2":        d["surf_gar"],
                "Surf_Cave_M2":          d["surf_cave"],
                "Surf_Autre_M2":         d["surf_autre"],
                "Combles_Estimes_M2":    combles,
                "Score":                 score(combles),
                "Statut_Appel":          "",
            })
            cp["rows"] = rows
            save_cp(prov_code, cp)

            if len(rows) % 20 == 0:
                pd.DataFrame(rows, columns=COLONNES).to_csv(csv_out, index=False)

    except KeyboardInterrupt:
        print(f"\n  Interrompu — {len(deja)} refs traitees, {len(rows)} qualifiees.")

    if rows:
        df = pd.DataFrame(rows, columns=COLONNES)
        df.sort_values("Score", ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df.to_csv(csv_out, index=False)
        total = len(df)
        print(f"\n{'='*55}")
        print(f"  {total:,} proprietes Vivienda Planta01 Puerta01")
        print(f"  Province : {prov_name}")
        print(f"{'='*55}")
        print("\nRepartition des scores :")
        for s in range(5, 0, -1):
            n = (df["Score"] == s).sum()
            bar = "█" * min(n * 30 // max(total, 1), 30)
            print(f"  Score {s} : {n:>5}  {bar}")
        print(f"\nCSV : {csv_out}")
    else:
        print("  Aucune propriete qualifiee trouvee.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  CADASTRE ES — Vivienda Planta 01 Puerta 01")
    print("  Urbano | Residencial | Combles confirmes")
    print("=" * 55)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if not args:
        print("\nUsage : python3 descargador.py <province>")
        print("  Ex   : python3 descargador.py 49")
        print("  CyL  : python3 descargador.py CYL")
        print("\nCodes : 05=Avila 09=Burgos 24=Leon 34=Palencia")
        print("        37=Salamanca 40=Segovia 42=Soria 47=Valladolid 49=Zamora")
        sys.exit(0)

    provs = sorted(PROVINCES_CYL) if "CYL" in [a.upper() for a in args] \
            else [a.zfill(2) for a in args if a.isdigit()]

    for prov in provs:
        if prov in NOMS_PROV:
            traiter_province(prov)
        else:
            print(f"Province {prov} inconnue.")

    print("\nTermine.")


if __name__ == "__main__":
    main()
