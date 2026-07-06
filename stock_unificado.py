# -*- coding: utf-8 -*-
"""
SISTEMA DE STOCK UNIFICADO PARA GESIO
=====================================
Descarga los feeds de Bimbidreams y Cambrass, inyecta 0 a los EAN que
DESAPARECEN del feed (anti stock-fantasma), lo unifica TODO en un solo CSV
y lo publica para que Gesio lo lea por URL una vez al dia.

- Bimbidreams llega por URL publica; Cambrass por FTP con usuario y
  contrasena (Secrets de GitHub, nunca en el codigo). La salida se publica
  como fichero del propio repositorio (GitHub Pages / raw URL).
- Pensado para correr en GitHub Actions (cron diario), pero funciona igual
  en local (python stock_unificado.py).

Regla de oro (NO tocar sin pensar):
  El UNICO disparador para poner un EAN a 0 es:
    (a) que el proveedor mande stock 0 explicito, o
    (b) que el EAN DESAPAREZCA del feed.
  "Descatalogado con stock > 0" SE SIGUE VENDIENDO -> nunca se pone a 0.

Freno de seguridad:
  Si un feed llega roto o a medias (menos filas de lo razonable, o sin las
  columnas esperadas), el proceso ABORTA sin publicar nada. Asi Gesio sigue
  leyendo el fichero bueno de ayer en lugar de reventar el catalogo a 0.
"""

import csv
import io
import json
import os
import sys
import urllib.request
import datetime

# ============================ CONFIG ============================
HOY = datetime.date.today()

# --- SALIDA (formato EXACTO que pidio Gesio) -------------------
OUT_CSV      = "docs/stock.csv"   # se publica via GitHub Pages (carpeta /docs)
OUT_DELIM    = ";"                # separador de columnas
OUT_QUOTECHAR = '"'              # delimitador de textos
OUT_QUOTING  = csv.QUOTE_ALL      # envuelve cada campo entre comillas dobles
OUT_HEADER   = ["SKU", "stock"]   # poner None si Gesio NO quiere fila de cabecera
OUT_ENCODING = "utf-8"           # sin BOM

# Dias que seguimos mandando 0 a un EAN desaparecido antes de soltarlo.
# (Cuando lleva mucho fuera ya esta asentado a 0 en Gesio; dejar de mandarlo
#  no lo resucita, porque "lo que no aparece, Gesio no lo toca".)
RETENCION_DIAS = 60

# Freno de seguridad: minimo de filas validas que esperamos de cada feed.
# Si llega menos -> feed roto -> ABORTA sin publicar.
MIN_FILAS = {"bimbidreams": 200, "cambrass": 500}

# Si un mismo EAN aparece en los dos proveedores, que stock mandamos:
#   "max" -> el mayor de los dos (conservador, recomendado)
#   "sum" -> la suma (solo si de verdad se acumula stock de ambos)
COMBINAR = "max"

# Las URLs/credenciales de los feeds NO van escritas aqui: se leen de
# variables cifradas.
#   - En GitHub: se definen como "Secrets" (BIMBI_FEED_URL, CAMBRASS_FTP_*).
#   - En local: se ponen en el fichero secrets.local.env (ignorado por git).
#
# Cambrass (desde jul-2026): la URL publica antigua quedo desfasada (sin
# colecciones nuevas); el stock vivo se sirve por FTP en dos Excel .xls.
PROVEEDORES = {
    "bimbidreams": {
        "tipo":      "csv_url",
        "url_env":   "BIMBI_FEED_URL",
        "delim":     ",",
        "col_ean":   "codigo-barras",
        "col_stock": "stock",
    },
    "cambrass": {
        "tipo":      "xls_ftp",
        "host_env":  "CAMBRASS_FTP_HOST",
        "user_env":  "CAMBRASS_FTP_USER",
        "pass_env":  "CAMBRASS_FTP_PASS",
        "ficheros":  ["Stock1.xls", "Stock2.xls"],
        "col_ean":   "EAN",
        "col_stock": "Disponible",
        # Complemento: Cambrass excluye del FTP los articulos baratos (los saca
        # del dropshipping), pero SI los sirve y Nenelandia SI los vende. Para
        # esos EAN (y SOLO para los que no vengan en el FTP) seguimos leyendo
        # la URL antigua. El FTP siempre manda si un EAN esta en los dos.
        "extra_csv": {
            "url_env":   "CAMBRASS_FEED_URL",
            "delim":     ";",
            "col_ean":   "EAN13",
            "col_stock": "STOCK",
        },
    },
}

MASTERS_DIR = "masters"  # historico de EAN vistos, un JSON por proveedor
# ===============================================================


def abort(msg):
    """Sale con error SIN publicar. GitHub Actions lo marca como fallo y avisa."""
    print("ERROR (no se publica nada):", msg, file=sys.stderr)
    sys.exit(1)


def cargar_env_local():
    """Para ejecuciones EN LOCAL: carga el fichero secrets.local.env (ignorado
    por git) con lineas CLAVE=VALOR. No pisa variables ya definidas (en GitHub
    mandan los Secrets)."""
    if not os.path.exists("secrets.local.env"):
        return
    with open("secrets.local.env", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def es_ean(x):
    return x.isdigit() and 12 <= len(x) <= 14


def to_int_stock(s):
    try:
        return max(0, int(round(float(str(s).strip().replace(",", ".")))))
    except Exception:
        return 0


def descargar(url):
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        abort(f"No se pudo descargar {url}: {e}")


def descargar_ftp(host, user, password, ficheros, nombre):
    """Descarga varios ficheros de un FTP en una sola conexion.
    Devuelve {nombre_fichero: bytes}."""
    from ftplib import FTP
    out = {}
    try:
        with FTP(host, timeout=90) as ftp:
            ftp.login(user, password)
            for fich in ficheros:
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {fich}", buf.write)
                out[fich] = buf.getvalue()
    except Exception as e:
        abort(f"{nombre}: fallo el FTP {host}: {e}")
    return out


def parse_xls(contenido, cfg, nombre, fichero):
    """Lee un Excel .xls (primera hoja, cabecera en la fila 1) y devuelve
    {ean: stock}. Requiere el paquete 'xlrd' (pip install xlrd)."""
    try:
        import xlrd
    except ImportError:
        abort("Falta el paquete 'xlrd' para leer los .xls de Cambrass "
              "(pip install xlrd).")
    try:
        wb = xlrd.open_workbook(file_contents=contenido)
        sh = wb.sheet_by_index(0)
    except Exception as e:
        abort(f"{nombre}: {fichero} no es un .xls legible: {e}")
    if sh.nrows < 2:
        abort(f"{nombre}: {fichero} llego vacio (solo {sh.nrows} filas).")
    cabecera = [str(sh.cell_value(0, j)).strip() for j in range(sh.ncols)]
    if cfg["col_ean"] not in cabecera or cfg["col_stock"] not in cabecera:
        abort(f"{nombre}: {fichero} no trae las columnas esperadas "
              f"({cfg['col_ean']}, {cfg['col_stock']}). Cabecera: {cabecera}")
    i_ean, i_stock = cabecera.index(cfg["col_ean"]), cabecera.index(cfg["col_stock"])
    out = {}
    for i in range(1, sh.nrows):
        ean = str(sh.cell_value(i, i_ean)).strip()
        if ean.endswith(".0"):          # xlrd devuelve floats en celdas numericas
            ean = ean[:-2]
        if not es_ean(ean):
            continue
        st = to_int_stock(sh.cell_value(i, i_stock))
        out[ean] = max(out.get(ean, 0), st)  # si hay EAN repetido, el mayor
    return out


def leer_proveedor(nombre, cfg):
    """Descarga y parsea el feed de un proveedor segun su tipo.
    Devuelve {ean: stock}."""
    if cfg.get("tipo") == "xls_ftp":
        host = os.environ.get(cfg["host_env"])
        user = os.environ.get(cfg["user_env"])
        pwd  = os.environ.get(cfg["pass_env"])
        if not (host and user and pwd):
            abort(f"Faltan credenciales FTP de {nombre}: define los Secrets "
                  f"'{cfg['host_env']}', '{cfg['user_env']}' y '{cfg['pass_env']}' "
                  f"en GitHub (o en secrets.local.env para pruebas).")
        contenidos = descargar_ftp(host, user, pwd, cfg["ficheros"], nombre)
        out = {}
        for fich, datos in contenidos.items():
            parcial = parse_xls(datos, cfg, nombre, fich)
            print(f"[{nombre}] {fich}: {len(parcial)} EAN validos")
            for ean, st in parcial.items():
                out[ean] = max(out.get(ean, 0), st)
        # Complemento CSV (articulos que el proveedor excluye del FTP):
        # solo aporta EAN que el FTP NO trae; nunca pisa un dato del FTP.
        extra = cfg.get("extra_csv")
        if extra:
            url = os.environ.get(extra["url_env"])
            if not url:
                abort(f"Falta la URL complementaria de {nombre}: define el "
                      f"Secret '{extra['url_env']}'.")
            aportados = 0
            for ean, st in parse_feed(descargar(url), extra, nombre).items():
                if ean not in out:
                    out[ean] = st
                    aportados += 1
            print(f"[{nombre}] URL complementaria: {aportados} EAN "
                  f"que el FTP no traia")
        return out
    # tipo por defecto: CSV por URL
    url = os.environ.get(cfg["url_env"])
    if not url:
        abort(f"Falta la URL del feed de {nombre}: define el Secret "
              f"'{cfg['url_env']}' en GitHub (o en secrets.local.env para pruebas).")
    return parse_feed(descargar(url), cfg, nombre)


def parse_feed(texto, cfg, nombre):
    reader = csv.DictReader(io.StringIO(texto), delimiter=cfg["delim"])
    campos = [c.strip().strip('"') for c in (reader.fieldnames or [])]
    if cfg["col_ean"] not in campos or cfg["col_stock"] not in campos:
        abort(f"{nombre}: el feed no trae las columnas esperadas "
              f"({cfg['col_ean']}, {cfg['col_stock']}). Cabecera recibida: {campos}")
    reader.fieldnames = campos  # cabeceras normalizadas (sin comillas/espacios)
    out = {}
    for r in reader:
        ean = (r.get(cfg["col_ean"]) or "").strip()
        if not es_ean(ean):
            continue
        st = to_int_stock(r.get(cfg["col_stock"]))
        out[ean] = max(out.get(ean, 0), st)  # si hay EAN repetido, el mayor
    return out


def cargar_master(nombre):
    path = os.path.join(MASTERS_DIR, f"{nombre}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_master(nombre, master):
    os.makedirs(MASTERS_DIR, exist_ok=True)
    path = os.path.join(MASTERS_DIR, f"{nombre}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=0, sort_keys=True)


def combina(actual, nuevo):
    return actual + nuevo if COMBINAR == "sum" else max(actual, nuevo)


def main():
    cargar_env_local()
    # --- FASE 1: descargar y validar TODO antes de tocar nada -------------
    parsed = {}
    for nombre, cfg in PROVEEDORES.items():
        actual = leer_proveedor(nombre, cfg)
        if len(actual) < MIN_FILAS[nombre]:
            abort(f"{nombre}: solo {len(actual)} filas validas "
                  f"(< minimo {MIN_FILAS[nombre]}). Feed sospechoso de estar roto.")
        parsed[nombre] = actual
        print(f"[{nombre}] {len(actual)} EAN validos en el feed de hoy")

    # --- FASE 2: actualizar maestros y construir la salida ----------------
    salida = {}          # ean -> stock final
    resumen = {}
    for nombre, actual in parsed.items():
        master = cargar_master(nombre)
        # EAN presentes hoy -> stock real + marcar como vistos
        for ean, st in actual.items():
            master[ean] = HOY.isoformat()
            salida[ean] = combina(salida.get(ean, 0), st)
        # EAN del maestro que YA NO aparecen -> 0 (dentro de la retencion)
        desap = 0
        for ean in list(master.keys()):
            if ean in actual:
                continue
            dias = (HOY - datetime.date.fromisoformat(master[ean])).days
            if dias <= RETENCION_DIAS:
                salida.setdefault(ean, 0)  # 0, pero sin pisar stock>0 de otro prov.
                desap += 1
            else:
                del master[ean]            # asentado a 0, lo soltamos
        guardar_master(nombre, master)
        resumen[nombre] = (len(actual), desap)

    # --- FASE 3: escribir el CSV unificado --------------------------------
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", encoding=OUT_ENCODING, newline="") as f:
        w = csv.writer(f, delimiter=OUT_DELIM, quotechar=OUT_QUOTECHAR,
                       quoting=OUT_QUOTING)
        if OUT_HEADER:
            w.writerow(OUT_HEADER)
        for ean in sorted(salida):
            w.writerow([ean, salida[ean]])

    con_stock = sum(1 for v in salida.values() if v > 0)
    print("\n=== RESUMEN ===")
    for nombre, (n, d) in resumen.items():
        print(f"  {nombre:12s}: {n} en feed | {d} desaparecidos->0")
    print(f"  TOTAL filas en {OUT_CSV}: {len(salida)} "
          f"({con_stock} con stock, {len(salida)-con_stock} a 0)")


if __name__ == "__main__":
    main()
