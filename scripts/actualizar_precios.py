#!/usr/bin/env python3
"""Baja la base SEPA (Precios Claros) del día y genera data/productos.json para el juego.

Fuente: Precios Claros – Base SEPA, Secretaría de Comercio (datos.produccion.gob.ar), CC BY 4.0.
Qué hace:
  1. Busca en el portal el ZIP más reciente del dataset "sepa-precios".
  2. Recorre el ZIP de ZIPs (uno por cadena) y lee sucursales.csv y productos.csv (separados por |).
  3. Se queda con sucursales de CABA y provincia de Buenos Aires.
  4. Para cada producto calcula la MEDIANA del precio de lista entre sucursales
     (la mediana ignora precios mal cargados).
  5. Elige los productos con marca más difundidos (los que más sucursales venden) y los guarda.
"""
import csv, io, json, os, re, statistics, sys, urllib.request, zipfile, datetime, hashlib
from array import array
from collections import defaultdict

API = "https://datos.produccion.gob.ar/api/3/action/package_show?id=sepa-precios"
UA = {"User-Agent": "cuanto-sale/1.0 (juego de precios)"}
PROVINCIAS = {"AR-C", "AR-B"}  # CABA y Buenos Aires
MIN_SUCURSALES = 150
CANTIDAD = 60
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "productos.json")

csv.field_size_limit(10_000_000)


def get(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def recurso_mas_reciente():
    data = json.load(get(API))
    recursos = data["result"]["resources"]

    def fecha(r):
        m = re.search(r"\d{4}-\d{2}-\d{2}", (r.get("name") or "") + " " + (r.get("description") or ""))
        return m.group(0) if m else ""

    zips = [r for r in recursos if (r.get("format") or "").lower() == "zip" or r["url"].lower().endswith(".zip")]
    zips.sort(key=fecha, reverse=True)
    if not zips:
        sys.exit("No encontré ZIPs en el dataset")
    return zips[0]["url"], fecha(zips[0])


def descargar(url, destino):
    with get(url, timeout=120) as r, open(destino, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def leer_csv(z, nombre):
    with z.open(nombre) as raw:
        texto = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        lector = csv.reader(texto, delimiter="|")
        cab = next(lector, None)
        if not cab:
            return
        cab = [c.strip().lower() for c in cab]
        for fila in lector:
            if len(fila) < len(cab) - 1:  # última línea "Última actualización: ..."
                continue
            yield dict(zip(cab, fila))


def num(s):
    try:
        return float(str(s).replace(",", "."))
    except ValueError:
        return 0.0


def procesar(path):
    precios = defaultdict(lambda: array("f"))
    info = {}
    externo = zipfile.ZipFile(path)
    internos = [n for n in externo.namelist() if n.lower().endswith(".zip")]
    print(f"{len(internos)} cadenas en el ZIP", flush=True)
    for nombre in internos:
        try:
            z = zipfile.ZipFile(io.BytesIO(externo.read(nombre)))
        except zipfile.BadZipFile:
            continue
        archivos = z.namelist()
        suc = next((a for a in archivos if a.lower().endswith("sucursales.csv")), None)
        prod = next((a for a in archivos if a.lower().endswith("productos.csv")), None)
        if not suc or not prod:
            continue
        validas = set()
        for s in leer_csv(z, suc):
            if s.get("sucursales_provincia", "").strip().upper() in PROVINCIAS:
                validas.add((s.get("id_comercio"), s.get("id_bandera"), s.get("id_sucursal")))
        if not validas:
            continue
        for p in leer_csv(z, prod):
            if (p.get("id_comercio"), p.get("id_bandera"), p.get("id_sucursal")) not in validas:
                continue
            pid = (p.get("id_producto") or "").strip()
            precio = num(p.get("productos_precio_lista"))
            if not pid or precio <= 0:
                continue
            precios[pid].append(precio)
            if pid not in info:
                info[pid] = {
                    "desc": (p.get("productos_descripcion") or "").strip(),
                    "marca": (p.get("productos_marca") or "").strip(),
                    "cant": num(p.get("productos_cantidad_presentacion")),
                    "unidad": (p.get("productos_unidad_medida_presentacion") or "").strip().lower(),
                }
        print(f"  {nombre}: {len(validas)} sucursales", flush=True)
    return precios, info


KINDS = [
    (r"chocolatada|leche entera|leche desc|leche larga|leche ultra|yogur|cepita|jugo", "carton"),
    (r"aceite|gaseosa|agua|cerveza|vino|vinagre|lavandina|detergente|shampoo|acondicionador|suavizante|coca|sprite|fanta|pepsi|aquarius|powerade|cynar|fernet|jab[oó]n l[ií]quido|limpiador|antitranspirante|desodorante|roll-on|aerosol", "bottle"),
    (r"dulce de leche|mermelada|mayonesa|manteca|queso untable", "jar"),
    (r"papel higi", "rolls"),
    (r"jugo en polvo|jugo polvo|sobre", "sachet"),
    (r"arveja|atun|atún|choclo|tomate|lata|durazno", "can"),
    (r"yerba|arroz|az[uú]car|harina|caf[eé]|polenta|sal |lentej|porot|galletit|papas fritas", "bag"),
]
ABREV = [
    (r"\bantitrans?\.?\b|\bantitr\b\.?", "Antitranspirante"), (r"\bdeo\b\.?", "Desodorante"),
    (r"\bsh\b\.?", "Shampoo"), (r"\baco?\b\.?", "Acondicionador"), (r"\br-on\b", "Roll-on"),
    (r"\bcaram\b\.?", "Caramelos"), (r"\bchoc\b\.?", "Chocolate"), (r"\bfid\b\.?", "Fideos"),
    (r"\bbizc\b\.?", "Bizcochuelo"), (r"\bdet\b\.?", "Detergente"), (r"\bjab\.?\s?liq\.?\s?", "Jabón líquido "),
    (r"\baer\b\.?", "Aerosol"), (r"\bcr\b\.?", "Crema"), (r"\bliq\b\.?", "Líquido"), (r"\blv\b\.?", "Lavar"),
    (r"\bsaq\b\.?", "Saquitos"), (r"\bp/dil\b\.?", "para diluir"), (r"\bdesinf\b\.?", "Desinfectante"),
    (r"\bgall\.?\b|\bgallet\b\.?", "Galletitas"), (r"\bmerm\b\.?", "Mermelada"), (r"\bmayon\b\.?", "Mayonesa"),
    (r"\bdl\b|\bdulce de l\b\.?", "Dulce de leche"), (r"\bpap\.?\s?hig\.?", "Papel higiénico"),
]
EXCLUIR = r"carefree|toa\b|toall|\bprot\b|protector|nosotras|siempre lib|ladysoft|afeit|venus|gillette|cotonete|algod|nutrilon|nutribaby|vital|pa[ñn]al|preservativ|tampon|\bob\b|cynar|aperitivo|vino|cerveza|fernet|whisky|vodka|pato|pastilla"
GRUPOS = [
    ("higiene", r"antitrans|antitr|desodor|\bdeo\b|shampoo|\bsh\b|acond|\baco?\b|roll|r-on|jab[oó]n de tocador|crema dental|cepillo|colgate|dove|rexona|axe|nivea|sedal|elvive|pantene|h&s", 6),
    ("limpieza", r"detergente|\bdet\b|lavandina|cif|lysoform|skip|ala\b|ayud|magistral|limpiador|desinf|suaviz|\baer\b|\bjab\.?\s?liq", 8),
]
COLORES = ["#D23B2E", "#F2C230", "#3A7BD5", "#3E8E41", "#8B5A2B", "#F28C28", "#1F4FA3", "#7EC8E3", "#8a5aa0", "#E86F8A"]


def tipo(desc):
    d = desc.lower()
    for patron, k in KINDS:
        if re.search(patron, d):
            return k
    return "box"


def expandir(s):
    s = re.sub(r"\s+x\s*\d+[.,]?\d*\s*(ml|cc|gr?|kg|lt?|un)\b.*$", "", s, flags=re.I)
    for patron, rep_ in ABREV:
        s = re.sub(patron, rep_, s, flags=re.I)
    return re.sub(r"\\s+", " ", s).strip()


def grupo(desc):
    d = desc.lower()
    for nombre, patron, _ in GRUPOS:
        if re.search(patron, d):
            return nombre
    return "almacen"


def tamano(desc, cant, unidad):
    m = re.search(r"x\s*(\d+[.,]?\d*)\s*(ml|cc|gr?|kg|lt?)\b", desc, re.I)
    if m:
        cant, unidad = num(m.group(1)), m.group(2).lower()
    if cant <= 0:
        return "", 0, ""
    u = {"g": "gr", "grs": "gr", "grm": "gr", "l": "lt", "lts": "lt", "litro": "lt", "kgs": "kg", "kilo": "kg", "cm3": "ml", "cc": "ml"}.get(unidad, unidad)
    if (u == "ml" and cant < 10) or (u == "gr" and cant < 20):
        return "", 0, ""
    if u == "ml" and cant >= 1000:
        u, cant = "lt", cant / 1000
    if u == "gr" and cant >= 1000:
        u, cant = "kg", cant / 1000
    txt = {"gr": f"{cant:g} g", "kg": f"{cant:g} kg".replace(".", ","), "ml": f"{cant:g} ml", "lt": f"{cant:g} L".replace(".", ",")}.get(u, "")
    return txt, cant, u


def titulo(s):
    chicas = {"de", "del", "la", "el", "y", "con", "sin", "en", "x"}
    pal = s.lower().split()
    return " ".join(p if (i and p in chicas) or re.match(r"^\d", p) else p.capitalize() for i, p in enumerate(pal))


def unitario(precio, cant, unidad):
    if cant <= 0:
        return None
    if unidad == "kg":
        return [cant, "kilo"]
    if unidad == "gr":
        return [cant / 1000, "kilo"]
    if unidad == "lt":
        return [cant, "litro"]
    if unidad == "ml":
        return [cant / 1000, "litro"]
    return None


def elegir(precios, info, fecha):
    candidatos = []
    for pid, lista in precios.items():
        i = info[pid]
        marca = i["marca"].upper()
        if len(lista) < MIN_SUCURSALES or not marca or marca in ("SIN MARCA", "GENERICO", "GENÉRICO", "VARIOS"):
            continue
        if re.search(EXCLUIR, i["desc"].lower()):
            continue
        candidatos.append((len(lista), pid))
    candidatos.sort(reverse=True)
    elegidos, por_marca, por_grupo, vistos = [], defaultdict(int), defaultdict(int), set()
    topes = {g: t for g, _, t in GRUPOS}
    for n, pid in candidatos:
        i = info[pid]
        g = grupo(i["desc"])
        if por_marca[i["marca"].upper()] >= 2 or por_grupo[g] >= topes.get(g, 999):
            continue
        desc = titulo(expandir(i["desc"]))
        tam, cant, uni = tamano(i["desc"], i["cant"], i["unidad"])
        clave = (desc, tam)
        if clave in vistos:
            continue
        vistos.add(clave)
        por_marca[i["marca"].upper()] += 1
        por_grupo[g] += 1
        med = statistics.median(precios[pid])
        h = int(hashlib.md5(pid.encode()).hexdigest(), 16)
        item = {
            "n": desc,
            "d": " · ".join(x for x in (tam, titulo(i["marca"])) if x),
            "p": int(round(med)),
            "k": tipo(desc),
            "c": COLORES[h % len(COLORES)],
            "s": f"SEPA – mediana de {n} sucursales de CABA y GBA · {fecha}",
            "ean": pid,
        }
        u = unitario(med, cant, uni)
        if u:
            item["u"] = u
        elegidos.append(item)
        if len(elegidos) >= CANTIDAD:
            break
    return elegidos


def main():
    url, fecha = recurso_mas_reciente()
    print("Descargando", url, fecha, flush=True)
    tmp = "/tmp/sepa.zip"
    descargar(url, tmp)
    precios, info = procesar(tmp)
    productos = elegir(precios, info, fecha or datetime.date.today().isoformat())
    if len(productos) < 15:
        sys.exit(f"Muy pocos productos ({len(productos)}); no actualizo el archivo")
    salida = {
        "fecha": fecha,
        "fuente": "Precios Claros – Base SEPA, Secretaría de Comercio (datos.produccion.gob.ar), CC BY 4.0",
        "productos": productos,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=1)
    print(f"Listo: {len(productos)} productos → {OUT}")


if __name__ == "__main__":
    main()
