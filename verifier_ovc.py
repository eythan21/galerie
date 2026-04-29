#!/usr/bin/env python3
"""
verifier_ovc.py — Vérifie VIVIENDA Planta=01 Puerta=01 via OVC
Prend le CSV de prospects INSPIRE, vérifie les meilleurs via OVC API.

Usage:
    python3 verifier_ovc.py zamora_prospects.csv        # top 100
    python3 verifier_ovc.py zamora_prospects.csv 50     # top 50
    python3 verifier_ovc.py zamora_prospects.csv all    # tous
"""

import sys, time, requests, pandas as pd
from xml.etree import ElementTree as ET
from tqdm import tqdm

OVC_URL   = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCallejero.asmx/Consulta_DNPRC"
OVC_DELAY = 2.0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

COLONNES_OUT = [
    "Referencia_Catastral","Provincia","Municipio","Calle","Numero","CP","Ano",
    "Surf_Total_M2","Surf_VIV_RDC_M2","Surf_VIV_1erEtage_M2",
    "Surf_Garage_M2","Surf_Cave_M2","Combles_Reels_M2","Score","Statut_Appel",
]

def ovc_verifier(ref, provincia, municipio):
    time.sleep(OVC_DELAY)
    try:
        r = SESSION.get(OVC_URL, params={"Provincia": provincia, "Municipio": municipio, "RC": ref}, timeout=20)
        if r.status_code == 403 or "limite" in r.text.lower():
            return {"_quota": True}
        if r.status_code != 200:
            return {}

        root = ET.fromstring(r.content)

        def val(tag):
            for el in root.iter():
                if el.tag.split("}")[-1] == tag:
                    return (el.text or "").strip()
            return ""

        if val("cn").upper() != "UR": return {}
        if "residencial" not in val("luso").lower(): return {}

        ano = 0
        try: ano = int(val("ant"))
        except: pass
        surf_total = 0.0
        try: surf_total = float(val("sfc") or 0)
        except: pass

        viv_rdc = viv_e1 = surf_gar = surf_cave = 0.0
        has_viv01 = False

        for cons in root.iter():
            if cons.tag.split("}")[-1] != "cons":
                continue
            lcd = pt = pu = ""
            stl = 0.0
            for ch in cons.iter():
                t = ch.tag.split("}")[-1]
                if   t == "lcd": lcd = (ch.text or "").strip().upper()
                elif t == "pt":  pt  = (ch.text or "").strip()
                elif t == "pu":  pu  = (ch.text or "").strip()
                elif t == "stl":
                    try: stl = float(ch.text or 0)
                    except: pass
            if stl <= 0: continue
            if "VIVIENDA" in lcd:
                if pt in ("00","0","PB","BJ"):
                    viv_rdc += stl
                elif pt == "01":
                    viv_e1 += stl
                    if pu == "01":
                        has_viv01 = True
            elif any(g in lcd for g in ("APARCAMIENTO","GARAJE","GARAGE")):
                surf_gar += stl
            elif any(c in lcd for c in ("ALMACEN","TRASTERO","SOPORT","BODEGA","DEPOSITO")):
                surf_cave += stl

        if not has_viv01:
            return {}

        return {
            "calle":      val("nv").title(),
            "numero":     val("pnp"),
            "cp":         val("dp"),
            "muni":       val("nm").title(),
            "ano":        ano,
            "surf_total": round(surf_total, 1),
            "viv_rdc":    round(viv_rdc, 1),
            "viv_e1":     round(viv_e1, 1),
            "surf_gar":   round(surf_gar, 1),
            "surf_cave":  round(surf_cave, 1),
            "combles":    round(viv_e1 * 0.90, 1),
        }
    except Exception:
        return {}

def score(combles):
    if combles >= 100: return 5
    if combles >= 70:  return 4
    if combles >= 45:  return 3
    if combles >= 25:  return 2
    return 1

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 verifier_ovc.py fichier.csv [nb_max|all]")
        sys.exit(1)

    csv_in = sys.argv[1]
    nb_arg = sys.argv[2] if len(sys.argv) > 2 else "100"

    df = pd.read_csv(csv_in)
    df.sort_values("Score", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)

    to_check = df if nb_arg.lower() == "all" else df.head(int(nb_arg))

    duree = int(len(to_check) * OVC_DELAY / 60)
    print(f"Verification OVC : {len(to_check)} proprietes (~{duree} min)")
    print("Ctrl+C pour interrompre\n")

    rows = []
    try:
        for _, row in tqdm(to_check.iterrows(), total=len(to_check), unit="prop"):
            ref       = str(row["Referencia_Catastral"])
            provincia = str(row["Provincia"])
            municipio = str(row["Municipio"])

            data = ovc_verifier(ref, provincia, municipio)

            if data.get("_quota"):
                print("\n[!] Quota OVC — change VPN et relance")
                break

            if not data:
                continue

            rows.append({
                "Referencia_Catastral":  ref,
                "Provincia":             provincia,
                "Municipio":             data["muni"] or municipio,
                "Calle":                 data["calle"],
                "Numero":                data["numero"],
                "CP":                    data["cp"],
                "Ano":                   data["ano"],
                "Surf_Total_M2":         data["surf_total"],
                "Surf_VIV_RDC_M2":       data["viv_rdc"],
                "Surf_VIV_1erEtage_M2":  data["viv_e1"],
                "Surf_Garage_M2":        data["surf_gar"],
                "Surf_Cave_M2":          data["surf_cave"],
                "Combles_Reels_M2":      data["combles"],
                "Score":                 score(data["combles"]),
                "Statut_Appel":          "",
            })

    except KeyboardInterrupt:
        print(f"\nInterrompu — {len(rows)} qualifies jusqu'ici.")

    if not rows:
        print("\nAucune propriete VIVIENDA 01/01 trouvee.")
        return

    df_out = pd.DataFrame(rows, columns=COLONNES_OUT)
    df_out.sort_values("Score", ascending=False, inplace=True)
    df_out.reset_index(drop=True, inplace=True)

    csv_out = csv_in.replace(".csv", "_vivienda01.csv")
    df_out.to_csv(csv_out, index=False)

    total = len(df_out)
    print(f"\n{total} maisons VIVIENDA Planta=01 Puerta=01 confirmees")
    print(df_out.head(5).to_string(index=False))
    print(f"\nCSV : {csv_out}")

if __name__ == "__main__":
    main()
