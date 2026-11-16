
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

URL_NTSB = (
    "https://data.ntsb.gov/avdata/FileDirectory/"
    "up15NOV.zip"
)

def ejecutar(comando):
    resultado = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        check=True
    )
    return resultado.stdout.strip()

def main():
    print("BITÁCORA DE SEGURIDAD AÉREA", flush=True)
    print("Prueba de lectura NTSB", flush=True)

    with tempfile.TemporaryDirectory() as carpeta:
        carpeta = Path(carpeta)
        archivo_zip = carpeta / "ntsb.zip"

        print("Descargando archivo NTSB...", flush=True)

        urllib.request.urlretrieve(
            URL_NTSB,
            archivo_zip
        )

        if not zipfile.is_zipfile(archivo_zip):
            raise ValueError("Descarga ZIP no válida")

        with zipfile.ZipFile(archivo_zip) as z:
            archivos = z.namelist()
            print("Contenido ZIP:", archivos)

            bases = [
                nombre for nombre in archivos
                if nombre.lower().endswith(".mdb")
            ]

            if not bases:
                raise ValueError("No se encontró MDB")

            z.extract(bases[0], carpeta)

        base = carpeta / bases[0]

        print("Leyendo tablas...", flush=True)

        tablas = ejecutar([
            "mdb-tables", "-1", str(base)
        ])

        print("TABLAS ENCONTRADAS:")
        print(tablas)

        for tabla in tablas.splitlines()[:5]:
            print("\nTABLA:", tabla)
            print(
                ejecutar([
                    "mdb-schema",
                    str(base),
                    "postgres"
                ])[:1500]
            )
            break

        print("\nPrueba completada")
        print("No se modificaron archivos")

if __name__ == "__main__":
    main()
