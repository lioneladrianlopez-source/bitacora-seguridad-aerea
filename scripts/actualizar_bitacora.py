
import json
import urllib.request

API = (
    "https://datos.jst.gob.ar/api/3/action/"
    "package_show?id=sucesos-aeronauticos"
)

def descargar_json(url):
    solicitud = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BitacoraSeguridadAerea/1.0",
            "Accept": "application/json"
        }
    )
    with urllib.request.urlopen(
        solicitud, timeout=60
    ) as respuesta:
        return json.load(respuesta)

def main():
    print("BITÁCORA DE SEGURIDAD AÉREA")
    print("Análisis de datos oficiales JST")

    catalogo = descargar_json(API)

    if not catalogo.get("success"):
        raise ValueError("Error en catálogo JST")

    recursos = catalogo["result"]["resources"]

    recurso = next(
        (
            r for r in recursos
            if r.get("format", "").upper() == "JSON"
        ),
        None
    )

    if recurso is None:
        raise ValueError("No se encontró recurso JSON")

    print("Descargando datos oficiales...")
    datos = descargar_json(recurso["url"])

    print("Tipo de datos:", type(datos).__name__)

    if isinstance(datos, list):
        print("Total de registros:", len(datos))
        registros = datos[:2]
    elif isinstance(datos, dict):
        print("Claves principales:", list(datos.keys()))
        registros = [datos]
    else:
        raise ValueError("Formato no reconocido")

    print("MUESTRA DE ESTRUCTURA:")
    print(
        json.dumps(
            registros,
            ensure_ascii=False,
            indent=2
        )[:6000]
    )

    print("Análisis completado")
    print("No se modificaron archivos")

if __name__ == "__main__":
    main()
