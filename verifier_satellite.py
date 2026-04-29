#!/usr/bin/env python3
"""
verifier_satellite.py — Verification satellite des combles via Claude vision.

Pour chaque maison du CSV:
1. Recupere lat/lon via Catastro (Consulta_CPMRC)
2. Telecharge la photo aerienne PNOA (gratuit, ortho-photo 25cm)
3. Sauvegarde l image en local (satellite/{ref}.png) -> verification manuelle
4. Analyse via Claude Opus 4.7 (toit pente/plat, combles, etat)
5. Ajoute URLs Google Maps (vue satellite) + Catastro Cartografia

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 verifier_satellite.py zamora_prospects_vivienda01.csv
    python3 verifier_satellite.py zamora_prospects_vivienda01.csv --no-ai   # images uniquement
    python3 verifier_satellite.py zamora_prospects_vivienda01.csv --max 10  # limite
"""

import sys, os, time, base64, json, math, requests, pandas as pd
from xml.etree import ElementTree as ET
from urllib.parse import quote_plus
from pathlib import Path
from tqdm import tqdm

OVC_COORD_URL = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCoordenadas.asmx/Consulta_CPMRC"
PNOA_WMS      = "https://www.ign.es/wms-inspire/pnoa-ma"
SAT_DIR       = Path("satellite")
DELAY         = 1.2

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PostEspagne/1.0"})

PROMPT = """Tu analyses une photo aerienne (vue du dessus, ortho-photo) d'une maison en Espagne pour cibler un service de pose d'isolation de combles perdus.

Reponds UNIQUEMENT en JSON valide, sans texte avant/apres, sans markdown:
{
  "toit_type": "pente|plat|mixte|inconnu",
  "toit_couleur": "rouge|brun|gris|noir|autre",
  "combles_amenageables": "oui|non|incertain",
  "etat_toiture": "bon|moyen|mauvais|inconnu",
  "confiance": "haute|moyenne|basse",
  "notes": "<5-10 mots max>"
}

Criteres:
- "pente" = toit incline classique avec faitage central (combles probables sous toiture)
- "plat" = terrasse plate ou toit-terrasse (PAS de combles)
- "mixte" = combinaison pente + plat
- combles_amenageables "oui" = toit pente avec surface significative (plus de 30 m2 d'empreinte)"""


def get_coords(ref: str):
    try:
        r = SESSION.get(OVC_COORD_URL, params={
            "Provincia": "", "Municipio": "", "SRS": "EPSG:4326", "RC": ref,
        }, timeout=15)
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.content)
        x = y = None
        for el in root.iter():
            t = el.tag.split("}")[-1]
            if t == "xcen" and x is None:
                try: x = float((el.text or "").replace(",", "."))
                except: pass
            elif t == "ycen" and y is None:
                try: y = float((el.text or "").replace(",", "."))
                except: pass
        if x and y:
            return (y, x)  # (lat, lon)
    except Exception:
        pass
    return None


def download_satellite(lat: float, lon: float, out_path: Path, size: int = 512, view_m: int = 45) -> bool:
    dlat = view_m / 111000
    dlon = view_m / (111000 * max(math.cos(math.radians(lat)), 0.01))
    bbox = f"{lon-dlon},{lat-dlat},{lon+dlon},{lat+dlat}"
    try:
        r = SESSION.get(PNOA_WMS, params={
            "service": "WMS", "version": "1.1.1", "request": "GetMap",
            "layers": "OI.OrthoimageCoverage",
            "srs": "EPSG:4326",
            "bbox": bbox,
            "width": size, "height": size,
            "format": "image/png", "styles": "",
        }, timeout=30)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            out_path.write_bytes(r.content)
            return True
    except Exception:
        pass
    return False


def analyze_image(client, image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    try:
        resp = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip().rstrip("`").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "json_invalid"}
    except Exception as e:
        return {"error": str(e)[:80]}


def main():
    args = [a for a in sys.argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    csv_in = args[0]
    use_ai = "--no-ai" not in args
    max_n = None
    if "--max" in args:
        try: max_n = int(args[args.index("--max") + 1])
        except: pass

    df = pd.read_csv(csv_in)
    if max_n:
        df = df.head(max_n)

    SAT_DIR.mkdir(exist_ok=True)

    client = None
    if use_ai:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("[!] ANTHROPIC_API_KEY non defini -> mode --no-ai active automatiquement")
            use_ai = False
        else:
            try:
                import anthropic
                client = anthropic.Anthropic()
            except ImportError:
                print("[!] pip install anthropic   (puis relance)")
                sys.exit(1)

    duree = int(len(df) * (DELAY + (3 if use_ai else 0.5)) / 60)
    print(f"\n{len(df)} maisons a verifier (~{duree} min)")
    print(f"Images   : {SAT_DIR.absolute()}/")
    print(f"Analyse IA : {'oui (Claude Opus 4.7)' if use_ai else 'non (telechargement seul)'}\n")

    results = []
    try:
        for _, row in tqdm(df.iterrows(), total=len(df), unit="maison"):
            ref = str(row["Referencia_Catastral"])
            addr_full = str(row.get("Direccion_Complete", "")).strip()
            if not addr_full or addr_full == "nan":
                addr_full = f"{row.get('Calle','')} {row.get('Numero','')}, {row.get('CP','')} {row.get('Municipio','')}"

            out = {
                "Referencia_Catastral":  ref,
                "Lat": "", "Lon": "",
                "URL_Google_Maps":       f"https://www.google.com/maps/search/{quote_plus(addr_full)}",
                "URL_Catastro_Carto":    f"https://www1.sedecatastro.gob.es/Cartografia/mapa.aspx?refcat={ref}",
                "Image_Path":            "",
                "Toit_Type":             "",
                "Toit_Couleur":          "",
                "Combles_Amenageables":  "",
                "Etat_Toiture":          "",
                "Confiance_IA":          "",
                "Notes_IA":              "",
            }

            time.sleep(DELAY)
            coords = get_coords(ref)
            if coords:
                lat, lon = coords
                out["Lat"] = round(lat, 6)
                out["Lon"] = round(lon, 6)
                out["URL_Google_Maps"] = f"https://www.google.com/maps/@{lat},{lon},20z/data=!3m1!1e3"

                img_path = SAT_DIR / f"{ref}.png"
                if not img_path.exists():
                    download_satellite(lat, lon, img_path)
                if img_path.exists():
                    out["Image_Path"] = str(img_path)
                    if use_ai and client:
                        data = analyze_image(client, img_path.read_bytes())
                        if "error" not in data:
                            out["Toit_Type"]            = data.get("toit_type", "")
                            out["Toit_Couleur"]         = data.get("toit_couleur", "")
                            out["Combles_Amenageables"] = data.get("combles_amenageables", "")
                            out["Etat_Toiture"]         = data.get("etat_toiture", "")
                            out["Confiance_IA"]         = data.get("confiance", "")
                            out["Notes_IA"]             = data.get("notes", "")
                        else:
                            out["Notes_IA"] = data["error"]

            results.append(out)
    except KeyboardInterrupt:
        print(f"\nInterrompu - {len(results)} maisons traitees jusqu'ici.")

    df_out = df.merge(pd.DataFrame(results), on="Referencia_Catastral", how="left")
    csv_out = csv_in.replace(".csv", "_satellite.csv")
    df_out.to_csv(csv_out, index=False)

    print(f"\nCSV enrichi : {csv_out}")
    print(f"Images      : {SAT_DIR.absolute()}/  ({len(list(SAT_DIR.glob('*.png')))} png)")

    if use_ai:
        def cnt(k, v): return sum(1 for r in results if r.get(k) == v)
        print("\nAnalyse Claude Opus 4.7 :")
        print(f"  Toit pente   : {cnt('Toit_Type','pente'):>3}  (combles probables)")
        print(f"  Toit mixte   : {cnt('Toit_Type','mixte'):>3}")
        print(f"  Toit plat    : {cnt('Toit_Type','plat'):>3}  (pas de combles)")
        print(f"  Inconnu/err  : {len(results) - cnt('Toit_Type','pente') - cnt('Toit_Type','plat') - cnt('Toit_Type','mixte'):>3}")
        print(f"\n  Combles 'oui'      : {cnt('Combles_Amenageables','oui'):>3}")
        print(f"  Combles 'incertain': {cnt('Combles_Amenageables','incertain'):>3}")
        print(f"  Combles 'non'      : {cnt('Combles_Amenageables','non'):>3}")


if __name__ == "__main__":
    main()
