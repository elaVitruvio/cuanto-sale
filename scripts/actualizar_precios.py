#!/usr/bin/env python3
"""Baja la base SEPA (Precios Claros) y genera los datos del juego.

Fuente: Precios Claros – Base SEPA, Secretaría de Comercio (datos.produccion.gob.ar), CC BY 4.0.

Etapas:
  1. Descarga el ZIP más reciente del dataset "sepa-precios" (un ZIP por cadena adentro).
  2. Para cada producto vendido en sucursales de CABA y provincia de Buenos Aires calcula
     la mediana del precio en CADA cadena y después la mediana entre cadenas
     (así una sola cadena grande no define el precio). Guarda los más difundidos en
     data/candidatos.json.
  3. Con esos candidatos arma una lista curada y variada (cupos por rubro, marca y fabricante),
     limpia nombres, elige envase y colores, y agenda el producto del día de hoy y de los 7 días siguientes
     (fijo para todos y sin repetir en 60 días).

Uso:
  python scripts/actualizar_precios.py            # todo
  python scripts/actualizar_precios.py --offline  # solo etapa 3, desde data/candidatos.json
"""
import csv, io, json, os, re, statistics, sys, urllib.request, zipfile, datetime, hashlib
from array import array
from collections import defaultdict
from zoneinfo import ZoneInfo

API = "https://datos.produccion.gob.ar/api/3/action/package_show?id=sepa-precios"
UA = {"User-Agent": "cuanto-sale/1.0 (juego de precios)"}
PROVINCIAS = {"AR-C", "AR-B"}
AQUI = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(AQUI, "..", "data")
OUT = os.path.join(DATA, "productos.json")
CAND = os.path.join(DATA, "candidatos.json")
HIST = os.path.join(DATA, "historial.json")
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
MIN_SUCURSALES = 100
MIN_CADENAS = 3
CANTIDAD = 80
SIN_REPETIR_DIAS = 60

csv.field_size_limit(10_000_000)


# ---------------------------------------------------------------- etapa 1-2: SEPA
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
    with get(url, timeout=180) as r, open(destino, "wb") as f:
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
    """Devuelve {ean: {cadena: array(precios)}} e info descriptiva por ean."""
    precios = defaultdict(lambda: defaultdict(lambda: array("f")))
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
            clave = (p.get("id_comercio"), p.get("id_bandera"), p.get("id_sucursal"))
            if clave not in validas:
                continue
            pid = (p.get("id_producto") or "").strip()
            precio = num(p.get("productos_precio_lista"))
            if not pid or precio <= 0:
                continue
            precios[pid][clave[0]].append(precio)
            if pid not in info:
                info[pid] = {
                    "desc": (p.get("productos_descripcion") or "").strip(),
                    "marca": (p.get("productos_marca") or "").strip(),
                    "cant": num(p.get("productos_cantidad_presentacion")),
                    "unidad": (p.get("productos_unidad_medida_presentacion") or "").strip().lower(),
                }
        print(f"  {nombre}: {len(validas)} sucursales", flush=True)
    return precios, info


def disparidad(med):
    """Cociente entre la cadena más cara y la más barata, sin contar los extremos si hay 5 o más."""
    m = sorted(med)
    if len(m) >= 5:
        m = m[1:-1]
    return m[-1] / max(m[0], 1) if m else 1


def percentil(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


def candidatos(precios, info, fecha):
    out = []
    for pid, por_cadena in precios.items():
        n = sum(len(v) for v in por_cadena.values())
        if n < MIN_SUCURSALES or len(por_cadena) < MIN_CADENAS:
            continue
        i = info[pid]
        if not re.fullmatch(r"\d{8,14}", pid):
            continue
        medianas = [statistics.median(v) for v in por_cadena.values()]
        todos = [x for v in por_cadena.values() for x in v]
        out.append({
            "ean": pid, **i,
            "sucursales": n, "cadenas": len(por_cadena),
            "precio": round(statistics.median(medianas)),
            "p10": round(percentil(todos, .1)), "p90": round(percentil(todos, .9)),
            "medianas": sorted(round(m) for m in medianas),
        })
    out.sort(key=lambda c: (c["cadenas"], c["sucursales"]), reverse=True)
    return {"fecha": fecha, "candidatos": out[:1500]}


# ---------------------------------------------------------------- etapa 3: curaduría
EXCLUIR = (r"carefree|\btoa\b|toall|\bprot\b|protector|nosotras|siempre lib|ladysoft|afeit|venus|gillette|"
           r"cotonete|algod|nutrilon|nutribaby|vital\b|pa[ñn]al|preservativ|tampon|\bob\b|\bpato\b|pastilla|"
           r"\bp4\b|4flex|alimento (para )?(perro|gato)|dog|cat chow|pedigree|whiskas|recarga|repuesto|"
           r"\bpromo\b|\bpack\b|\bx ?\d+ ?u(n|nid)?\b|combo|\bkit\b|regalo|vodka|whisky|ron\b")

# (rubro, regex, cupo)
RUBROS = [
    ("higiene", r"antitrans|antitr|desodor|\bdeo\b|shampoo|\bsh\b|acond|\baco?\b|roll|r-on|jab[oó]n de tocador|"
                r"crema dental|dental|cepillo|colgate|dove|rexona|axe|nivea|sedal|elvive|pantene|h&s|papel hig", 4),
    ("limpieza", r"detergente|\bdet\b|lavandina|cif|lysoform|skip|\bala\b|ayud|magistral|limpiador|\blimp\b|desinf|"
                 r"suaviz|jab\.?\s?liq|lavar|poett|mr\.? ?musculo|esponja|trapo", 6),
    ("alcohol", r"cerveza|fernet|vino|sidra|aperitivo|cynar|gancia|campari|quilmes|brahma|stella|branca", 6),
    ("golosinas", r"caramel|gomita|chocolate|alfajor|bon o bon|oblea|chicle|turr[oó]n|sugus|mogul|rocklet|"
                  r"cofler|milka|block|tita|rhodesia|chupet", 6),
    ("snacks", r"galletit|pepitos|saladix|oreo|criollita|express|traviata|lays|papas fritas|chizito|palitos|"
               r"man[ií]|cereal|bizcochuelo|budin|vainillas", 8),
    ("bebidas", r"gaseosa|coca|sprite|fanta|pepsi|7 ?up|agua|aquarius|powerade|gatorade|jugo|cepita|ades|baggio|"
                r"levite|villavicencio|villa del sur|glaciar|t[oó]nica|schweppes|paso de los toros", 10),
    ("lacteos", r"leche|yogur|chocolatada|cindor|manteca|queso|crema de leche|dulce de leche|postre|danette|"
                r"serenito|actimel|danonino|ser\b|serenisima|sancor|tregar|ilolay|casancrem", 12),
    ("almacen", r".", 99),
]

ABREV = [
    (r"\bantitrans?\.?\b|\bantitr\b\.?|\bantit\b\.?", "antitranspirante"), (r"\bdeo\b\.?", "desodorante"),
    (r"\bsh\b\.?", "shampoo"), (r"\baco?\b\.?", "acondicionador"), (r"\br-on\b", "roll-on"),
    (r"\bcaram\b\.?", "caramelos"), (r"\bchoc\b\.?", "chocolate"), (r"\bfid\b\.?", "fideos"),
    (r"\bbizc\b\.?", "bizcochuelo"), (r"\bdet\b\.?", "detergente"), (r"\bjab\.?\s?liq\.?\s?", "jabón líquido "),
    (r"\baer\b\.?", "aerosol"), (r"\bcr\b\.?", "crema"), (r"\bliq\b\.?", "líquido"),
    (r"\blv\b\.?", "para lavar"), (r"\bsaq\b\.?", "saquitos"), (r"\bp/\s?dil\b\.?", "para diluir"),
    (r"\bdesinf\b\.?", "desinfectante"), (r"\bgall\.?\b|\bgallet\b\.?", "galletitas"),
    (r"\bmerm\b\.?", "mermelada"), (r"\bmayon\b\.?", "mayonesa"),
    (r"\bddl\b\.?|\bd\.?\s?de\s?l\b\.?|\bdulce de l\b\.?", "dulce de leche"),
    (r"\bpap\.?\s?hig\.?", "papel higiénico"), (r"\bbat\b\.?", "bebible"), (r"\bcalc\b\.?\+?", "calcio"),
    (r"\bvain\b\.?", "vainilla"), (r"\btrad\b\.?", "tradicional"), (r"\btall\b\.?", "tallarines"),
    (r"\blimp\b\.?", "limpiador"), (r"\benzi\b\.?", "enzimas"), (r"\bprimave\b\.?", "primavera"),
    (r"\bdesc\b\.?", "descremada"), (r"\bent\b\.?", "entera"), (r"\bfrut\b\.?", "frutilla"),
    (r"\bfrutil\b\.?", "frutilla"), (r"\bdur\b\.?", "durazno"), (r"\bnat\b\.?", "natural"),
    (r"\borig\b\.?", "original"), (r"\bclas\b\.?", "clásico"), (r"\bsab\b\.?", "sabor"),
    (r"\bc/\s?", "con "), (r"\bs/\s?", "sin "), (r"\bp/\s?", "para "), (r"\bc\b\.?(?=\s)", "con"),
    (r"\bn\s?°?\s?(\d+)\b", r"n.º \1"), (r"\bmolid\b\.?", "molido"), (r"\bgaseos\b\.?", "gaseosa"),
    (r"\b(reg|sint|lam|est|bot|bsa|pqt|paq|doy|dp|tb|pet|vid|fco|lta|sob|s/tacc|sin tacc)\b\.?", ""),
]
SOLO_HIGIENE = [(r"\bm\b\.?", "mujer"), (r"\bh\b\.?", "hombre")]
TILDES = {
    "limon": "limón", "tallarin": "tallarín", "tirabuzon": "tirabuzón", "pure": "puré", "cafe": "café",
    "jamon": "jamón", "ayudin": "Ayudín", "serenisima": "Serenísima", "canuelas": "Cañuelas",
    "azucar": "azúcar", "jabon": "jabón", "liquido": "líquido", "higienico": "higiénico", "clasico": "clásico",
    "clasica": "clásica", "mani": "maní", "atun": "atún", "budin": "budín", "turron": "turrón",
    "melon": "melón", "maracuya": "maracuyá", "algodon": "algodón", "pina": "piña", "cacao": "cacao",
    "mananita": "Mañanita", "taragui": "Taragüí", "amanda": "Amanda", "rosamonte": "Rosamonte",
    "lacteo": "lácteo", "light": "light", "zero": "Zero", "musculo": "Músculo", "mostaza": "mostaza",
    "tonica": "tónica", "garbanzos": "garbanzos", "porotos": "porotos", "pure,": "puré,",
}
MARCAS = {"coca cola": "Coca-Cola", "bon o bon": "Bon o Bon", "bc": "BC", "la serenisima": "La Serenísima",
          "h&s": "H&S", "mr musculo": "Mr. Músculo", "7up": "7UP", "7 up": "7UP", "bon-o-bon": "Bon o Bon", "don vicente": "Don Vicente"}
# Correcciones manuales por EAN (tienen prioridad sobre todo lo demás)
NOMBRES = {
    "7790387015324": "Yerba Mañanita",
    "7792710000182": "Yerba Amanda tradicional",
    "7790070336293": "Fideos tirabuzón Matarazzo",
    "7790070336316": "Tallarines n.º 5 Matarazzo",
    "7791337008472": "Serenito con Rocklets",
    "7791337008489": "Serenito con Chocobombas",
    "7791337008106": "Danette de dulce de leche",
    "7791337008564": "Yogur bebible Ser vainilla",
    "7790895000232": "Coca-Cola en lata",
    "7791293043791": "Desodorante Axe Marine",
    "7791293049502": "Antitranspirante Rexona Men",
    "7791293049557": "Antitranspirante Rexona Nutritive",
    "7891150081239": "Antitranspirante Dove Original",
    "7790580346515": "Bon o Bon de leche",
    "7790580056001": "Caramelos Sugus",
    "7790580199913": "Gomitas Mogul ositos",
    "7791290792814": "Jabón líquido Skip BioEnzimas",
    "7791290792043": "Jabón líquido Ala Ecolavado para diluir",
    "7793253003548": "Limpiador desinfectante Poett Primavera",
    "7793253003838": "Lavandina en gel Ayudín Lavanda",
    "7790070336576": "Tallarines Don Vicente",
    "7791337008694": "Danonino frutilla",
    "7622201735906": "Galletitas Pepitos",
    "7790580716707": "Galletitas Saladix jamón",
    "7792360094968": "Café en saquitos Sensaciones",
}

# (regex sobre el nombre limpio, envase, color cuerpo, color acento). Primer match gana.
REGLAS = [
    (r"coca", "pet", "#C8102E", "#C8102E"), (r"sprite|7up", "pet", "#2E9E4A", "#2E9E4A"),
    (r"fanta", "pet", "#F28C28", "#1F4FA3"), (r"pepsi", "pet", "#1F4FA3", "#1F4FA3"),
    (r"t[oó]nica|schweppes", "pet", "#F2E14A", "#1d1b1a"),
    (r"aquarius", "pet", "#BFE3F2", "#3A7BD5"), (r"powerade|gatorade", "pet", "#3A7BD5", "#1d1b1a"),
    (r"\bagua\b|villavicencio|villa del sur|glaciar|eco de los andes", "pet", "#BFE3F2", "#3A7BD5"),
    (r"cerveza|quilmes|brahma|stella|heineken|andes", "can", "#F2C230", "#1F4FA3"),
    (r"fernet|branca", "bottle", "#2B2B2B", "#D23B2E"), (r"vino|malbec", "bottle", "#6B1E2E", "#F2C230"),
    (r"aperitivo|gancia|campari|cynar", "bottle", "#3E8E41", "#F2C230"),
    (r"aceite", "pet", "#F2C230", "#3E8E41"), (r"vinagre", "pet", "#F5E6C8", "#D23B2E"),
    (r"antitransp|desodor", "aerosol", "#2B2B2B", "#3A7BD5"), (r"aerosol", "aerosol", "#3A7BD5", "#fff"),
    (r"shampoo|acondic", "squeeze", "#F28CB0", "#fff"), (r"crema dental|dental", "squeeze", "#fff", "#D23B2E"),
    (r"cif crema|savora|mostaza|ketchup", "squeeze", "#F2C230", "#D23B2E"),
    (r"mayonesa", "pouch", "#F7E3A1", "#3A7BD5"),
    (r"lavandina|ayud[ií]n", "jug", "#8a5aa0", "#fff"),
    (r"skip|lavar|poett|limpiador|ecolava|\bala\b|suaviz|desinf|lysoform|m[uú]sculo", "jug", "#3A7BD5", "#fff"),
    (r"detergente|magistral", "jug", "#F2E14A", "#3E8E41"),
    (r"papel hig", "rolls", "#9ED0E6", "#fff"),
    (r"manteca", "brick", "#F5E08A", "#3A7BD5"),
    (r"queso crema|casancrem|queso untable", "cup", "#fff", "#3A7BD5"),
    (r"danette|serenito|yogur firme|postre|flan", "cup", "#fff", "#8B5A2B"),
    (r"actimel|danonino|bebible|yogurisimo", "minibottle", "#fff", "#E86F8A"),
    (r"yogur", "cup", "#fff", "#E86F8A"),
    (r"bizcochuelo|bud[ií]n", "box", "#8B5A2B", "#F2C230"),
    (r"gomita|sugus|caramel|chupet|mogul", "pouch", "#E86F8A", "#F2C230"),
    (r"alfajor", "sleeve", "#1d1b1a", "#F2C230"),
    (r"chocolate|tableta|milka|cofler|block", "bar", "#6B3A1E", "#fff"),
    (r"bon o bon|oblea|opera", "sleeve", "#D23B2E", "#F2C230"),
    (r"galletit|pepitos|saladix|oreo|criollita|express|traviata|vainillas", "sleeve", "#1F4FA3", "#8B5A2B"),
    (r"chocolatada|cindor", "carton", "#6B3A1E", "#F2C230"),
    (r"leche", "pouch", "#fff", "#3A7BD5"),
    (r"cepita|baggio|jugo(?! en polvo)|ades|levite", "tetra", "#F28C28", "#3E8E41"),
    (r"jugo en polvo|tang|clight|\bsobre\b", "sachet", "#F28C28", "#D23B2E"),
    (r"papas fritas|lays|chizito|palitos|man[ií]\b", "bag", "#F2C230", "#D23B2E"),
    (r"fideo|tallar|spaghetti|tirabuz|mo[ñn]o|codito|guisero", "pasta", "#1F4FA3", "#fff"),
    (r"yerba", "yerba", "#F2C230", "#3E8E41"),
    (r"arroz", "bag", "#F4F1E8", "#D23B2E"), (r"az[uú]car", "bag", "#fff", "#1F4FA3"),
    (r"harina|polenta", "bag", "#F5E6C8", "#D23B2E"), (r"caf[eé].*saquito|saquito|t[eé]\b|mate cocido", "box", "#D23B2E", "#F2C230"),
    (r"caf[eé]", "bag", "#4B2E1E", "#F2C230"), (r"sal\b", "box", "#3A7BD5", "#fff"),
    (r"mermelada|dulce de leche|miel", "jar", "#E86F8A", "#D23B2E"),
    (r"pur[eé] de tomate|tomate|salsa", "tetra", "#D23B2E", "#3E8E41"),
    (r"at[uú]n|arveja|choclo|durazno|lenteja|poroto|garbanzo|caballa|sardina", "tin", "#D9D9D9", "#3E8E41"),
    (r"pur[eé]|bizcochuelo|gelatina|flan|cereal|avena", "box", "#D23B2E", "#F2C230"),
    (r"pan\b|lactal|bimbo|fargo", "bag", "#F5E6C8", "#1F4FA3"),
]
COLORES = ["#D23B2E", "#F2C230", "#3A7BD5", "#3E8E41", "#8B5A2B", "#F28C28", "#1F4FA3", "#7EC8E3", "#8a5aa0", "#E86F8A"]
COLOR_MARCA = {"dulce de leche": "#8B5A2B"}
ENVASE_BEBIDA = {"pet", "can", "bottle", "tetra", "carton", "minibottle"}


def limpiar_nombre(desc, rubro):
    s = re.sub(r"\s+x?\s*\d+[.,]?\d*\s*(ml|cc|grs?|g|kg|lts?|l|un|u|cm3)\b\.?.*$", "", desc, flags=re.I)
    s = re.sub(r"\b\d+[.,]?\d*\s*(ml|cc|grs?|g|kg|lts?|l|cm3)\b\.?", "", s, flags=re.I)
    reglas = ABREV + (SOLO_HIGIENE if rubro == "higiene" else [])
    for patron, rep_ in reglas:
        s = re.sub(patron, rep_, s, flags=re.I)
    s = re.sub(r"[^\wáéíóúñü&.º+/\- ]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip(" .-/")
    for k, v in MARCAS.items():
        s = re.sub(rf"\b{re.escape(k)}\b", v, s)
    chicas = {"de", "del", "la", "el", "y", "con", "sin", "en", "para", "o", "a", "al", "n.º", "en"}
    out = []
    for i, p in enumerate(s.split()):
        q = TILDES.get(p, p)
        if q != p and q[0].isupper():
            out.append(q)
        elif i == 0:
            out.append(q[:1].upper() + q[1:])
        elif q in chicas or re.match(r"^\d", q) or any(c.isupper() for c in q):
            out.append(q)
        else:
            out.append(q)
    s = " ".join(out)
    # marcas con mayúscula: las que figuran en MARCAS ya están; el resto, capitalizar palabras de marca después
    return s


def marca_bonita(m):
    k = m.lower().strip()
    if k in MARCAS:
        return MARCAS[k]
    return " ".join(TILDES.get(w, w).capitalize() if TILDES.get(w, w).islower() else TILDES.get(w, w) for w in k.split())


def con_marca(nombre, marca):
    """Pone la marca con mayúsculas dentro del nombre (o la agrega al final si no está)."""
    mb = marca_bonita(marca)
    patron = re.compile(r"\b" + r"[\s\-]*".join(re.escape(w) for w in re.split(r"[\s\-]+", marca.lower()) if w) + r"\b", re.I)
    sin_tilde = nombre
    if patron.search(sin_tilde):
        return patron.sub(mb, sin_tilde, count=1)
    # probá con la versión con tildes
    p2 = re.compile(re.escape(mb), re.I)
    if p2.search(nombre):
        return p2.sub(mb, nombre, count=1)
    return f"{nombre} {mb}"


def tamano(desc, cant, unidad, envase, rubro_=None):
    m = re.search(r"x?\s*(\d+[.,]?\d*)\s*(ml|cc|grs?|g|kg|lts?|l|cm3)\b", desc, re.I)
    if m:
        cant, unidad = num(m.group(1)), m.group(2).lower()
    if cant <= 0:
        return "", 0, ""
    u = {"g": "gr", "grs": "gr", "grm": "gr", "l": "lt", "lts": "lt", "litro": "lt", "kgs": "kg", "kilo": "kg",
         "cm3": "ml", "cc": "ml"}.get(unidad, unidad)
    if u not in ("gr", "kg", "ml", "lt"):
        return "", 0, ""
    if rubro_ in ("bebidas", "alcohol") and u in ("gr", "kg"):  # bebidas cargadas en gramos
        u = "ml" if u == "gr" else "lt"
    if (u == "ml" and cant < 10) or (u == "gr" and cant < 10):
        return "", 0, ""
    if u == "ml" and cant >= 1000:
        u, cant = "lt", cant / 1000
    if u == "gr" and cant >= 1000:
        u, cant = "kg", cant / 1000
    f = lambda x: f"{x:g}".replace(".", ",")
    txt = {"gr": f"{f(cant)} g", "kg": f"{f(cant)} kg", "ml": f"{f(cant)} ml", "lt": f"{f(cant)} L"}[u]
    return txt, cant, u


def unitario(cant, unidad):
    base = {"kg": (cant, "kilo"), "gr": (cant / 1000, "kilo"), "lt": (cant, "litro"), "ml": (cant / 1000, "litro")}.get(unidad)
    if not base:
        return None
    # solo tiene sentido en envases medianos o grandes, y no cuando ya es 1 kg / 1 L justo
    if base[0] < 0.25 or abs(base[0] - 1) < 1e-6:
        return None
    return [round(base[0], 3), base[1]]


def rubro(desc):
    d = desc.lower()
    for nombre, patron, _ in RUBROS:
        if re.search(patron, d):
            return nombre
    return "almacen"


def estilo(nombre, desc):
    t = (nombre + " " + desc).lower()
    for patron, k, c, a in REGLAS:
        if re.search(patron, t):
            return k, c, a
    h = int(hashlib.md5(nombre.encode()).hexdigest(), 16)
    return "box", COLORES[h % len(COLORES)], "#fff"


def linea(nombre, marca):
    """Primera palabra + marca: evita pares casi gemelos (Powerade/Powerade, Serenito/Serenito)."""
    w = re.findall(r"\w+", nombre.lower())
    return (w[0] if w else "", marca.lower().strip())


def fabricante(ean):
    return ean[:7] if ean.startswith("779") else ean[:6]


def curar(cands, fecha):
    elegidos, n_marca, n_fab, n_rubro, lineas, vistos, gemelos = [], defaultdict(int), defaultdict(int), defaultdict(int), set(), set(), set()
    cupos = {r: c for r, _, c in RUBROS}
    fecha_txt = datetime.date.fromisoformat(fecha).strftime("%d/%m/%Y") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha or "") else fecha
    for c in cands:
        desc, marca = c["desc"], c["marca"]
        mu = marca.upper().strip()
        if not mu or mu in ("SIN MARCA", "GENERICO", "GENÉRICO", "VARIOS", "S/M", "SM"):
            continue
        if re.search(EXCLUIR, desc.lower()):
            continue
        if c["p10"] and c["p90"] / max(c["p10"], 1) > 2.2:  # precios demasiado dispersos: dato poco fiable
            continue
        med = c["medianas"]
        if disparidad(med) > 2.2:
            continue
        r = rubro(desc)
        if n_rubro[r] >= cupos.get(r, 99):
            continue
        if n_marca[mu] >= 1 or n_fab[fabricante(c["ean"])] >= 4:
            continue
        nombre = NOMBRES.get(c["ean"]) or con_marca(limpiar_nombre(desc, r), marca)
        k, col, acc = estilo(nombre, desc)
        tam, cant, uni = tamano(desc, c["cant"], c["unidad"], k, r)
        if not tam:
            continue
        if k == "pet" and r in ("bebidas", "alcohol") and uni == "ml" and cant <= 473 and "agua" not in nombre.lower():
            k = "can"
        if k == "tetra" and uni == "ml" and cant <= 250:
            k = "minitetra"
        ln = linea(nombre, marca)
        gem = (fabricante(c["ean"]), int(c["precio"]))
        if ln in lineas or (nombre, tam) in vistos or gem in gemelos:
            continue
        gemelos.add(gem)
        lineas.add(ln)
        vistos.add((nombre, tam))
        n_rubro[r] += 1
        n_marca[mu] += 1
        n_fab[fabricante(c["ean"])] += 1
        mb = marca_bonita(marca)
        item = {
            "n": nombre,
            "d": tam if mb.lower() in nombre.lower() else f"{tam} · {mb}",
            "p": int(c["precio"]),
            "k": k, "c": col, "a": acc,
            "r": r,
            "s": (f"Precios Claros (SEPA) · mediana de {len(med)} cadenas y "
                  f"{c['sucursales']:,} sucursales de CABA y provincia de Bs. As. · {fecha_txt}").replace(",", "."),
            "ean": c["ean"],
        }
        u = unitario(cant, uni)
        if u:
            item["u"] = u
        # golosinas chiquitas y precios muy bajos: sirven para el modo libre, no como producto del día
        if (r == "golosinas" and uni == "gr" and cant < 60) or item["p"] < 700:
            item["diario"] = False
        elegidos.append(item)
        if len(elegidos) >= CANTIDAD:
            break
    elegidos.sort(key=lambda x: x["ean"])
    return elegidos


def desde_curados(cands, fecha):
    """Arma la lista con los productos curados a mano (curados.py) y el precio del día de SEPA."""
    sys.path.insert(0, AQUI)
    from curados import CURADOS
    from collections import Counter
    firma = Counter((f[2], f[3].lower(), f[4].lower(), (f[6].get("g", "") if len(f) > 6 else "")) for f in CURADOS)
    dup = [k for k, n in firma.items() if n > 1]
    if dup:
        sys.exit(f"Hay productos con el mismo dibujo (envase, color, acento, ícono): {dup}")
    por_ean = {c["ean"]: c for c in cands}
    fecha_txt = datetime.date.fromisoformat(fecha).strftime("%d/%m/%Y") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha or "") else fecha
    out = []
    for fila in CURADOS:
        ean, nombre, k, col, acc, r = fila[:6]
        extra = fila[6] if len(fila) > 6 else {}
        c = por_ean.get(ean)
        if not c:
            print("  (sin datos hoy)", nombre)
            continue
        med = c["medianas"]
        if (c["p10"] and c["p90"] / max(c["p10"], 1) > 2.2) or disparidad(med) > 2.2:
            print("  (precios muy dispares hoy)", nombre, med)
            continue
        tam, cant, uni = tamano(c["desc"], c["cant"], c["unidad"], k, r)
        item = {
            "n": nombre, "d": extra.get("t", tam), **({"g": extra["g"]} if extra.get("g") else {}), "p": int(round(statistics.median(med))) if med else int(c["precio"]), "k": k, "c": col, "a": acc, "r": r,
            "s": (f"Precios Claros (SEPA) · mediana de {len(med)} cadenas y "
                  f"{c['sucursales']:,} sucursales de CABA y provincia de Bs. As. · {fecha_txt}").replace(",", "."),
            "ean": ean,
        }
        u = unitario(cant, uni)
        if u and "para preparar" not in extra.get("t", ""):
            item["u"] = u
        n = len(med)
        iqr = (med[(3 * n) // 4] - med[n // 4]) / max(item["p"], 1) if n >= 4 else 0
        # golosinas chiquitas, precios muy bajos o precios que dependen mucho de la cadena:
        # sirven para el modo libre, pero no como producto del día (que se juega a ±5%)
        cerca = sum(abs(m - item["p"]) <= .05 * item["p"] for m in med)  # cadenas a ±5% del precio
        if (extra.get("libre") or (r == "golosinas" and uni == "gr" and cant < 60) or item["p"] < 700 or iqr > 0.20
                or not (.90 * item["p"] <= c["p10"] <= item["p"] <= c["p90"] <= 1.10 * item["p"])
                or cerca < .55 * len(med)):
            item["diario"] = False
        out.append(item)
    out.sort(key=lambda x: x["ean"])
    return out


def leer_historial():
    """Lee data/historial.json. Si falta o está roto, rescata la agenda publicada en productos.json."""
    try:
        with open(HIST, encoding="utf-8") as f:
            hist = json.load(f)
        if not isinstance(hist, dict):
            raise ValueError("el historial no es un objeto")
        return hist
    except (OSError, ValueError) as e:
        print("Historial ilegible, uso la agenda de productos.json:", e)
        try:
            with open(OUT, encoding="utf-8") as f:
                return dict(json.load(f).get("agenda") or {})
        except (OSError, ValueError):
            return {}


def agendar(productos):
    """Agenda el producto del día para hoy y los 7 días siguientes (hora argentina).

    El de hoy no cambia nunca (ya se está jugando). Los días futuros se actualizan con el precio
    del día y, si un producto dejó de ser apto (sin datos o precios dispares), se reprograma.
    Devuelve la agenda desde ayer y la lista de EAN ya jugados (para el modo libre)."""
    hist = leer_historial()
    hoy = datetime.datetime.now(TZ).date()
    por_ean = {p["ean"]: p for p in productos if p.get("diario", True)}
    for k in list(hist):
        if k > hoy.isoformat():
            if hist[k].get("ean") in por_ean:
                hist[k] = dict(por_ean[hist[k]["ean"]])
            else:
                print(f"Reprogramo el {k}: {hist[k].get('n')} ya no es apto")
                del hist[k]
    for d in (hoy + datetime.timedelta(days=i) for i in range(8)):  # hoy y 7 días de reserva
        clave = d.isoformat()
        if clave in hist:
            continue
        desde = (d - datetime.timedelta(days=SIN_REPETIR_DIAS)).isoformat()
        usados = {v["ean"] for k, v in hist.items() if k >= desde}
        pool = [p for p in productos if p.get("diario", True) and p["ean"] not in usados]
        if not pool:
            pool = [p for p in productos if p.get("diario", True)]
        # variedad: no repetir el rubro de ayer ni de anteayer si se puede
        recientes = {hist[x].get("r") for x in ((d - datetime.timedelta(days=j)).isoformat() for j in (1, 2)) if x in hist}
        pool = [p for p in pool if p.get("r") not in recientes] or pool
        i = int(hashlib.md5(("cuanto-sale/" + clave).encode()).hexdigest(), 16) % len(pool)
        hist[clave] = dict(pool[i])
        print(f"Producto del {clave}: {pool[i]['n']} ({pool[i]['p']})")
    limite = (hoy - datetime.timedelta(days=120)).isoformat()
    hist = {k: v for k, v in sorted(hist.items()) if k >= limite}
    with open(HIST + ".tmp", "w", encoding="utf-8") as f:  # escritura atómica
        json.dump(hist, f, ensure_ascii=False, indent=1)
    os.replace(HIST + ".tmp", HIST)
    ayer = (hoy - datetime.timedelta(days=1)).isoformat()
    jugados = sorted({v["ean"] for k, v in hist.items() if k < hoy.isoformat()})
    return {k: v for k, v in hist.items() if k >= ayer}, jugados


def main():
    offline = "--offline" in sys.argv
    os.makedirs(DATA, exist_ok=True)
    if offline:
        with open(CAND, encoding="utf-8") as f:
            C = json.load(f)
    else:
        url, fecha = recurso_mas_reciente()
        print("Descargando", url, fecha, flush=True)
        tmp = "/tmp/sepa.zip"
        descargar(url, tmp)
        precios, info = procesar(tmp)
        C = candidatos(precios, info, fecha or datetime.date.today().isoformat())
        with open(CAND, "w", encoding="utf-8") as f:
            json.dump(C, f, ensure_ascii=False, separators=(",", ":"))
        print(f"{len(C['candidatos'])} candidatos → {CAND}")
    productos = desde_curados(C["candidatos"], C["fecha"])
    if len(productos) < 30:
        print(f"Solo {len(productos)} curados disponibles; completo con la selección automática")
        ya = {p["ean"] for p in productos}
        productos += [p for p in curar(C["candidatos"], C["fecha"]) if p["ean"] not in ya][: 80 - len(productos)]
    if len(productos) < 20:
        sys.exit(f"Muy pocos productos ({len(productos)}); no actualizo el archivo")
    agenda, jugados = agendar(productos)
    salida = {
        "fecha": C["fecha"],
        "fuente": "Precios Claros – Base SEPA, Secretaría de Comercio (datos.produccion.gob.ar), CC BY 4.0",
        "agenda": agenda,
        "jugados": jugados,
        "productos": productos,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=1)
    por_rubro = defaultdict(int)
    for p in productos:
        por_rubro[p["r"]] += 1
    print(f"Listo: {len(productos)} productos → {OUT}  {dict(por_rubro)}")


if __name__ == "__main__":
    main()
