
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
    "mdb-export",
    str(base),
    "events"
])[:3000]
        )

        print("\nPrueba completada correctamente")
        print("No se modificaron archivos")
        
        import csv
        import io
        from datetime import datetime

        def leer_tabla(nombre):
            contenido = ejecutar([
                "mdb-export", str(base), nombre
            ])
            return list(csv.DictReader(io.StringIO(contenido)))

        print("\nBUSCANDO SUCESOS DE AVIONES...")

        eventos = leer_tabla("events")
        aeronaves = leer_tabla("aircraft")
        
        from collections import Counter

        print("\nDIAGNÓSTICO DE AERONAVES")
        print("Total de registros:", len(aeronaves))

        if aeronaves:
            print("Columnas disponibles:")
            print(list(aeronaves[0].keys()))

            print("\nCategorías encontradas:")
            categorias = Counter(
                str(a.get("acft_category", "")).strip()
                for a in aeronaves
            )
            for categoria, cantidad in categorias.most_common(20):
                print(repr(categoria), cantidad)

            print("\nPrimer registro de aeronave:")
            print(aeronaves[0])


        # Identificar aeronaves de ala fija.
        ids_aviones = set()

        
        for avion in aeronaves:
            tipo = avion.get("acft_category", "").strip().lower()

            if tipo == "air":
                ids_aviones.add(avion.get("ev_id", ""))

        resultados = []


        for evento in eventos:
            if evento.get("ev_id", "") not in ids_aviones:
                continue

            fecha = evento.get("ev_date", "").strip()

            try:
                fecha_orden = datetime.strptime(
                    fecha[:10], "%m/%d/%y"
                )
            except ValueError:
                try:
                    fecha_orden = datetime.strptime(
                        fecha[:10], "%Y-%m-%d"
                    )
                except ValueError:
                    continue

            resultados.append((fecha_orden, evento))

        resultados.sort(key=lambda x: x[0], reverse=True)

        print("\n10 SUCESOS MÁS RECIENTES:")

        for fecha, evento in resultados[:10]:
            print("------------------------------")
            print("Fecha:", fecha.strftime("%d/%m/%Y"))
            print("Expediente:", evento.get("ntsb_no", ""))
            print("Tipo:", evento.get("ev_type", ""))
            print("Ciudad:", evento.get("ev_city", ""))
            print("Estado:", evento.get("ev_state", ""))
            print("País:", evento.get("ev_country", ""))

        print("\nTotal de sucesos de aviones:", len(resultados))
        print("Prueba terminada. Sin publicaciones.")


if __name__ == "__main__":
    main()
