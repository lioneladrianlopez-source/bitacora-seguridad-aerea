import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
import hashlib
from html import escape
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


NTSB_DIRECTORIO = "https://data.ntsb.gov/avdata/filedirectory"
JST_CATALOGO = "https://datos.jst.gob.ar/api/3/action/package_show?id=sucesos-aeronauticos"
MIN_PALABRAS = 40
ARCHIVOS_NTSB = re.compile(
    r'<td id="fileName">up\d{2}[A-Z]{3}\.zip</td>\s*'
    r'<td id="fileDate">\s*(?P<fecha>[^<]+?)\s*</td>\s*'
    r'<td[^>]*>[^<]*</td>\s*<td>\s*'
    r'<a href="(?P<enlace>/avdata/filedirectory/DownloadFile\?fileID=up\d{2}[A-Z]{3}\.zip)"',
    re.IGNORECASE,
)


def descargar(url):
    solicitud = Request(url, headers={"User-Agent": "BitacoraSeguridadAerea/1.0"})
    with urlopen(solicitud, timeout=45) as respuesta:
        return respuesta.read()


def url_ntsb_mas_reciente(pagina, hoy):
    archivos = []
    for coincidencia in ARCHIVOS_NTSB.finditer(pagina):
        fecha = datetime.strptime(coincidencia["fecha"], "%m/%d/%Y %I:%M:%S %p")
        if fecha.date() <= hoy:
            archivos.append((fecha, coincidencia["enlace"]))
    if not archivos:
        raise ValueError("El directorio NTSB no contiene actualizaciones semanales válidas")
    return urljoin(NTSB_DIRECTORIO, max(archivos)[1])


def leer_tabla(base, tabla, columnas):
    resultado = subprocess.run(
        ["mdb-export", "-D", "%Y-%m-%d", "-T", "%Y-%m-%d %H:%M:%S", str(base), tabla],
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    lector = csv.DictReader(io.StringIO(resultado.stdout))
    faltantes = set(columnas) - set(lector.fieldnames or ())
    if faltantes:
        raise ValueError(f"Faltan columnas en {tabla}: {', '.join(sorted(faltantes))}")
    return list(lector)


def leer_ntsb(datos_zip):
    with tempfile.TemporaryDirectory() as temporal:
        base = Path(temporal) / "datos.mdb"
        with zipfile.ZipFile(io.BytesIO(datos_zip)) as archivo:
            bases = [
                miembro for miembro in archivo.infolist()
                if not miembro.is_dir() and miembro.filename.lower().endswith(".mdb")
            ]
            if len(bases) != 1:
                raise ValueError("Se esperaba una sola base MDB en el ZIP de NTSB")
            with archivo.open(bases[0]) as origen, base.open("wb") as destino:
                shutil.copyfileobj(origen, destino)
        eventos = leer_tabla(base, "events", (
            "ev_id", "ntsb_no", "ev_type", "ev_date", "ev_city", "ev_country", "inj_tot_f",
        ))
        aeronaves = leer_tabla(base, "aircraft", (
            "ev_id", "Aircraft_Key", "acft_category", "acft_make", "acft_model", "far_part",
        ))
        narrativas = leer_tabla(base, "narratives", (
            "ev_id", "Aircraft_Key", "narr_accp", "narr_accf",
        ))
    return eventos, aeronaves, narrativas


def texto(fila, campo):
    return (fila.get(campo) or "").strip()


def fecha_valida(valor, hoy):
    try:
        fecha = datetime.fromisoformat(valor).date()
    except ValueError:
        return None
    return fecha if fecha <= hoy else None


def es_comercial(aeronave):
    # Transporte aéreo: líneas aéreas, carga, chárter y operadores extranjeros.
    return texto(aeronave, "far_part").lstrip("0") in {"121", "129", "135"}


def fatalidades(evento):
    valor = texto(evento, "inj_tot_f")
    return int(valor) if valor.isdigit() else None


def candidatos_ntsb(eventos, aeronaves, narrativas, hoy):
    aviones = {}
    for aeronave in aeronaves:
        if texto(aeronave, "acft_category").upper() != "AIR":
            continue
        if texto(aeronave, "acft_make") and texto(aeronave, "acft_model"):
            clave = (texto(aeronave, "ev_id"), texto(aeronave, "Aircraft_Key"))
            if all(clave):
                aviones[clave] = aeronave

    informes = {}
    for narrativa in narrativas:
        identificador = texto(narrativa, "ev_id")
        clave = (identificador, texto(narrativa, "Aircraft_Key"))
        if clave not in aviones:
            continue
        for prioridad, campo, estado in (
            (2, "narr_accf", "Narrativa final NTSB"),
            (1, "narr_accp", "Narrativa preliminar NTSB"),
        ):
            palabras = len(re.findall(r"\b[^\W\d_]+\b", texto(narrativa, campo)))
            if palabras >= MIN_PALABRAS:
                propuesta = (prioridad, palabras, estado)
                informes[identificador] = max(informes.get(identificador, propuesta), propuesta)

    candidatos = []
    for evento in eventos:
        tipo = texto(evento, "ev_type").upper()
        relacionadas = [a for (ev_id, _), a in aviones.items() if ev_id == texto(evento, "ev_id")]
        comercial = any(es_comercial(a) for a in relacionadas)
        if tipo not in {"ACC", "INC"}:
            continue
        if not comercial and not (tipo == "ACC" and (fatalidades(evento) or 0) > 0):
            continue
        informe = informes.get(texto(evento, "ev_id"))
        expediente = texto(evento, "ntsb_no")
        fecha = fecha_valida(texto(evento, "ev_date"), hoy)
        lugar = texto(evento, "ev_city") and texto(evento, "ev_country")
        if informe and expediente and fecha and lugar:
            _, palabras, estado = informe
            candidatos.append((fecha, expediente, estado, palabras))
    return sorted(candidatos, reverse=True)


def leer_jst():
    catalogo = json.loads(descargar(JST_CATALOGO))
    if not isinstance(catalogo, dict) or not catalogo.get("success"):
        raise ValueError("El catálogo de JST no devolvió un resultado válido")
    recurso = next(
        (r for r in catalogo["result"]["resources"] if r["format"].upper() == "JSON"),
        None,
    )
    if recurso is None:
        raise ValueError("El catálogo de JST no ofrece el recurso JSON")
    url = urlsplit(recurso["url"])
    if url.scheme != "https" or url.netloc not in {"datos.jst.gob.ar", "datos2.jst.gob.ar"}:
        raise ValueError("URL del recurso JST fuera del portal oficial")
    # El recurso también se sirve en el dominio del catálogo.
    try:
        contenido = descargar(recurso["url"])
    except OSError:
        if url.netloc != "datos2.jst.gob.ar":
            raise
        contenido = descargar(url._replace(netloc="datos.jst.gob.ar").geturl())
    registros = json.loads(contenido)
    if not isinstance(registros, list) or any(
        not isinstance(registro, dict)
        or not {"fecha_suceso", "clase_suceso", "estado_investigacion"} <= registro.keys()
        for registro in registros
    ):
        raise ValueError("El recurso de JST cambió de estructura")
    return registros


def revisar_ntsb(hoy, zip_local):
    if zip_local:
        datos_zip = zip_local.read_bytes()
        origen = str(zip_local)
    else:
        pagina = descargar(NTSB_DIRECTORIO).decode("utf-8-sig")
        origen = url_ntsb_mas_reciente(pagina, hoy)
        datos_zip = descargar(origen)
    eventos, aeronaves, narrativas = leer_ntsb(datos_zip)
    candidatos = candidatos_ntsb(eventos, aeronaves, narrativas, hoy)
    print(f"NTSB: {len(eventos)} sucesos, {len(candidatos)} candidatos. Fuente: {origen}")
    for fecha, expediente, estado, palabras in candidatos[:10]:
        print(f"  {fecha.isoformat()} | {expediente} | {estado} | {palabras} palabras")
    return eventos, aeronaves, narrativas


def revisar_jst(hoy):
    registros = leer_jst()
    fichas = sum(
        bool(fecha_valida(texto(registro, "fecha_suceso"), hoy))
        and texto(registro, "clase_suceso").casefold() in {"accidente", "incidente grave"}
        for registro in registros
    )
    print(f"JST: {fichas} fichas fechadas de accidentes o incidentes graves.")
    print("Las fichas quedan pendientes de vinculación con el informe o relato oficial.")


INICIO_AUTO = "<!-- BITACORA-AUTO-INICIO -->"
FIN_AUTO = "<!-- BITACORA-AUTO-FIN -->"


def articulos_ntsb(eventos, aeronaves, narrativas, hoy):
    candidatos = {exp for _, exp, _, _ in candidatos_ntsb(eventos, aeronaves, narrativas, hoy)}
    articulos = []
    for evento in eventos:
        exp = texto(evento, "ntsb_no")
        if exp not in candidatos or not re.fullmatch(r"[A-Z0-9]+", exp):
            continue
        ev_id = texto(evento, "ev_id")
        aviones = [a for a in aeronaves if texto(a, "ev_id") == ev_id
                   and texto(a, "acft_category").upper() == "AIR"
                   and texto(a, "acft_make") and texto(a, "acft_model")]
        claves = {texto(a, "Aircraft_Key") for a in aviones}
        relatos = []
        for n in narrativas:
            if texto(n, "ev_id") != ev_id or texto(n, "Aircraft_Key") not in claves:
                continue
            for prioridad, campo in ((1, "narr_accp"), (2, "narr_accf")):
                relato = texto(n, campo)
                if len(re.findall(r"\b[^\W\d_]+\b", relato)) >= MIN_PALABRAS:
                    relatos.append((prioridad, len(relato), relato))
        if not relatos:
            continue
        prioridad, _, relato = max(relatos)
        comercial = any(es_comercial(a) for a in aviones)
        tipo = "Incidente" if texto(evento, "ev_type").upper() == "INC" else "Accidente"
        modelos = ", ".join(sorted({texto(a, "acft_make") + " " + texto(a, "acft_model") for a in aviones}))
        lugar = ", ".join(filter(None, [texto(evento, "ev_city"), texto(evento, "ev_state"), texto(evento, "ev_country")]))
        articulos.append(dict(expediente=exp, identificador=ev_id,
            fecha=fecha_valida(texto(evento, "ev_date"), hoy).isoformat(),
            titulo=f"{tipo}: {modelos} en {texto(evento, 'ev_city')}",
            lugar=lugar, modelos=modelos, tipo=tipo,
            categoria="Transporte aéreo comercial" if comercial else "Aviación general · accidente fatal",
            fatales=fatalidades(evento), prioridad=prioridad,
            estado="Narrativa final NTSB" if prioridad == 2 else "Narrativa preliminar NTSB",
            relato=relato,
            fuente="https://data.ntsb.gov/carol-main-public/basic-search"))
    return sorted(articulos, key=lambda a: (a["fecha"], a["expediente"]), reverse=True)


def pagina_articulo(a, estilos):
    e = lambda campo: escape(str(a[campo]))
    fatal = "No informado" if a["fatales"] is None else str(a["fatales"])
    parrafos = "".join('<p>' + escape(p) + '</p>' for p in re.split(r"\n\s*\n", a["relato"]) if p.strip())
    nota = ("La información es preliminar y puede cambiar. No establece una causa probable."
            if a["prioridad"] == 1 else "Se reproduce la narrativa final disponible; las conclusiones y la causa probable deben consultarse en el expediente oficial.")
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e('titulo')} | Bitácora de Seguridad Aérea</title>{estilos}</head>
<body><header><strong>✈ Bitácora de Seguridad Aérea</strong><nav><a href="../index.html">Inicio</a><a href="../index.html#automaticas">Actualizaciones</a></nav></header>
<main><article><span class="tag">{e('categoria')}</span><h1>{e('titulo')}</h1>
<p>{e('estado')} · Expediente {e('expediente')}</p><div class="card">
<p><strong>Fecha del suceso:</strong> {e('fecha')}<br><strong>Lugar:</strong> {e('lugar')}<br>
<strong>Aeronave:</strong> {e('modelos')}<br><strong>Víctimas fatales informadas:</strong> {fatal}</p>
<p><strong>Publicado:</strong> {e('publicado')} · <strong>Actualizado:</strong> {e('actualizado')}</p></div>
<p>{nota}</p><h2>Relato oficial de NTSB</h2>
<p>Texto original en inglés. La ficha en castellano presenta los datos estructurados del suceso.</p>
<section lang="en" class="card" style="overflow-wrap:anywhere">{parrafos}</section>
<p><a href="{e('fuente')}">Buscar el expediente {e('expediente')} en NTSB CAROL</a></p>
<p><a href="https://data.ntsb.gov/avdata/filedirectory">Base oficial utilizada</a> · Consulta: {e('actualizado')}</p>
</article></main><footer>Conocer los hechos para volar más seguros</footer></body></html>\n'''


def publicar_ntsb(raiz, datos, hoy):
    indice = raiz / "index.html"
    portada = indice.read_text(encoding="utf-8")
    estilos = re.search(r"<style>.*?</style>", portada, re.S)
    if not estilos or "</section>" not in portada:
        raise ValueError("La portada no tiene la estructura esperada; no se publicará")
    manifiesto = raiz / "data" / "ntsb-publicados.json"
    guardados = json.loads(manifiesto.read_text(encoding="utf-8")) if manifiesto.exists() else {}
    if not isinstance(guardados, dict) or any(not re.fullmatch(r"[A-Z0-9]+", k) for k in guardados):
        raise ValueError("Registro de publicaciones inválido")
    # Los expedientes ya redactados se conservan y no generan otra página.
    editoriales = "\n".join(p.read_text(encoding="utf-8") for p in raiz.glob("*.html") if p.name != "index.html")
    nuevos = 0
    cambios = 0
    for a in articulos_ntsb(*datos, hoy):
        exp = a["expediente"]
        previo = guardados.get(exp)
        if not previo and (exp in editoriales or a["fecha"] < (hoy - timedelta(days=90)).isoformat()):
            continue
        if not previo and nuevos >= 5:
            continue
        huella = hashlib.sha256(json.dumps(a, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if previo and (previo.get("huella") == huella or previo["prioridad"] > a["prioridad"]):
            continue
        a.update(huella=huella, publicado=previo["publicado"] if previo else hoy.isoformat(), actualizado=hoy.isoformat())
        guardados[exp] = a
        nuevos += int(previo is None)
        cambios += 1
    if not cambios:
        print("Publicación: sin cambios; no se modificó el sitio.")
        return
    tarjetas = []
    for a in sorted(guardados.values(), key=lambda a: (a["actualizado"], a["fecha"], a["expediente"]), reverse=True):
        e = lambda campo: escape(str(a[campo]))
        tarjetas.append(f'<div class="card"><span class="tag">{e("categoria")} · {e("estado")}</span><h3>{e("titulo")}</h3><p>Suceso: {e("fecha")} · Actualización: {e("actualizado")}</p><p>{e("lugar")}</p><a href="articulos/ntsb-{e("expediente")}.html">Leer expediente →</a></div>')
    bloque = INICIO_AUTO + '<section id="automaticas"><h2 class="section">Últimas publicaciones · NTSB</h2><p>Transporte aéreo comercial y accidentes fatales de aviación general. Ordenados por actualización; la fecha del suceso figura por separado.</p><div class="grid">' + "\n".join(tarjetas) + '</div></section>' + FIN_AUTO
    if INICIO_AUTO in portada:
        if portada.count(INICIO_AUTO) != 1 or portada.count(FIN_AUTO) != 1:
            raise ValueError("Marcadores de portada inválidos")
        portada = re.sub(re.escape(INICIO_AUTO) + r".*?" + re.escape(FIN_AUTO), lambda _: bloque, portada, flags=re.S)
    elif FIN_AUTO in portada:
        raise ValueError("Marcadores de portada incompletos")
    else:
        portada = portada.replace("</section>", "</section>" + bloque, 1)
    # Generar todo antes de escribir; portada al final para no enlazar páginas ausentes.
    paginas = {raiz / "articulos" / f"ntsb-{exp}.html": pagina_articulo(a, estilos.group()) for exp, a in guardados.items()}
    paginas[manifiesto] = json.dumps(guardados, ensure_ascii=False, indent=2) + "\n"
    paginas[indice] = portada
    for destino, contenido in paginas.items():
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_suffix(destino.suffix + ".tmp")
        temporal.write_text(contenido, encoding="utf-8")
        temporal.replace(destino)
    print(f"Publicación: {nuevos} artículos nuevos, {cambios - nuevos} actualizados.")


def main():
    parser = argparse.ArgumentParser(description="Revisar candidatos para la Bitácora.")
    parser.add_argument("--ntsb-zip", type=Path, help="Usar un ZIP local de NTSB para pruebas.")
    parser.add_argument("--publicar", action="store_true", help="Generar artículos y actualizar la portada.")
    args = parser.parse_args()
    hoy = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    fuentes = (("NTSB", revisar_ntsb, (hoy, args.ntsb_zip)), ("JST", revisar_jst, (hoy,)))
    if args.publicar:
        # JST todavía no genera artículos: una consulta ajena no debe bloquear NTSB.
        fuentes = fuentes[:1]
    errores = []
    datos_ntsb = None
    for nombre, revisar, parametros in fuentes:
        try:
            resultado = revisar(*parametros)
            if nombre == "NTSB":
                datos_ntsb = resultado
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
            detalle = getattr(error, "stderr", None) or str(error)
            errores.append(f"{nombre}: {detalle.strip()}")
    if args.publicar and datos_ntsb is not None:
        publicar_ntsb(Path(__file__).resolve().parents[1], datos_ntsb, hoy)
    else:
        print("Revisión terminada. No se generaron publicaciones.")
    if errores:
        parser.exit(1, "\n".join(errores) + "\n")


if __name__ == "__main__":
    main()
