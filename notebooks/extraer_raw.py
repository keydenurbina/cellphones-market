"""
Extrae los celulares de solotodo.cl SOLO desde el HTML (requests + BeautifulSoup, sin API) y guarda los datos RAW.

Uso:   python extraer_raw.py
Salida: data/output/data_raw_AAAAMMDD_HHMM.csv (histórico) y data/output/data_raw.csv (última extracción)
Respaldo: el HTML descargado queda en data/input/html_AAAAMMDD/ (si se corta, al re-ejecutar retoma desde ahí)
"""
# Librerías: todas vienen con Anaconda / Colab
import re, json, time, gzip
import pandas as pd, requests
from bs4 import BeautifulSoup                         # lee HTML como un árbol de etiquetas
from requests.adapters import HTTPAdapter, Retry      # reintentos automáticos si el sitio falla
from tqdm.auto import tqdm                            # barra de progreso
from pathlib import Path

# Carpeta "data" del proyecto: la busca subiendo desde donde se ejecuta (sirve desde notebooks/ o desde la raíz)
DATA = next((p / "data" for p in [Path.cwd(), *Path.cwd().parents] if (p / "data").is_dir()), Path("data"))
OUT = DATA / "output"                                 # aquí queda data_raw.csv
ahora = pd.Timestamp.now(tz="America/Santiago")       # hora de Chile (Colab corre en hora UTC)
CACHE = DATA / "input" / f"html_{ahora:%Y%m%d}"       # HTML descargado hoy: si el proceso se corta, se retoma desde aquí
for carpeta in (OUT, CACHE): carpeta.mkdir(parents=True, exist_ok=True)
print("RAW →", OUT.resolve(), "\nHTML →", CACHE.resolve())

BASE = "https://www.solotodo.cl"
s = requests.Session()                                # reutiliza la conexión: más rápido que abrir una por página
s.headers["User-Agent"] = "Mozilla/5.0"               # nos presentamos como un navegador común
s.mount("https://", HTTPAdapter(max_retries=Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))

def bajar(ruta, nombre):
    """HTML de BASE + ruta. Si ya se bajó hoy, lo lee del disco; si no, lo descarga, lo guarda comprimido y espera 1 s."""
    archivo = CACHE / f"{nombre}.html.gz"
    if archivo.exists():                              # ya descargado → no se vuelve a pedir al sitio
        return gzip.open(archivo, "rt", encoding="utf-8").read()
    r = s.get(BASE + ruta, timeout=30)
    r.raise_for_status()                              # si el sitio responde error (403, 404…), se detiene en vez de guardar basura
    with gzip.open(archivo, "wt", encoding="utf-8") as f: f.write(r.text)   # se guarda apenas se descarga
    time.sleep(1)                                     # pausa de 1 s entre requests, para no sobrecargar el sitio
    return r.text

# Paso 1 · Listado: recorre /celulares?page=1, 2, 3… y junta los enlaces de cada tarjeta (20 por página)
enlaces, pag = [], 1
while True:
    sopa = BeautifulSoup(bajar(f"/celulares?page={pag}", f"listado_{pag:02d}"), "html.parser")
    hrefs = [a["href"] for a in sopa.find_all("a", href=True)]
    nuevos = [h for h in dict.fromkeys(hrefs)                                  # dict.fromkeys: quita repetidos y mantiene el orden
              if (h.startswith("/products/") or "?product=" in h) and h not in enlaces]   # tarjetas: producto o página de modelo
    if not nuevos: break                                                       # página sin celulares nuevos → se acabó el listado
    enlaces += nuevos
    pag += 1
modelos = [h for h in enlaces if not h.startswith("/products/")]
print(f"{len(enlaces)} celulares en {pag - 1} páginas | {len(modelos)} enlaces a página de modelo (ej: {modelos[:2]})")

# Paso 2 · Enlace → id del producto. Las páginas de modelo (/iphone-17) no muestran precios:
# su HTML trae en un <script> la lista "products":[{"id":…}] y se usa la PRIMERA variante (instrucción de la tarea).
def id_producto(href):
    if href.startswith("/products/"):
        return re.search(r"/products/(\d+)", href)[1]
    ruta = href.split("?")[0]                                                  # "/iphone-17?product=355682" → "/iphone-17"
    html = bajar(ruta, "modelo_" + ruta.strip("/")).replace('\\"', '"')        # el <script> trae las comillas escapadas: \" → "
    return re.search(r'"products":\[\{"id":(\d+)', html)[1]

# Paso 3 · Ficha del producto (todo sale del HTML):
#  a) características, avanzadas y accesorios → pares <dt>etiqueta</dt><dd>valor</dd>
#  b) ofertas de TODAS las tiendas (no solo las 5 visibles) → <script type="application/ld+json">: tienda, URL y condición
#  c) precio normal, precio oferta, cupón y si la oferta exige plan → datos de cada oferta que la página trae embebidos
#     en sus <script> (self.__next_f.push). Se cruzan con (b) por la URL de la tienda.
def producto(pid):
    html = bajar(f"/products/{pid}", f"producto_{pid}")
    sopa = BeautifulSoup(html, "html.parser")
    tag = sopa.find("script", type="application/ld+json")
    ld = json.loads(tag.string) if tag else {}                                 # sin ofertas, la página no trae ld+json
    marca = ld.get("brand", {})
    base = {"nombre": ld.get("name") or sopa.h1.get_text(strip=True),
            "marca": marca.get("name") if isinstance(marca, dict) else marca,
            "id": pid,
            **{dt.get_text(strip=True): " ".join(dd.get_text().split())        # " ".join(...split()) deja el texto en una sola línea
               for dt, dd in zip(sopa.find_all("dt"), sopa.find_all("dd"))}}
    tienda = {o["url"]: o["seller"]["name"] for o in ld.get("offers", {}).get("offers", [])}   # URL de la oferta → nombre de la tienda

    # (c) Los <script> self.__next_f.push([1,"…"]) guardan texto escapado: json.loads lo desescapa y se unen en uno solo
    texto = "".join(json.loads(f'"{t}"') for t in re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html))
    i = texto.find('"entities":')
    ofertas = json.JSONDecoder().raw_decode(texto, i + len('"entities":'))[0] if i >= 0 else []   # lee solo la lista [...]

    filas = [{**base, "tienda": tienda.get(e["external_url"]),
              "precio_normal": e["active_registry"]["normal_price"],
              "precio_oferta": e["active_registry"]["offer_price"],
              "condicion": e["condition"],                                     # NewCondition / RefurbishedCondition / UsedCondition
              "cupon_tipo": (e["best_coupon"] or {}).get("amount_type_text"),  # cupón de descuento, si existe
              "cupon_monto": (e["best_coupon"] or {}).get("amount")}
             for e in ofertas if e["cell_plan"] is None]                       # solo equipos "Liberados" (sin plan de compañía)
    return filas or [base]                                                     # sin ofertas liberadas: queda la ficha sin precios

# Paso 4 · Recorre todos los celulares y guarda data_raw.csv (1 fila = 1 celular en 1 tienda)
filas = []
for h in tqdm(enlaces):
    try:
        filas += producto(id_producto(h))
    except Exception as e:                                                     # un celular con problemas no detiene todo
        print("Error en", h, "→", e)

raw = pd.DataFrame(filas)
raw["fecha_extraccion"] = ahora.strftime("%Y-%m-%d %H:%M")                     # sello de tiempo en cada fila
archivo = f"data_raw_{ahora:%Y%m%d_%H%M}.csv"                                  # ej: data_raw_20261107_1130.csv
raw.to_csv(OUT / archivo, index=False)                                         # copia histórica: cada ejecución crea un archivo nuevo
raw.to_csv(OUT / "data_raw.csv", index=False)                                  # versión vigente, con el nombre que pide el enunciado
print(raw.shape, "| celulares:", raw.nombre.nunique(), "→", OUT / archivo)
