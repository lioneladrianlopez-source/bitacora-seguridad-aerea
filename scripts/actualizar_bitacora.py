
import subprocess
import tempfile
import zipfile
from pathlib import Path

ZIP = Path("scripts/up15NOV.zip")

def ejecutar(comando):
    resultado = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        check=True,
        timeout=60
    )
    return resultado.stdout.strip()

def main():
    print("BITÁCORA DE SEGURIDAD AÉREA", flush=True)
    print("Prueba local NTSB", flush=True)

    if not ZIP.is_file():
        raise FileNotFoundError(
            f"No se encontró {ZIP}"
        )

    with tempfile.TemporaryDirectory() as temporal:
        carpeta = Path(temporal)

        with zipfile.ZipFile(ZIP) as archivo:
            bases = [
                nombre for nombre in archivo.namelist()
                if nombre.lower().endswith(".mdb")
            ]

            if len(bases) != 1:
                raise ValueError(
                    "Se esperaba una sola base MDB"
                )

            with archivo.open(bases[0]) as origen:
                base = carpeta / "datos.mdb"
                with base.open("wb") as destino:
                    import shutil
                    shutil.copyfileobj(origen, destino)

        tablas = ejecutar([
            "mdb-tables", "-1", str(base)
        ]).splitlines()

        print("TABLAS ENCONTRADAS:", flush=True)
        for tabla in tablas:
            print(tabla, flush=True)

        if not tablas:
            raise ValueError("No se encontraron tablas")

        print("\nCOLUMNAS DE LA PRIMERA TABLA:")
        print(
            ejecutar([
                "mdb-schema", str(base), "postgres"
            ])[:5000]
        )

        print("\nPrueba completada correctamente")
        print("No se modificaron archivos")

if __name__ == "__main__":
    main()
