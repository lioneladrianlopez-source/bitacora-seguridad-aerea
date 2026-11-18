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
from collections import Counter
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


TRADUCCIONES_REVISADAS = {'df742a0f82d20d696153f77f82a61ba6661cae2f30bf249f0570c384ee5d7826': 'El 24 de octubre de 2026, aproximadamente a las 08:22, hora de verano de la zona montañosa, un avión Piper PA-28-235, matrícula N8777W, resultó destruido en un accidente cerca de Gunnison, Colorado. El piloto y los dos pasajeros sufrieron lesiones fatales. La aeronave realizaba un vuelo personal conforme a la Parte 91 del Título 14 del Código de Reglamentos Federales de Estados Unidos.\n\nSegún la información preliminar, el piloto había previsto despegar el 23 de octubre de 2026 para realizar un vuelo bajo reglas de vuelo visual desde el aeropuerto Rifle Garfield County (RIL), en Rifle, Colorado, hasta el aeropuerto regional Gunnison-Crested Butte (GUC), en Gunnison, Colorado. Posteriormente cambió la salida al 24 de octubre debido a un compromiso personal con los pasajeros. El piloto sabía que el 23 de octubre el tiempo estaría «despejado», pero para el vuelo del 24 quería salir «muy temprano por la mañana», porque consideraba que entonces estaría «despejado». El 24 de octubre, a las 07:46, el vuelo partió de RIF —identificador consignado así en el relato original— sin plan de vuelo y no recibió servicios de control de tránsito aéreo durante el trayecto a GUC.\n\nLos datos ADS-B mostraron que el avión seguía una trayectoria hacia el sudeste desde RIL, aproximadamente a 1 milla náutica (nm) al oeste de la cadena montañosa Anthracite, a unos 10.000 pies sobre el nivel medio del mar (MSL). Luego efectuó un viraje a la izquierda de aproximadamente 270° hacia el oeste, a unos 9.500 pies MSL, seguido por un viraje a la derecha hacia el noroeste. El informe original remite a su figura 1, correspondiente a la trayectoria ADS-B; esa imagen no se reproduce aquí.\n\nUn testigo que estaba cazando en la zona declaró que se encontraba entre 9.300 y 9.500 pies MSL cuando el avión pasó hacia el norte y, por su sonido, parecía volar «muy bajo». Estimó que se encontraba a unos 100 pies sobre el terreno (AGL) y a unos 300 a 400 pies de su posición, y afirmó que «no había forma de que [el avión] superara los Anthracites». Dijo que la visibilidad «no era muy buena» y se preguntó por qué el piloto volaba ese día. Oyó al avión atravesar el valle y realizar «un par de vueltas», y no sabía si el piloto estaba explorando las cuencas para encontrar un paso a través de la cadena montañosa. Agregó que las cumbres estaban bastante cubiertas de nubes, aunque estas no cubrían las copas de los árboles.\n\nUn segundo testigo, situado a varios cientos de yardas del lugar del accidente, dijo que estaba observando el paso de montaña cuando vio al avión sobrevolarlo y pasar detrás de él mientras efectuaba un viraje. Parecía que iba a cruzar nuevamente el paso, donde había nubes y algo de nieve. Mientras lo observaba, podía oírlo. Declaró que parecía «caer en vertical» y que oyó «aumentar las revoluciones del motor» durante el descenso. Según su relato, el avión parecía haber efectuado un viraje de 180° y estar regresando cuando cayó en vertical. Añadió que apenas logró superar el paso y que, segundos después, salió de las nubes y descendió directamente. Afirmó que, durante todo el tiempo que oyó u observó la aeronave, esta «sonaba bien».\n\nEl lugar del accidente se encontraba aproximadamente a 18 nm al noroeste de GUC. La inspección reveló una trayectoria de restos entre los árboles orientada aproximadamente a 102°, a una elevación de unos 10.400 pies MSL. Todos los componentes principales del avión fueron localizados en el lugar. Las alas se habían separado del fuselaje de una manera compatible con impactos contra árboles y terreno; el fuselaje estaba orientado aproximadamente a 137°, medidos de la cola hacia la nariz. El motor se había separado de la estructura y las palas de la hélice presentaban torsiones y dobleces en forma de S.\n\nLos restos de la aeronave fueron retenidos para continuar su examen.\n\nEl pronóstico de aeródromo para GUC —elevación del aeropuerto: 7.680 pies MSL—, emitido a las 05:20 y válido el 24 de octubre de 2026 desde las 06:00, indicaba visibilidad superior a 6 millas terrestres (sm) y un techo de 15.000 pies AGL. Entre las 07:00 y las 10:00 se pronosticaban fluctuaciones temporarias: visibilidad de 4 sm, chaparrones y lluvia de intensidad débil, y techo con nubosidad fragmentada a 5.000 pies AGL.\n\nLas observaciones meteorológicas de GUC registraron:\nA las 05:56: visibilidad de 10 sm y cielo despejado.\nA las 06:56: visibilidad de 10 sm, pocas nubes a 7.500 pies AGL y nubosidad dispersa a 12.000 pies AGL.\nA las 07:56: visibilidad de 6 sm, lluvia moderada y neblina, con techo de nubosidad fragmentada a 3.200 pies AGL y cielo cubierto a 4.400 pies AGL.\n\nDe acuerdo con los registros del piloto, el 3 de abril de 2024 obtuvo su licencia de piloto privado con habilitación para avión monomotor terrestre. Realizó su revisión de vuelo más reciente el 18 de abril de 2026 y, al momento del accidente, había acumulado unas 278 horas de vuelo.'}


def traducir_relato(original):
    huella = hashlib.sha256(original.encode("utf-8")).hexdigest()
    if huella in TRADUCCIONES_REVISADAS:
        return TRADUCCIONES_REVISADAS[huella], "Traducción revisada"
    import argostranslate.package
    import argostranslate.translate
    instalados = argostranslate.package.get_installed_packages()
    if not any(p.from_code == "en" and p.to_code == "es" for p in instalados):
        argostranslate.package.update_package_index()
        paquete = next((p for p in argostranslate.package.get_available_packages()
                        if p.from_code == "en" and p.to_code == "es"), None)
        if paquete is None:
            raise ValueError("No se encontró el modelo de traducción al castellano")
        argostranslate.package.install_from_path(paquete.download())
    traducido = argostranslate.translate.translate(original, "en", "es").strip()
    if not traducido or traducido == original or len(traducido) < len(original) * 0.5:
        raise ValueError("Traducción incompleta; se cancela la publicación")
    # Ante cambios de cifras, conservar la versión publicada y pedir revisión.
    cifras = lambda t: Counter(re.sub(r"[,.]", "", n) for n in re.findall(r"\d+(?:[,.]\d+)*", t))
    if cifras(original) != cifras(traducido):
        raise ValueError("La traducción alteró cifras; se cancela la publicación para revisión")
    return traducido, "Traducción automática"


def preparar_castellano(a):
    huella = hashlib.sha256(a["relato"].encode("utf-8")).hexdigest()
    cambio = False
    if not a.get("relato_es") or a.get("traduccion_fuente") != huella:
        a["relato_es"], a["traduccion_tipo"] = traducir_relato(a["relato"])
        a["traduccion_fuente"] = huella
        cambio = True
    if a.get("version_idioma") != 2:
        a["version_idioma"] = 2
        a["lugar"] = a["lugar"].replace(", CO, USA", ", Colorado, Estados Unidos").replace(", USA", ", Estados Unidos")
        cambio = True
    return cambio


def pagina_articulo(a, estilos):
    e = lambda campo: escape(str(a[campo]))
    fatal = "No informado" if a["fatales"] is None else str(a["fatales"])
    if not a.get("relato_es"):
        raise ValueError("No se puede publicar un relato sin traducción al castellano")
    formatear = lambda t: "".join('<p>' + escape(p).replace('\n', '<br>') + '</p>' for p in re.split(r"\n\s*\n", t) if p.strip())
    parrafos = formatear(a["relato_es"])
    original = formatear(a["relato"])
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
<p>{nota}</p><h2>Relato del suceso en castellano</h2>
<p>{escape(a.get('traduccion_tipo', 'Traducción al castellano'))} del texto de NTSB. Ante una discrepancia, prevalece el documento oficial.</p>
<section lang="es" class="card" style="overflow-wrap:anywhere">{parrafos}</section>
<details class="card" style="margin-top:20px"><summary style="cursor:pointer;font-weight:700">Leer texto original (inglés)</summary>
<section lang="en" style="overflow-wrap:anywhere">{original}</section></details>
<p><a href="{e('fuente')}">Buscar el expediente {e('expediente')} en NTSB CAROL</a></p>
<p><a href="https://data.ntsb.gov/avdata/filedirectory">Base oficial utilizada</a> · Consulta: {e('actualizado')}</p>
</article></main><footer>Conocer los hechos para volar más seguros</footer></body></html>\n'''


def encabezado_actualizacion(portada, fecha):
    meses = ("enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
    dia = datetime.fromisoformat(fecha).date()
    etiqueta = f"Última actualización · {dia.day} de {meses[dia.month - 1]} de {dia.year}"
    portada = re.sub(r'(<h2 id="semanal" class="section">).*?(</h2>)',
                     lambda m: m[1] + etiqueta + m[2], portada, count=1, flags=re.S)
    portada = portada.replace('href="index.html#semanal">Resumen semanal</a>',
                              'href="index.html#semanal">Última actualización</a>')
    portada = portada.replace(
        "Selección de novedades de la semana. Cada entrada enlaza a su expediente dentro de la categoría correspondiente.",
        "Selección de publicaciones y actualizaciones. Cada entrada enlaza a su expediente dentro de la categoría correspondiente.")
    return portada


def compactar_portada(portada):
    def tarjeta(coincidencia):
        bloque = coincidencia[0]
        actualizada = re.search(r"Actualización: ([0-9-]+)", bloque)
        bloque = re.sub(r"<p\\b[^>]*>.*?</p>", "", bloque, flags=re.S)
        bloque = re.sub(r"(<a\\b[^>]*>).*?(</a>)", lambda m: m[1] + "Leer artículo →" + m[2], bloque, flags=re.S)
        bloque = re.sub(r'(<span class="tag">)(.*?)(</span>)',
                        lambda m: m[1] + (actualizada[1] if actualizada else m[2].split("·")[0].strip()) + m[3],
                        bloque, flags=re.S)
        return bloque
    def seccion(m):
        return re.sub(r'<div class="card">.*?</div>', tarjeta, m[0], flags=re.S)
    portada = re.sub(r'<h2 id="semanal".*?(?=<h2 class="section" id="accidentes">)',
                     seccion, portada, flags=re.S)
    return re.sub(re.escape(INICIO_AUTO) + r".*?" + re.escape(FIN_AUTO),
                  seccion, portada, flags=re.S)


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
    modificados = set()
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
        modificados.add(exp)
    for a in guardados.values():
        if preparar_castellano(a):
            a["actualizado"] = hoy.isoformat()
            cambios += int(a["expediente"] not in modificados)
    if guardados:
        nueva_portada = encabezado_actualizacion(
            portada, hoy.isoformat())
        if nueva_portada != portada:
            portada = nueva_portada
            cambios += 1
    compacta = compactar_portada(portada)
    if compacta != portada:
        portada = compacta
        cambios += 1
    if not cambios:
        print("Publicación: sin cambios; no se modificó el sitio.")
        return
    tarjetas = []
    for a in sorted(guardados.values(), key=lambda a: (a["actualizado"], a["fecha"], a["expediente"]), reverse=True):
        e = lambda campo: escape(str(a[campo]))
        tarjetas.append(f'<div class="card"><span class="tag">{e("actualizado")}</span><h3>{e("titulo")}</h3><a href="articulos/ntsb-{e("expediente")}.html">Leer artículo →</a></div>')
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
    parser.add_argument("--solo-idioma", action="store_true", help="Traducir los artículos guardados sin consultar nuevas fuentes.")
    args = parser.parse_args()
    hoy = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    if args.solo_idioma:
        publicar_ntsb(Path(__file__).resolve().parents[1], ([], [], []), hoy)
        return
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
