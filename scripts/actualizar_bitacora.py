

from datetime import datetime, timezone
from pathlib import Path
import json
import urllib.request

def actualizar_bitacora():
    print("BITÁCORA DE SEGURIDAD AÉREA")
    print("Comprobación de fuentes oficiales")
    print("Fecha UTC:", datetime.now(timezone.utc))

    if not Path("index.html").exists():
        raise FileNotFoundError(
            "No se encontró index.html"
        )

    url = (
        "https://datos.jst.gob.ar/api/3/action/"
        "package_show?id=sucesos-aeronauticos"
    )

    print("Consultando catálogo de la JST...")

    solicitud = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BitacoraSeguridadAerea/1.0",
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(
            solicitud, timeout=30
        ) as respuesta:
            datos = json.load(respuesta)

        if not datos.get("success"):
            raise ValueError(
                "La API no confirmó la consulta"
            )

        recursos = datos["result"].get(
            "resources", []
        )

        print("Consulta completada")
        print("Recursos encontrados:", len(recursos))

        for recurso in recursos:
            print(
                recurso.get("name", "Sin nombre"),
                recurso.get("format", ""),
                recurso.get("url", "")
            )

    except Exception as error:
        print("No se pudo consultar la JST:")
        print(type(error).__name__, str(error))
        raise

    print("Prueba finalizada")
    print("No se modificaron archivos")

if __name__ == "__main__":
    actualizar_bitacora()
from datetime import datetime, timezone
from pathlib import Path

def actualizar_bitacora():
    print("BITÁCORA DE SEGURIDAD AÉREA")
    print("Iniciando sistema de actualización")

    fecha = datetime.now(timezone.utc)
    print("Fecha UTC:", fecha.isoformat())

    archivo = Path("index.html")

    if not archivo.exists():
        raise FileNotFoundError(
            "No se encontró index.html"
        )

    contenido = archivo.read_text(
        encoding="utf-8"
    )

    print("Archivo principal encontrado")
    print("Caracteres:", len(contenido))
    print("Verificación inicial completada")
    print("No se realizaron modificaciones")

if __name__ == "__main__":
    actualizar_bitacora()
