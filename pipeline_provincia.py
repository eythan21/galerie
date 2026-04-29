#!/usr/bin/env python3
"""
pipeline_provincia.py - Scan complet d'une province espagnole.

Phase 1: INSPIRE pour toutes les communes de la province
Phase 2: OVC verif VIVIENDA 01/01 sur tous les candidats
Sortie : <provincia>_provincia_vivienda01.csv

Usage:
    python3 pipeline_provincia.py Zamora
    python3 pipeline_provincia.py 49        # par code province (2 chiffres)

Reprenable : si interrompu (Ctrl+C, quota OVC, crash), relance la meme
commande -> reprend au checkpoint le plus recent.
"""

import sys, re, json, time
import pandas as pd
from pathlib import Path
from tqdm import tqdm

from descargador import (
    PROV_NOM, ATOM_BU, COLONNES,
    SESSION, get_entries, traiter_ville,
)
from verifier_ovc import (
    ovc_verifier, COLONNES_OUT, score as score_ovc, OVC_DELAY,
)

PROV_CODES = {v.upper(): k for k, v in PROV_NOM.items()}


def enumerate_municipios(prov_code: str):
    """Liste toutes les communes d'une province via l'ATOM provincial."""
    entries = get_entries(ATOM_BU)
    for e in entries:
        for href, _ in e["links"]:
            if f"/{prov_code}/" in href and href.endswith(".xml"):
                muni_entries = get_entries(href)
                names = []
                seen = set()
                for me in muni_entries:
                    title = me["title"].strip()
                    m = re.match(r"\d+-(.+?)(?:\s+buildings|\s+addresses|\s*$)", title, re.I)
                    if not m:
                        continue
                    name = m.group(1).strip()
                    if name.upper() in seen:
                        continue
                    seen.add(name.upper())
                    names.append(name)
                return names
    return []


def resolve_province(arg: str):
    arg = arg.strip()
    if arg.isdigit():
        code = arg.zfill(2)
        return code, PROV_NOM.get(code, code)
    code = PROV_CODES.get(arg.upper())
    if not code:
        print(f"Province inconnue : {arg}")
        print(f"Disponibles : {', '.join(sorted(PROV_NOM.values()))}")
        sys.exit(1)
    return code, PROV_NOM[code]


def phase_inspire(prov_code: str, prov_nom: str):
    cp = Path(f"cp_provincia_{prov_code}_inspire.json")
    state = json.loads(cp.read_text()) if cp.exists() else {"done": [], "rows": []}
    done = set(state["done"])
    rows = state["rows"]

    print(f"\n{'='*60}")
    print(f"  Phase 1 : INSPIRE - province de {prov_nom}")
    print(f"{'='*60}")

    print("Enumeration des communes...")
    municipios = enumerate_municipios(prov_code)
    print(f"  {len(municipios)} communes trouvees\n")

    pending = [m for m in municipios if m.upper() not in done]
    if not pending:
        print(f"  -> deja toutes traitees ({len(rows)} candidats accumules)")
    else:
        print(f"  {len(pending)} a traiter, {len(done)} deja faites\n")

        try:
            for city in tqdm(pending, unit="commune"):
                try:
                    new_rows, _ = traiter_ville(city, prov_code)
                    rows.extend(new_rows)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"  [{city}] erreur : {str(e)[:60]}")
                done.add(city.upper())
                if len(done) % 5 == 0:
                    cp.write_text(json.dumps({"done": list(done), "rows": rows}))
        except KeyboardInterrupt:
            cp.write_text(json.dumps({"done": list(done), "rows": rows}))
            print(f"\nInterrompu - {len(rows)} candidats sauvegardes. Relance pour reprendre.")
            sys.exit(0)

        cp.write_text(json.dumps({"done": list(done), "rows": rows}))

    if not rows:
        print("\nAucun candidat INSPIRE.")
        return None

    df = pd.DataFrame(rows, columns=COLONNES)
    df.drop_duplicates("Referencia_Catastral", inplace=True)
    df.sort_values("Score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)

    csv_inspire = f"{prov_nom.lower()}_provincia_prospects.csv"
    df.to_csv(csv_inspire, index=False)
    print(f"\n  -> {csv_inspire} ({len(df):,} candidats uniques)")
    return df


def phase_ovc(df: pd.DataFrame, prov_code: str, prov_nom: str):
    cp = Path(f"cp_provincia_{prov_code}_ovc.json")
    state = json.loads(cp.read_text()) if cp.exists() else {"done": [], "rows": []}
    done = set(state["done"])
    rows = state["rows"]

    print(f"\n{'='*60}")
    print(f"  Phase 2 : OVC verif VIVIENDA 01/01")
    print(f"{'='*60}")

    pending = df[~df["Referencia_Catastral"].astype(str).isin(done)]
    duree = int(len(pending) * OVC_DELAY / 60)
    print(f"\n  {len(pending):,} a verifier (~{duree} min) - {len(done):,} deja faits")
    print("  Ctrl+C pour interrompre - reprenable\n")

    try:
        for _, row in tqdm(pending.iterrows(), total=len(pending), unit="prop"):
            ref = str(row["Referencia_Catastral"])
            data = ovc_verifier(ref, str(row["Provincia"]), str(row["Municipio"]))

            if data.get("_quota"):
                cp.write_text(json.dumps({"done": list(done), "rows": rows}))
                print(f"\n[!] Quota OVC atteint - change de VPN puis relance la meme commande")
                print(f"    Sauvegarde : {len(rows)} maisons confirmees jusqu'ici")
                sys.exit(0)

            done.add(ref)

            if data:
                rows.append({
                    "Referencia_Catastral":  ref,
                    "Provincia":             data["prov"] or str(row.get("Provincia", "")),
                    "Municipio":             data["muni"] or str(row.get("Municipio", "")),
                    "Tipo_Via":              data["tipo_via"],
                    "Calle":                 data["calle"],
                    "Numero":                data["numero"],
                    "Bloque":                data["bloque"],
                    "Escalera":              data["escalera"],
                    "Planta":                "01",
                    "Puerta":                "01",
                    "CP":                    data["cp"],
                    "Direccion_Complete":    data["direccion"],
                    "Ano":                   data["ano"],
                    "Surf_Total_M2":         data["surf_total"],
                    "Surf_VIV_RDC_M2":       data["viv_rdc"],
                    "Surf_VIV_1erEtage_M2":  data["viv_e1"],
                    "Surf_Garage_M2":        data["surf_gar"],
                    "Surf_Cave_M2":          data["surf_cave"],
                    "Combles_Reels_M2":      data["combles"],
                    "Score":                 score_ovc(data["combles"]),
                    "Statut_Appel":          "",
                })

            if len(done) % 50 == 0:
                cp.write_text(json.dumps({"done": list(done), "rows": rows}))
    except KeyboardInterrupt:
        cp.write_text(json.dumps({"done": list(done), "rows": rows}))
        print(f"\nInterrompu - {len(rows)} maisons sauvegardees. Relance pour reprendre.")
        sys.exit(0)

    cp.write_text(json.dumps({"done": list(done), "rows": rows}))

    if not rows:
        print("\nAucune maison VIVIENDA 01/01 trouvee.")
        return

    df_out = pd.DataFrame(rows, columns=COLONNES_OUT)
    df_out.sort_values("Score", ascending=False, inplace=True)
    df_out.reset_index(drop=True, inplace=True)

    csv_final = f"{prov_nom.lower()}_provincia_vivienda01.csv"
    df_out.to_csv(csv_final, index=False)

    print(f"\n{'='*60}")
    print(f"  TERMINE - province de {prov_nom}")
    print(f"{'='*60}")
    print(f"  {len(df_out):,} maisons VIVIENDA Planta=01 Puerta=01 confirmees")
    print(f"\n  Repartition scores :")
    for s in range(5, 0, -1):
        n = (df_out["Score"] == s).sum()
        bar = "#" * min(n * 30 // max(len(df_out), 1), 30)
        print(f"    Score {s} : {n:>5,}  {bar}")
    print(f"\n  CSV : {csv_final}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    prov_code, prov_nom = resolve_province(sys.argv[1])
    print(f"\nPipeline complet : province de {prov_nom} ({prov_code})\n")

    df = phase_inspire(prov_code, prov_nom)

    if df is None:
        return

    phase_ovc(df, prov_code, prov_nom)


if __name__ == "__main__":
    main()
