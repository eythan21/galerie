#!/usr/bin/env python3
"""
postespagne.py — Cadastre Espagnol (.CAT) → Google Sheets
Qualification Isolation 1€: Maisons individuelles (Vivienda Unifamiliar)
Calcul exact des combles via les codes usage garage/cave du fichier CAT.

Usage:
    python3 postespagne.py
    python3 postespagne.py mon_fichier.CAT
"""

import sys, os, re
import pandas as pd
import pygsheets
from tqdm import tqdm

# ─── CONFIGURATION RAPIDE ────────────────────────────────────────────────────
SHEET_URL   = ""   # ex: "https://docs.google.com/spreadsheets/d/XXXX..."
CREDENTIALS = ""   # ex: "credentials.json"

ANNEE_MIN, ANNEE_MAX = 1960, 2006
ETAGES_MAX = 2

COLONNES_SORTIE = [
    "Referencia_Catastral", "Calle", "Numero", "CP", "Municipio", "Ano",
    "Surface_Totale_M2", "Surface_Habitable_M2", "Surface_Garage_M2",
    "Surface_Cave_M2", "Combles_Nets_M2", "Score", "Statut_Appel",
]

# ─── FORMAT FIXE DU FICHIER .CAT ─────────────────────────────────────────────
# Format SIDPA — Sede Electronica del Catastro (positions 0-indexed)

# Tipo 11 — FINCA (adresse postale)
T11 = {
    "ref_catastral": slice(9, 23),
    "nombre_via":    slice(100, 150),
    "numero":        slice(150, 154),
    "cod_postal":    slice(162, 167),
    "municipio":     slice(167, 207),
}

# Tipo 13 — LOCAL / BIEN INMUEBLE
T13 = {
    "ref_catastral":  slice(9, 23),
    "naturaleza":     slice(23, 24),   # U=Urbana
    "uso":            slice(24, 26),   # 01=Residencial
    "anyo":           slice(30, 34),
    "plantas_sobre":  slice(38, 41),
    "superficie":     slice(44, 51),   # Surface totale construite m²
    "tipo_finca":     slice(55, 57),   # VU=Unifamiliar
}

# Tipo 15 — CONSTRUCCION (detail de chaque espace dans le bien)
# ⚠️  Ajustez si vos valeurs semblent incorrectes
T15 = {
    "ref_catastral": slice(9, 23),
    "calificacion":  slice(43, 45),   # Code usage: VV, GA, TR, AL...
    "superficie":    slice(45, 52),   # Surface de cet espace en m²
}

# Codes usage par categorie (plusieurs variantes selon version du fichier)
USOS_HABITABLE = {"VV", "VI", "VT", "VP", "01", "1", "VIV"}
USOS_GARAGE    = {"GA", "GAR", "03", "3", "GR"}
USOS_CAVE      = {"TR", "TRS", "ALM", "AL", "DE", "08", "8", "TRO"}

USAGE_RESIDENCIAL = {"01", "1", "R"}
TIPO_UNIFAMILIAR  = {"VU", "U", "V1", "UF", "1U"}

# ─── SCORING ─────────────────────────────────────────────────────────────────

def calculer_score(combles_nets: float) -> int:
    """Score 1-5 base sur la surface combles nette reelle."""
    if combles_nets >= 100: return 5   # Excellent — gros contrat
    if combles_nets >= 70:  return 4   # Tres bien
    if combles_nets >= 45:  return 3   # Bien
    if combles_nets >= 25:  return 2   # Moyen
    return 1                           # Faible potentiel

# ─── PARSING ─────────────────────────────────────────────────────────────────

def parse_cat_file(filepath: str) -> pd.DataFrame:
    print("\nComptage des lignes...")
    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        total = sum(1 for _ in f)
    print(f"-> {total:,} lignes detectees.")

    adresses:  dict = {}   # ref → {Calle, Numero, CP, Municipio}
    biens:     dict = {}   # ref → {anyo, superficie, ...}  (Tipo 13)
    surf_hab:  dict = {}   # ref → surface habitable (Tipo 15 VV)
    surf_gar:  dict = {}   # ref → surface garage    (Tipo 15 GA)
    surf_cave: dict = {}   # ref → surface cave      (Tipo 15 TR)

    with open(filepath, "r", encoding="latin-1", errors="replace") as fh:
        for line in tqdm(fh, total=total, desc="Extraction", unit="lig"):
            if len(line) < 10:
                continue
            tipo = line[:2]

            # ── Adresse (Tipo 11) ─────────────────────────────────────────
            if tipo == "11":
                ref = line[T11["ref_catastral"]].strip()
                if ref:
                    adresses[ref] = {
                        "Calle":     line[T11["nombre_via"]].strip().title(),
                        "Numero":    line[T11["numero"]].strip(),
                        "CP":        line[T11["cod_postal"]].strip(),
                        "Municipio": line[T11["municipio"]].strip().title(),
                    }

            # ── Bien immobilier (Tipo 13) ─────────────────────────────────
            elif tipo == "13":
                if len(line) < 60:
                    continue
                if line[T13["naturaleza"]].strip().upper() != "U":
                    continue
                if line[T13["uso"]].strip() not in USAGE_RESIDENCIAL:
                    continue
                if line[T13["tipo_finca"]].strip().upper() not in TIPO_UNIFAMILIAR:
                    continue
                try:
                    anyo       = int(line[T13["anyo"]].strip()        or 0)
                    plantas    = int(line[T13["plantas_sobre"]].strip() or 0)
                    superficie = float(line[T13["superficie"]].strip() or 0)
                except ValueError:
                    continue
                if not (ANNEE_MIN <= anyo <= ANNEE_MAX): continue
                if plantas > ETAGES_MAX:                 continue
                if superficie <= 0:                      continue
                ref = line[T13["ref_catastral"]].strip()
                biens[ref] = {"anyo": anyo, "superficie": superficie}

            # ── Detail construction (Tipo 15) — garage, cave, habitable ───
            elif tipo == "15":
                if len(line) < 52:
                    continue
                ref   = line[T15["ref_catastral"]].strip()
                calif = line[T15["calificacion"]].strip().upper()
                try:
                    surf = float(line[T15["superficie"]].strip() or 0)
                except ValueError:
                    continue
                if surf <= 0 or not ref:
                    continue

                if calif in USOS_HABITABLE:
                    surf_hab[ref]  = surf_hab.get(ref, 0)  + surf
                elif calif in USOS_GARAGE:
                    surf_gar[ref]  = surf_gar.get(ref, 0)  + surf
                elif calif in USOS_CAVE:
                    surf_cave[ref] = surf_cave.get(ref, 0) + surf

    if not biens:
        print("\nAucune propriete qualifiee trouvee.")
        return pd.DataFrame(columns=COLONNES_SORTIE)

    # ── Construire le DataFrame ───────────────────────────────────────────────
    rows = []
    for ref, b in biens.items():
        surf_totale  = b["superficie"]
        s_hab  = round(surf_hab.get(ref, 0),  1)
        s_gar  = round(surf_gar.get(ref, 0),  1)
        s_cave = round(surf_cave.get(ref, 0), 1)

        # Combles nets = total - habitable - garage - cave
        combles = round(max(0, surf_totale - s_hab - s_gar - s_cave), 1)

        # Si Tipo 15 absent pour ce bien → estimation classique
        if s_hab == 0 and s_gar == 0 and s_cave == 0:
            combles = round(surf_totale * 0.20, 1)   # 20% de la surface totale

        addr = adresses.get(ref, {})
        rows.append({
            "Referencia_Catastral":  ref,
            "Calle":                 addr.get("Calle", ""),
            "Numero":                addr.get("Numero", ""),
            "CP":                    addr.get("CP", ""),
            "Municipio":             addr.get("Municipio", ""),
            "Ano":                   b["anyo"],
            "Surface_Totale_M2":     round(surf_totale, 1),
            "Surface_Habitable_M2":  s_hab,
            "Surface_Garage_M2":     s_gar,
            "Surface_Cave_M2":       s_cave,
            "Combles_Nets_M2":       combles,
            "Score":                 calculer_score(combles),
            "Statut_Appel":          "",
        })

    df = pd.DataFrame(rows, columns=COLONNES_SORTIE)

    # Trier par Score decroissant (les meilleurs en premier)
    df.sort_values("Score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)

    return df

# ─── GOOGLE SHEETS ───────────────────────────────────────────────────────────

def export_to_sheets(df: pd.DataFrame, url: str, creds: str) -> None:
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

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  POSTESPAGNE — Cadastre .CAT -> Google Sheets")
    print("  Qualification Isolation 1€(Vivienda Unifamiliar)")
    print("=" * 60)

    cat_file = sys.argv[1] if len(sys.argv) > 1 else input("\nChemin vers le fichier .CAT : ").strip().strip('"')
    if not os.path.isfile(cat_file):
        print(f"Erreur : fichier introuvable -> {cat_file}")
        sys.exit(1)

    df = parse_cat_file(cat_file)
    total = len(df)
    print(f"\n{total:,} proprietes qualifiees.")

    if df.empty:
        sys.exit(0)

    print("\nApercu (triees par Score) :")
    print(df.head(5).to_string(index=False))

    # Repartition des scores
    print("\nRepartition :")
    for s in range(5, 0, -1):
        n = (df["Score"] == s).sum()
        bar = "█" * (n * 20 // max(total, 1))
        print(f"  Score {s} : {n:>5} proprietes  {bar}")

    # Combien exporter
    print(f"\nCombien exporter ? (max {total:,} — Entree = tout)")
    choix = input("  Nombre : ").strip()
    if choix:
        try:
            df = df.head(max(1, min(int(choix), total)))
        except ValueError:
            pass

    # Export CSV
    csv_out = os.path.splitext(os.path.basename(cat_file))[0] + "_qualifies.csv"
    df.to_csv(csv_out, index=False)
    print(f"\nCSV cree : {csv_out}")

    # Export Google Sheets (optionnel)
    creds_file = CREDENTIALS or "credentials.json"
    if os.path.isfile(creds_file):
        sheet_input = SHEET_URL or input("URL Google Sheet (Entree pour ignorer) : ").strip()
        if sheet_input:
            export_to_sheets(df, sheet_input, creds_file)

    print("\nTermine.")


if __name__ == "__main__":
    main()
