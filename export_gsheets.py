#!/usr/bin/env python3
"""
export_gsheets.py — Export CSV cadastre vers Google Sheets.

Usage:
    python3 export_gsheets.py zamora_provincia_vivienda01.csv "Zamora V2"
    python3 export_gsheets.py zamora_provincia_vivienda01.csv        # nom auto
    python3 export_gsheets.py zamora_provincia_vivienda01.csv --csv  # CSV renomme aussi

Auth Google :
    Option 1 (recommande) : Compte de service
        - Cree un projet sur console.cloud.google.com
        - Active "Google Sheets API"
        - Cree un compte de service -> telecharge JSON -> save as credentials.json
        - Partage le Google Sheet avec l email du compte de service
        export GOOGLE_CREDENTIALS_JSON=/chemin/vers/credentials.json

    Option 2 : OAuth2 (navigateur)
        - Cree des identifiants OAuth2 Desktop -> telecharge JSON -> save as client_secrets.json
        - La premiere fois ouvre le navigateur pour autoriser
        export GOOGLE_CLIENT_SECRETS=/chemin/vers/client_secrets.json
"""

import sys, os, json
import pandas as pd
from pathlib import Path

CREDS_ENV  = "GOOGLE_CREDENTIALS_JSON"
OAUTH_ENV  = "GOOGLE_CLIENT_SECRETS"
CREDS_FILE = "credentials.json"
OAUTH_FILE = "client_secrets.json"


def load_pygsheets():
    try:
        import pygsheets
        return pygsheets
    except ImportError:
        print("[!] pip install pygsheets google-auth google-auth-oauthlib")
        sys.exit(1)


def get_client(pygsheets):
    creds_path = os.environ.get(CREDS_ENV, CREDS_FILE)
    oauth_path = os.environ.get(OAUTH_ENV, OAUTH_FILE)

    if Path(creds_path).exists():
        print(f"  Auth : compte de service ({creds_path})")
        return pygsheets.authorize(service_account_file=creds_path)

    if Path(oauth_path).exists():
        print(f"  Auth : OAuth2 ({oauth_path})")
        return pygsheets.authorize(client_secret=oauth_path)

    print(f"""[!] Aucun fichier d'authentification trouve.

Methode 1 - Compte de service (recommande pour scripts):
  1. console.cloud.google.com -> nouveau projet -> activer "Google Sheets API"
  2. "Credentials" -> "Create credentials" -> "Service account"
  3. Telecharge le JSON -> sauvegarde en 'credentials.json' ici
  4. Partage ton sheet Google avec l'email du compte de service

Methode 2 - OAuth2 (pour usage personnel):
  1. console.cloud.google.com -> "Credentials" -> "OAuth client ID" -> Desktop app
  2. Telecharge le JSON -> sauvegarde en 'client_secrets.json' ici

Ou definit la variable d'env:
  export {CREDS_ENV}=/chemin/vers/service_account.json
""")
    sys.exit(1)


def format_header(wks, ncols: int):
    """Formate la ligne d'entete : gras, fond bleu fonce, texte blanc."""
    try:
        header_range = wks.get_values(
            start=(1, 1), end=(1, ncols),
            returnas="range",
        )
        fmt = pygsheets.format_structure.CellFormatMixin
        from pygsheets.custom_types import HorizontalAlignment
        import pygsheets.format_structure as pf

        model_cell = wks.cell((1, 1))
        model_cell.set_text_format("bold", True)
        model_cell.color = (0.13, 0.27, 0.49, 1)  # bleu fonce
        model_cell.set_text_format("foregroundColor", {"red": 1, "green": 1, "blue": 1})
        wks.apply_format((1, 1), (1, ncols), model_cell)
    except Exception:
        pass  # formatting est optionnel


def freeze_and_filter(wks, nrows: int, ncols: int):
    """Fige la premiere ligne et active les filtres."""
    try:
        wks.frozen_rows = 1
    except Exception:
        pass
    try:
        requests = [{
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": wks.id,
                        "startRowIndex": 0,
                        "endRowIndex": nrows + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": ncols,
                    }
                }
            }
        }]
        wks.spreadsheet.custom_request(requests, None)
    except Exception:
        pass


def col_widths(wks, df: pd.DataFrame):
    """Ajuste la largeur des colonnes importantes."""
    widths = {
        "Referencia_Catastral": 160,
        "Direccion_Complete":   280,
        "Calle":                180,
        "Municipio":            120,
        "Provincia":            120,
        "Statut_Appel":         140,
        "Notes_IA":             200,
        "URL_Google_Maps":      50,
        "URL_Catastro_Carto":   50,
    }
    cols = list(df.columns)
    try:
        requests = []
        for name, px in widths.items():
            if name in cols:
                idx = cols.index(name)
                requests.append({
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": wks.id,
                            "dimension": "COLUMNS",
                            "startIndex": idx,
                            "endIndex": idx + 1,
                        },
                        "properties": {"pixelSize": px},
                        "fields": "pixelSize",
                    }
                })
        if requests:
            wks.spreadsheet.custom_request(requests, None)
    except Exception:
        pass


def score_colors(wks, df: pd.DataFrame):
    """Colorie la colonne Score (5=vert fonce ... 1=rouge)."""
    if "Score" not in df.columns:
        return
    col_idx = list(df.columns).index("Score") + 1
    palette = {
        5: (0.20, 0.66, 0.33, 1),
        4: (0.60, 0.86, 0.47, 1),
        3: (1.00, 0.95, 0.41, 1),
        2: (1.00, 0.74, 0.20, 1),
        1: (0.96, 0.40, 0.33, 1),
    }
    try:
        requests = []
        for row_idx, score_val in enumerate(df["Score"], start=2):
            try:
                s = int(score_val)
            except Exception:
                continue
            color = palette.get(s)
            if not color:
                continue
            r, g, b, a = color
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": wks.id,
                        "startRowIndex": row_idx - 1,
                        "endRowIndex": row_idx,
                        "startColumnIndex": col_idx - 1,
                        "endColumnIndex": col_idx,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": r, "green": g, "blue": b, "alpha": a},
                            "textFormat": {"bold": True},
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })
        if requests:
            for chunk in [requests[i:i+500] for i in range(0, len(requests), 500)]:
                wks.spreadsheet.custom_request(chunk, None)
    except Exception:
        pass


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    csv_path = args[0]
    also_csv = "--csv" in args
    args_clean = [a for a in args if a != "--csv"]

    sheet_name = args_clean[1] if len(args_clean) > 1 else Path(csv_path).stem.replace("_", " ").title()

    if not Path(csv_path).exists():
        print(f"[!] Fichier introuvable : {csv_path}")
        sys.exit(1)

    df = pd.read_csv(csv_path)
    print(f"\n  CSV source  : {csv_path}  ({len(df):,} lignes, {len(df.columns)} colonnes)")
    print(f"  Sheet name  : {sheet_name}")

    if also_csv:
        csv_out = Path(csv_path).parent / f"{sheet_name}.csv"
        df.to_csv(csv_out, index=False)
        print(f"  CSV copie   : {csv_out}")

    pygsheets = load_pygsheets()
    gc = get_client(pygsheets)

    try:
        sh = gc.open(sheet_name)
        print(f"  -> Spreadsheet existant trouve : {sheet_name}")
    except Exception:
        sh = gc.create(sheet_name)
        print(f"  -> Nouveau spreadsheet cree : {sheet_name}")

    wks = sh.sheet1
    wks.title = "Prospects"

    wks.clear()
    wks.set_dataframe(df, start=(1, 1), copy_index=False, copy_head=True, nan="")

    nrows, ncols = df.shape
    print(f"\n  Upload : {nrows:,} lignes x {ncols} colonnes... ", end="", flush=True)
    print("OK")

    print("  Formatage...", end="", flush=True)
    format_header(wks, ncols)
    freeze_and_filter(wks, nrows, ncols)
    col_widths(wks, df)
    score_colors(wks, df)
    print(" OK")

    url = f"https://docs.google.com/spreadsheets/d/{sh.id}"
    print(f"\n  Google Sheet : {url}")
    print(f"  Partage avec l equipe via 'Partager' en haut a droite du sheet.\n")


if __name__ == "__main__":
    main()
