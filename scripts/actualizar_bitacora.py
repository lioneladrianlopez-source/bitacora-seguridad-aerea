
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
