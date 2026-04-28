#!/usr/bin/env python3
"""
postespagne.py — Cadastre Espagnol → Google Sheets
Qualification Isolation 1€ : Maisons individuelles (Vivienda Unifamiliar)

Usage:
    python postespagne.py
    python postespagne.py mon_fichier.CAT
"""

import sys
import os
import re
import pandas as pd
import pygsheets
from tqdm import tqdm

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
ANNEE_MIN, ANNEE_MAX = 1960, 2006
ETAGES_MAX = 2          # Bajo + 1 étage = 2 au maximum
CHUNK_SIZE = 50_000     # Lignes chargées en mémoire par lot

COLONNES_SORTIE = [
    "Referencia_Catastral",
    "Calle",
    "Numero",
    "CP",
    "Municipio",
    "Año",
    "Estimation_M2_Combles",
    "Statut_Appel",
]

# ─── FORMAT FIXE DU FICHIER .CAT ─────────────────────────────────────────────
# Format SIDPA — Sede Electrónica del Catastro (positions 0-indexed)
# ⚠️  Si vos valeurs semblent décalées, ajustez les slices ci-dessous
#     en ouvrant le .CAT dans un éditeur et en comptant les colonnes.

# Tipo 11 — FINCA (adresse postale du bien)
T11 = {
    "ref_catastral": slice(9, 23),    # Référence cadastrale (14 chars)
    "nombre_via":    slice(100, 150), # Nom de la rue
    "numero":        slice(150, 154), # Numéro de rue
    "cod_postal":    slice(162, 167), # Code postal (5 chiffres)
    "municipio":     slice(167, 207), # Nom de la commune
}

# Tipo 13 — LOCAL / BIEN INMUEBLE (caractéristiques du bien)
T13 = {
    "ref_catastral":  slice(9, 23),   # Référence cadastrale
    "naturaleza":     slice(23, 24),  # U=Urbana, R=Rústica
    "uso":            slice(24, 26),  # 01=Residencial
    "anyo":           slice(30, 34),  # Année de construction
    "plantas_sobre":  slice(38, 41),  # Étages au-dessus du sol
    "plantas_bajo":   slice(41, 44),  # Étages en sous-sol
    "superficie":     slice(44, 51),  # Surface totale construite (m²)
    "tipo_finca":     slice(55, 57),  # VU=Unifamiliar, VB=Bloc résidentiel…
}

# Codes acceptés pour usage résidentiel et type unifamiliar
# (varient selon la version du fichier CAT — complétez si besoin)
USAGE_RESIDENCIAL  = {"01", "1", "R"}
TIPO_UNIFAMILIAR   = {"VU", "U", "V1", "UF", "1U"}

# ─── PARSING ─────────────────────────────────────────────────────────────────

def _compter_lignes(filepath: str) -> int:
    with open(filepath, "r", encoding="latin-1", errors="replace") as f:
        return sum(1 for _ in f)


def parse_cat_file(filepath: str) -> pd.DataFrame:
    """
    Lit le fichier .CAT ligne par ligne et retourne un DataFrame
    contenant uniquement les biens qualifiés (filtres isolation 1€).
    """
    print(f"\nComptage des lignes du fichier…")
    total = _compter_lignes(filepath)
    print(f"→ {total:,} lignes détectées.")

    adresses: dict[str, dict] = {}   # ref_catastral → adresse (Tipo 11)
    resultats: list[dict]    = []    # biens qualifiés (Tipo 13)

    with open(filepath, "r", encoding="latin-1", errors="replace") as fh:
        for line in tqdm(fh, total=total, desc="Extraction", unit="lig"):
            if len(line) < 10:
                continue
            tipo = line[:2]

            # ── Enregistrement adresse ────────────────────────────────────
            if tipo == "11":
                ref = line[T11["ref_catastral"]].strip()
                if ref:
                    adresses[ref] = {
                        "Calle":     line[T11["nombre_via"]].strip().title(),
                        "Numero":    line[T11["numero"]].strip(),
                        "CP":        line[T11["cod_postal"]].strip(),
                        "Municipio": line[T11["municipio"]].strip().title(),
                    }

            # ── Enregistrement bien immobilier ────────────────────────────
            elif tipo == "13":
                if len(line) < 60:
                    continue

                # Filtre 1 — Urbano
                naturaleza = line[T13["naturaleza"]].strip().upper()
                if naturaleza != "U":
                    continue

                # Filtre 2 — Usage résidentiel
                uso = line[T13["uso"]].strip()
                if uso not in USAGE_RESIDENCIAL:
                    continue

                # Filtre 3 — Vivienda Unifamiliar
                tipo_finca = line[T13["tipo_finca"]].strip().upper()
                if tipo_finca not in TIPO_UNIFAMILIAR:
                    continue

                # Parsing numérique
                try:
                    anyo       = int(line[T13["anyo"]].strip()         or 0)
                    plantas    = int(line[T13["plantas_sobre"]].strip() or 0)
                    superficie = float(line[T13["superficie"]].strip()  or 0)
                except ValueError:
                    continue

                # Filtre 4 — Année 1960-2006
                if not (ANNEE_MIN <= anyo <= ANNEE_MAX):
                    continue

                # Filtre 5 — Maximum 2 étages
                if plantas > ETAGES_MAX:
                    continue

                # Données incohérentes
                if superficie <= 0:
                    continue

                ref = line[T13["ref_catastral"]].strip()
                estimation = round((superficie / max(plantas, 1)) * 1.10, 2)

                resultats.append({
                    "Referencia_Catastral": ref,
                    "Año":                  anyo,
                    "Estimation_M2_Combles": estimation,
                    "Statut_Appel":          "",
                    "_ref":                 ref,
                })

    if not resultats:
        print("\nAucune propriété qualifiée trouvée avec les filtres actuels.")
        return pd.DataFrame(columns=COLONNES_SORTIE)

    df = pd.DataFrame(resultats)

    # Jointure avec les adresses (Tipo 11)
    for col in ("Calle", "Numero", "CP", "Municipio"):
        df[col] = df["_ref"].map(lambda r, c=col: adresses.get(r, {}).get(c, ""))

    df.drop(columns=["_ref"], inplace=True)
    return df[COLONNES_SORTIE]


# ─── GOOGLE SHEETS ───────────────────────────────────────────────────────────

def _extraire_sheet_id(url_ou_id: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_ou_id)
    return m.group(1) if m else url_ou_id.strip()


def export_to_sheets(df: pd.DataFrame, url_ou_id: str, credentials_file: str) -> None:
    """Exporte df vers Google Sheets en mode Append (sans écraser l'existant)."""

    print("\nConnexion à Google Sheets…")
    gc       = pygsheets.authorize(service_file=credentials_file)
    sheet_id = _extraire_sheet_id(url_ou_id)
    sh       = gc.open_by_key(sheet_id)
    ws       = sh.sheet1

    existantes = ws.get_all_values(include_tailing_empty=False)

    if not existantes or existantes == [[]]:
        # Feuille vide → écrire l'en-tête en ligne 1
        ws.update_row(1, COLONNES_SORTIE)
        debut = 2
    else:
        # Trouver la première ligne vraiment vide après les données
        debut = len(existantes) + 1

    if df.empty:
        print("Aucune donnée à exporter.")
        return

    rows = df.fillna("").astype(str).values.tolist()
    ws.update_values(crange=f"A{debut}", values=rows)
    print(f"✓ {len(rows)} propriétés exportées (à partir de la ligne {debut}).")


# ─── AIDE GOOGLE SHEETS ──────────────────────────────────────────────────────

def afficher_aide_credentials() -> None:
    print("""
╔══════════════════════════════════════════════════════════════════╗
║   COMMENT AUTORISER L'ÉCRITURE DANS VOTRE GOOGLE SHEET          ║
╠══════════════════════════════════════════════════════════════════╣
║  1. Allez sur https://console.cloud.google.com                   ║
║  2. Créez un projet (ou sélectionnez-en un existant)             ║
║  3. APIs & Services → Bibliothèque → activez :                   ║
║       • Google Sheets API                                        ║
║       • Google Drive API                                         ║
║  4. APIs & Services → Identifiants → Créer des identifiants      ║
║     → Compte de service → téléchargez le fichier JSON           ║
║     → Renommez-le credentials.json                               ║
║  5. Copiez l'adresse email du compte de service                  ║
║     (ex: mon-bot@mon-projet.iam.gserviceaccount.com)             ║
║  6. Ouvrez votre Google Sheet → Partager → collez l'email        ║
║     → Donner le rôle Éditeur → Envoyer                          ║
╚══════════════════════════════════════════════════════════════════╝
""")


# ─── CONFIGURATION RAPIDE ────────────────────────────────────────────────────
# Collez directement vos valeurs ici pour ne plus avoir à les saisir à chaque fois.
# Laissez "" pour que le script vous les demande au lancement.

SHEET_URL      = ""   # ex: "https://docs.google.com/spreadsheets/d/XXXX..."
CREDENTIALS    = ""   # ex: "credentials.json"  ou chemin absolu


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  POSTESPAGNE — Cadastre ES → Google Sheets")
    print("  Qualification Isolation 1€ (Vivienda Unifamiliar)")
    print("=" * 60)

    # 1. Fichier CAT
    if len(sys.argv) > 1:
        cat_file = sys.argv[1]
    else:
        cat_file = input("\nChemin vers le fichier .CAT : ").strip().strip('"')

    if not os.path.isfile(cat_file):
        print(f"\nErreur : fichier introuvable → {cat_file}")
        sys.exit(1)

    # 2. Google Sheet (URL ou ID)
    sheet_input = SHEET_URL or input("URL ou ID de votre Google Sheet : ").strip()
    if not sheet_input:
        print("Erreur : URL/ID du Sheet manquant.")
        sys.exit(1)

    # 3. Fichier credentials
    creds_file = CREDENTIALS or input("Chemin vers credentials.json [credentials.json] : ").strip() or "credentials.json"

    if not os.path.isfile(creds_file):
        print(f"\nFichier credentials.json introuvable → {creds_file}")
        afficher_aide_credentials()
        sys.exit(1)

    # 4. Extraction
    df = parse_cat_file(cat_file)
    print(f"\n{len(df):,} propriétés qualifiées trouvées.")

    if not df.empty:
        print("\nAperçu des 3 premières lignes :")
        print(df.head(3).to_string(index=False))
        export_to_sheets(df, sheet_input, creds_file)

    print("\nTerminé.")


if __name__ == "__main__":
    main()
