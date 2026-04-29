#!/usr/bin/env python3
"""
postespagne.py — Fichier .CAT → CSV
Filtre : Clase Urbano + Uso Residencial + VIVIENDA Planta 01 Puerta 01
Extrait toutes les surfaces : habitable RDC, habitable etage, garage, cave.

Usage:
    python3 postespagne.py fichier.CAT
    python3 postespagne.py                  (demande le chemin)
"""

import sys, os, re
import pandas as pd
from tqdm import tqdm

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANNEE_MIN, ANNEE_MAX = 1960, 2006

COLONNES_SORTIE = [
    "Referencia_Catastral", "Calle", "Numero", "CP", "Municipio", "Ano",
    "Surf_Total_M2",
    "Surf_VIV_RDC_M2",      # Planta 00 / BJ / PB
    "Surf_VIV_1erEtage_M2", # Planta 01 ← filtre obligatoire
    "Surf_VIV_Autres_M2",   # Planta 02+
    "Surf_Garage_M2",
    "Surf_Cave_M2",
    "Combles_Estimes_M2",   # = Surf_VIV_1erEtage x 0.90
    "Score",
    "Statut_Appel",
]

# ─── FORMAT SIDPA DU FICHIER .CAT (positions 0-indexed) ──────────────────────

# Tipo 11 — Adresse
T11 = {
    "ref_catastral": slice(9,   23),
    "nombre_via":    slice(100, 150),
    "numero":        slice(150, 154),
    "cod_postal":    slice(162, 167),
    "municipio":     slice(167, 207),
}

# Tipo 13 — Propriete (niveau bien)
T13 = {
    "ref_catastral": slice(9,  23),
    "naturaleza":    slice(23, 24),   # U=Urbana, R=Rustica
    "uso":           slice(24, 26),   # 01=Residencial
    "anyo":          slice(30, 34),
    "superficie":    slice(44, 51),   # Surface totale m²
}

# Tipo 15 — Detail construction (une ligne par espace)
T15 = {
    "ref_catastral": slice(9,  23),
    "planta":        slice(29, 31),   # 00=RDC  01=1er etage  SS=sous-sol
    "puerta":        slice(31, 33),   # 01=porte 1  (unifamiliar = toujours 01)
    "calificacion":  slice(43, 45),   # VV/GA/TR/ALM...
    "superficie":    slice(45, 52),   # Surface en m²
}

# Codes usage
USOS_VIV  = {"VV","VI","VT","VP","VIV","01","1","V"}
USOS_GAR  = {"GA","GAR","GR","03","3","PAR"}
USOS_CAVE = {"TR","TRS","ALM","AL","DE","08","8","TRO","BOD"}

PLANTA_RDC    = {"00","BJ","PB","0","BA"}
PLANTA_ETAGE1 = {"01","1"}

# ─── SCORE ────────────────────────────────────────────────────────────────────

def score(combles: float) -> int:
    if combles >= 100: return 5
    if combles >= 70:  return 4
    if combles >= 45:  return 3
    if combles >= 25:  return 2
    return 1

# ─── PARSING ──────────────────────────────────────────────────────────────────

def parse_cat(filepath: str) -> pd.DataFrame:
    print("\nComptage des lignes...")
    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        total = sum(1 for _ in f)
    print(f"-> {total:,} lignes")

    adresses = {}   # ref → {Calle, Numero, CP, Municipio}
    biens    = {}   # ref → {anyo, superficie}
    # ref → surfaces par planta/type
    viv_rdc    = {}
    viv_etage1 = {}
    viv_autres = {}
    surf_gar   = {}
    surf_cave  = {}
    # ref → flag : a-t-il VIV Planta01 Puerta01 ?
    has_viv01  = set()

    with open(filepath, "r", encoding="latin-1", errors="replace") as fh:
        for line in tqdm(fh, total=total, desc="Lecture", unit="lig"):
            if len(line) < 10:
                continue
            tipo = line[:2]

            # ── Tipo 11 : adresse ─────────────────────────────────────────────
            if tipo == "11":
                ref = line[T11["ref_catastral"]].strip()
                if ref:
                    adresses[ref] = {
                        "Calle":     line[T11["nombre_via"]].strip().title(),
                        "Numero":    line[T11["numero"]].strip(),
                        "CP":        line[T11["cod_postal"]].strip(),
                        "Municipio": line[T11["municipio"]].strip().title(),
                    }

            # ── Tipo 13 : bien immobilier ─────────────────────────────────────
            elif tipo == "13":
                if len(line) < 52:
                    continue
                naturaleza = line[T13["naturaleza"]].strip().upper()
                uso        = line[T13["uso"]].strip()
                # Filtre : Urbano + Residencial uniquement
                if naturaleza != "U":
                    continue
                if uso not in {"01", "1", "R", "02", "2"}:
                    continue
                try:
                    anyo = int(line[T13["anyo"]].strip() or 0)
                    surf = float(line[T13["superficie"]].strip() or 0)
                except ValueError:
                    continue
                if not (ANNEE_MIN <= anyo <= ANNEE_MAX):
                    continue
                if surf <= 0:
                    continue
                ref = line[T13["ref_catastral"]].strip()
                biens[ref] = {"anyo": anyo, "superficie": surf}

            # ── Tipo 15 : detail construction ─────────────────────────────────
            elif tipo == "15":
                if len(line) < 52:
                    continue
                ref    = line[T15["ref_catastral"]].strip()
                planta = line[T15["planta"]].strip().upper().zfill(2)
                puerta = line[T15["puerta"]].strip().upper().zfill(2)
                calif  = line[T15["calificacion"]].strip().upper()
                try:
                    surf = float(line[T15["superficie"]].strip() or 0)
                except ValueError:
                    continue
                if surf <= 0 or not ref:
                    continue

                if calif in USOS_VIV:
                    if planta in PLANTA_RDC:
                        viv_rdc[ref] = viv_rdc.get(ref, 0) + surf
                    elif planta in PLANTA_ETAGE1:
                        viv_etage1[ref] = viv_etage1.get(ref, 0) + surf
                        # Marquer si Puerta = 01 (unifamiliar)
                        if puerta == "01":
                            has_viv01.add(ref)
                    else:
                        viv_autres[ref] = viv_autres.get(ref, 0) + surf
                elif calif in USOS_GAR:
                    surf_gar[ref]  = surf_gar.get(ref, 0)  + surf
                elif calif in USOS_CAVE:
                    surf_cave[ref] = surf_cave.get(ref, 0) + surf

    # ── Construire le DataFrame — filtre VIV Planta01 Puerta01 ───────────────
    print(f"\n{len(biens):,} biens Urbano+Residencial trouves.")
    print(f"{len(has_viv01):,} avec VIVIENDA Planta=01 Puerta=01.")

    rows = []
    for ref in has_viv01:
        if ref not in biens:
            continue
        b       = biens[ref]
        addr    = adresses.get(ref, {})
        s_rdc   = round(viv_rdc.get(ref, 0), 1)
        s_e1    = round(viv_etage1.get(ref, 0), 1)
        s_aut   = round(viv_autres.get(ref, 0), 1)
        s_gar   = round(surf_gar.get(ref, 0), 1)
        s_cave  = round(surf_cave.get(ref, 0), 1)
        combles = round(s_e1 * 0.90, 1)   # surface sous toit = etage x 0.90

        rows.append({
            "Referencia_Catastral":  ref,
            "Calle":                 addr.get("Calle", ""),
            "Numero":                addr.get("Numero", ""),
            "CP":                    addr.get("CP", ""),
            "Municipio":             addr.get("Municipio", ""),
            "Ano":                   b["anyo"],
            "Surf_Total_M2":         round(b["superficie"], 1),
            "Surf_VIV_RDC_M2":       s_rdc,
            "Surf_VIV_1erEtage_M2":  s_e1,
            "Surf_VIV_Autres_M2":    s_aut,
            "Surf_Garage_M2":        s_gar,
            "Surf_Cave_M2":          s_cave,
            "Combles_Estimes_M2":    combles,
            "Score":                 score(combles),
            "Statut_Appel":          "",
        })

    df = pd.DataFrame(rows, columns=COLONNES_SORTIE)
    df.sort_values("Score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  POSTESPAGNE — Vivienda Planta 01 Puerta 01")
    print("  Clase Urbano | Uso Residencial | Adresse + Surfaces")
    print("=" * 60)

    cat_file = sys.argv[1] if len(sys.argv) > 1 else \
               input("\nChemin vers le fichier .CAT : ").strip().strip('"')

    if not os.path.isfile(cat_file):
        print(f"Erreur : fichier introuvable -> {cat_file}")
        sys.exit(1)

    df = parse_cat(cat_file)
    total = len(df)

    if df.empty:
        print("\nAucune propriete qualifiee.")
        sys.exit(0)

    print(f"\n{total:,} proprietes qualifiees (VIV Planta01 Puerta01)")
    print("\nApercu :")
    print(df.head(5).to_string(index=False))

    print("\nRepartition des scores :")
    for s in range(5, 0, -1):
        n = (df["Score"] == s).sum()
        bar = "█" * min(n * 30 // max(total, 1), 30)
        print(f"  Score {s} : {n:>6,}  {bar}")

    csv_out = os.path.splitext(os.path.basename(cat_file))[0] + "_vivienda01.csv"
    df.to_csv(csv_out, index=False)
    print(f"\nCSV : {csv_out}")
    print("\nTermine.")


if __name__ == "__main__":
    main()
