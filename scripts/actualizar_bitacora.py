import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
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
            "ev_id", "ntsb_no", "ev_type", "ev_date", "ev_city", "ev_country",
        ))
        aeronaves = leer_tabla(base, "aircraft", (
            "ev_id", "Aircraft_Key", "acft_category", "acft_make", "acft_model",
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
        # La exportación identifica incidentes, pero no confirma su gravedad.
        if texto(evento, "ev_type") != "ACC":
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


def revisar_jst(hoy):
    registros = leer_jst()
    fichas = sum(
        bool(fecha_valida(texto(registro, "fecha_suceso"), hoy))
        and texto(registro, "clase_suceso").casefold() in {"accidente", "incidente grave"}
        for registro in registros
    )
    print(f"JST: {fichas} fichas fechadas de accidentes o incidentes graves.")
    print("Las fichas quedan pendientes de vinculación con el informe o relato oficial.")


def main():
    parser = argparse.ArgumentParser(description="Revisar candidatos para la Bitácora.")
    parser.add_argument("--ntsb-zip", type=Path, help="Usar un ZIP local de NTSB para pruebas.")
    args = parser.parse_args()
    hoy = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    fuentes = (("NTSB", revisar_ntsb, (hoy, args.ntsb_zip)), ("JST", revisar_jst, (hoy,)))
    errores = []
    for nombre, revisar, parametros in fuentes:
        try:
            revisar(*parametros)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
            detalle = getattr(error, "stderr", None) or str(error)
            errores.append(f"{nombre}: {detalle.strip()}")
    print("Revisión de candidatos terminada. No se generaron publicaciones.")
    if errores:
        parser.exit(1, "\n".join(errores) + "\n")


if __name__ == "__main__":
    main()
