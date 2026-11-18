--- a/scripts/actualizar_bitacora.py
+++ b/scripts/actualizar_bitacora.py
@@ -1,246 +1,211 @@
-
+import argparse
+import csv
+import io
+import json
+import re
+import shutil
 import subprocess
 import tempfile
 import zipfile
+from datetime import datetime
 from pathlib import Path
-
-ZIP = Path("scripts/up15NOV.zip")
-
-def ejecutar(comando):
+from urllib.parse import urljoin, urlsplit
+from urllib.request import Request, urlopen
+from zoneinfo import ZoneInfo
+
+
+NTSB_DIRECTORIO = "https://data.ntsb.gov/avdata/filedirectory"
+JST_CATALOGO = "https://datos.jst.gob.ar/api/3/action/package_show?id=sucesos-aeronauticos"
+MIN_PALABRAS = 40
+ARCHIVOS_NTSB = re.compile(
+    r'<td id="fileName">up\d{2}[A-Z]{3}\.zip</td>\s*'
+    r'<td id="fileDate">\s*(?P<fecha>[^<]+?)\s*</td>\s*'
+    r'<td[^>]*>[^<]*</td>\s*<td>\s*'
+    r'<a href="(?P<enlace>/avdata/filedirectory/DownloadFile\?fileID=up\d{2}[A-Z]{3}\.zip)"',
+    re.IGNORECASE,
+)
+
+
+def descargar(url):
+    solicitud = Request(url, headers={"User-Agent": "BitacoraSeguridadAerea/1.0"})
+    with urlopen(solicitud, timeout=45) as respuesta:
+        return respuesta.read()
+
+
+def url_ntsb_mas_reciente(pagina, hoy):
+    archivos = []
+    for coincidencia in ARCHIVOS_NTSB.finditer(pagina):
+        fecha = datetime.strptime(coincidencia["fecha"], "%m/%d/%Y %I:%M:%S %p")
+        if fecha.date() <= hoy:
+            archivos.append((fecha, coincidencia["enlace"]))
+    if not archivos:
+        raise ValueError("El directorio NTSB no contiene actualizaciones semanales válidas")
+    return urljoin(NTSB_DIRECTORIO, max(archivos)[1])
+
+
+def leer_tabla(base, tabla, columnas):
     resultado = subprocess.run(
-        comando,
+        ["mdb-export", "-D", "%Y-%m-%d", "-T", "%Y-%m-%d %H:%M:%S", str(base), tabla],
         capture_output=True,
-        text=True,
+        encoding="utf-8",
         check=True,
-        timeout=60
+        timeout=60,
     )
-    return resultado.stdout.strip()
+    lector = csv.DictReader(io.StringIO(resultado.stdout))
+    faltantes = set(columnas) - set(lector.fieldnames or ())
+    if faltantes:
+        raise ValueError(f"Faltan columnas en {tabla}: {', '.join(sorted(faltantes))}")
+    return list(lector)
+
+
+def leer_ntsb(datos_zip):
+    with tempfile.TemporaryDirectory() as temporal:
+        base = Path(temporal) / "datos.mdb"
+        with zipfile.ZipFile(io.BytesIO(datos_zip)) as archivo:
+            bases = [
+                miembro for miembro in archivo.infolist()
+                if not miembro.is_dir() and miembro.filename.lower().endswith(".mdb")
+            ]
+            if len(bases) != 1:
+                raise ValueError("Se esperaba una sola base MDB en el ZIP de NTSB")
+            with archivo.open(bases[0]) as origen, base.open("wb") as destino:
+                shutil.copyfileobj(origen, destino)
+        eventos = leer_tabla(base, "events", (
+            "ev_id", "ntsb_no", "ev_type", "ev_date", "ev_city", "ev_country",
+        ))
+        aeronaves = leer_tabla(base, "aircraft", (
+            "ev_id", "Aircraft_Key", "acft_category", "acft_make", "acft_model",
+        ))
+        narrativas = leer_tabla(base, "narratives", (
+            "ev_id", "Aircraft_Key", "narr_accp", "narr_accf",
+        ))
+    return eventos, aeronaves, narrativas
+
+
+def texto(fila, campo):
+    return (fila.get(campo) or "").strip()
+
+
+def fecha_valida(valor, hoy):
+    try:
+        fecha = datetime.fromisoformat(valor).date()
+    except ValueError:
+        return None
+    return fecha if fecha <= hoy else None
+
+
+def candidatos_ntsb(eventos, aeronaves, narrativas, hoy):
+    aviones = {}
+    for aeronave in aeronaves:
+        if texto(aeronave, "acft_category").upper() != "AIR":
+            continue
+        if texto(aeronave, "acft_make") and texto(aeronave, "acft_model"):
+            clave = (texto(aeronave, "ev_id"), texto(aeronave, "Aircraft_Key"))
+            if all(clave):
+                aviones[clave] = aeronave
+
+    informes = {}
+    for narrativa in narrativas:
+        identificador = texto(narrativa, "ev_id")
+        clave = (identificador, texto(narrativa, "Aircraft_Key"))
+        if clave not in aviones:
+            continue
+        for prioridad, campo, estado in (
+            (2, "narr_accf", "Narrativa final NTSB"),
+            (1, "narr_accp", "Narrativa preliminar NTSB"),
+        ):
+            palabras = len(re.findall(r"\b[^\W\d_]+\b", texto(narrativa, campo)))
+            if palabras >= MIN_PALABRAS:
+                propuesta = (prioridad, palabras, estado)
+                informes[identificador] = max(informes.get(identificador, propuesta), propuesta)
+
+    candidatos = []
+    for evento in eventos:
+        # La exportación identifica incidentes, pero no confirma su gravedad.
+        if texto(evento, "ev_type") != "ACC":
+            continue
+        informe = informes.get(texto(evento, "ev_id"))
+        expediente = texto(evento, "ntsb_no")
+        fecha = fecha_valida(texto(evento, "ev_date"), hoy)
+        lugar = texto(evento, "ev_city") and texto(evento, "ev_country")
+        if informe and expediente and fecha and lugar:
+            _, palabras, estado = informe
+            candidatos.append((fecha, expediente, estado, palabras))
+    return sorted(candidatos, reverse=True)
+
+
+def leer_jst():
+    catalogo = json.loads(descargar(JST_CATALOGO))
+    if not isinstance(catalogo, dict) or not catalogo.get("success"):
+        raise ValueError("El catálogo de JST no devolvió un resultado válido")
+    recurso = next(
+        (r for r in catalogo["result"]["resources"] if r["format"].upper() == "JSON"),
+        None,
+    )
+    if recurso is None:
+        raise ValueError("El catálogo de JST no ofrece el recurso JSON")
+    url = urlsplit(recurso["url"])
+    if url.scheme != "https" or url.netloc not in {"datos.jst.gob.ar", "datos2.jst.gob.ar"}:
+        raise ValueError("URL del recurso JST fuera del portal oficial")
+    # El recurso también se sirve en el dominio del catálogo.
+    try:
+        contenido = descargar(recurso["url"])
+    except OSError:
+        if url.netloc != "datos2.jst.gob.ar":
+            raise
+        contenido = descargar(url._replace(netloc="datos.jst.gob.ar").geturl())
+    registros = json.loads(contenido)
+    if not isinstance(registros, list) or any(
+        not isinstance(registro, dict)
+        or not {"fecha_suceso", "clase_suceso", "estado_investigacion"} <= registro.keys()
+        for registro in registros
+    ):
+        raise ValueError("El recurso de JST cambió de estructura")
+    return registros
+
+
+def revisar_ntsb(hoy, zip_local):
+    if zip_local:
+        datos_zip = zip_local.read_bytes()
+        origen = str(zip_local)
+    else:
+        pagina = descargar(NTSB_DIRECTORIO).decode("utf-8-sig")
+        origen = url_ntsb_mas_reciente(pagina, hoy)
+        datos_zip = descargar(origen)
+    eventos, aeronaves, narrativas = leer_ntsb(datos_zip)
+    candidatos = candidatos_ntsb(eventos, aeronaves, narrativas, hoy)
+    print(f"NTSB: {len(eventos)} sucesos, {len(candidatos)} candidatos. Fuente: {origen}")
+    for fecha, expediente, estado, palabras in candidatos[:10]:
+        print(f"  {fecha.isoformat()} | {expediente} | {estado} | {palabras} palabras")
+
+
+def revisar_jst(hoy):
+    registros = leer_jst()
+    fichas = sum(
+        bool(fecha_valida(texto(registro, "fecha_suceso"), hoy))
+        and texto(registro, "clase_suceso").casefold() in {"accidente", "incidente grave"}
+        for registro in registros
+    )
+    print(f"JST: {fichas} fichas fechadas de accidentes o incidentes graves.")
+    print("Las fichas quedan pendientes de vinculación con el informe o relato oficial.")
+
 
 def main():
-    print("BITÁCORA DE SEGURIDAD AÉREA", flush=True)
-    print("Prueba local NTSB", flush=True)
-
-    if not ZIP.is_file():
-        raise FileNotFoundError(
-            f"No se encontró {ZIP}"
-        )
-
-    with tempfile.TemporaryDirectory() as temporal:
-        carpeta = Path(temporal)
-
-        with zipfile.ZipFile(ZIP) as archivo:
-            bases = [
-                nombre for nombre in archivo.namelist()
-                if nombre.lower().endswith(".mdb")
-            ]
-
-            if len(bases) != 1:
-                raise ValueError(
-                    "Se esperaba una sola base MDB"
-                )
-
-            with archivo.open(bases[0]) as origen:
-                base = carpeta / "datos.mdb"
-                with base.open("wb") as destino:
-                    import shutil
-                    shutil.copyfileobj(origen, destino)
-
-        tablas = ejecutar([
-            "mdb-tables", "-1", str(base)
-        ]).splitlines()
-
-        print("TABLAS ENCONTRADAS:", flush=True)
-        for tabla in tablas:
-            print(tabla, flush=True)
-
-        if not tablas:
-            raise ValueError("No se encontraron tablas")
-
-        print("\nCOLUMNAS DE LA PRIMERA TABLA:")
-        print(
-            ejecutar([
-    "mdb-export",
-    str(base),
-    "events"
-])[:3000]
-        )
-
-        print("\nPrueba completada correctamente")
-        print("No se modificaron archivos")
-        
-        import csv
-        import io
-        from datetime import datetime
-
-        def leer_tabla(nombre):
-            contenido = ejecutar([
-                "mdb-export", str(base), nombre
-            ])
-            return list(csv.DictReader(io.StringIO(contenido)))
-
-        print("\nBUSCANDO SUCESOS DE AVIONES...")
-
-        eventos = leer_tabla("events")
-        aeronaves = leer_tabla("aircraft")
-        
-        from collections import Counter
-
-        print("\nDIAGNÓSTICO DE AERONAVES")
-        print("Total de registros:", len(aeronaves))
-
-        if aeronaves:
-            print("Columnas disponibles:")
-            print(list(aeronaves[0].keys()))
-
-            print("\nCategorías encontradas:")
-            categorias = Counter(
-                str(a.get("acft_category", "")).strip()
-                for a in aeronaves
-            )
-            for categoria, cantidad in categorias.most_common(20):
-                print(repr(categoria), cantidad)
-
-            print("\nPrimer registro de aeronave:")
-            print(aeronaves[0])
-
-
-        # Identificar aeronaves de ala fija.
-        ids_aviones = set()
-
-        
-        for avion in aeronaves:
-            tipo = avion.get("acft_category", "").strip().lower()
-
-            if tipo == "air":
-                ids_aviones.add(avion.get("ev_id", ""))
-
-        resultados = []
-
-
-        for evento in eventos:
-            if evento.get("ev_id", "") not in ids_aviones:
-                continue
-
-            fecha = evento.get("ev_date", "").strip()
-
-           
-            fecha_orden = None
-
-            for formato in (
-                "%m/%d/%y %H:%M:%S",
-                "%m/%d/%Y %H:%M:%S",
-                "%m/%d/%y",
-                "%m/%d/%Y",
-                "%Y-%m-%d %H:%M:%S",
-                "%Y-%m-%d"
-            ):
-                try:
-                    fecha_orden = datetime.strptime(
-                        fecha, formato
-                    )
-                    break
-                except ValueError:
-                    continue
-
-            if fecha_orden is None:
-                print("Fecha no reconocida:", repr(fecha))
-                continue
-
-
-            resultados.append((fecha_orden, evento))
-
-        resultados.sort(key=lambda x: x[0], reverse=True)
-
-        print("\n10 SUCESOS MÁS RECIENTES:")
-
-        for fecha, evento in resultados[:10]:
-            print("------------------------------")
-            print("Fecha:", fecha.strftime("%d/%m/%Y"))
-            print("Expediente:", evento.get("ntsb_no", ""))
-            print("Tipo:", evento.get("ev_type", ""))
-            print("Ciudad:", evento.get("ev_city", ""))
-            print("Estado:", evento.get("ev_state", ""))
-            print("País:", evento.get("ev_country", ""))
-
-        
-        print("\nDATOS DE LAS AERONAVES:")
-
-        for fecha, evento in resultados[:10]:
-            expediente = evento.get("ntsb_no", "")
-            identificador = evento.get("ev_id", "")
-
-            print("\n--------------------------")
-            print("Fecha:", fecha.strftime("%d/%m/%Y"))
-            print("Expediente:", expediente)
-
-            relacionadas = [
-                a for a in aeronaves
-                if a.get("ev_id", "") == identificador
-                and a.get("acft_category", "").strip().upper() == "AIR"
-            ]
-
-            for avion in relacionadas:
-                print("Matrícula:", avion.get("regis_no", ""))
-                print("Fabricante:", avion.get("acft_make", ""))
-                print("Modelo:", avion.get("acft_model", ""))
-                print("Categoría:", avion.get("acft_category", ""))
-
-        
-        print("\nDIAGNÓSTICO DE NARRATIVAS")
-
-        narrativas = leer_tabla("narratives")
-
-        print("Total de narrativas:", len(narrativas))
-
-        if narrativas:
-            print("Columnas disponibles:")
-            print(list(narrativas[0].keys()))
-
-            print("\nPrimeros identificadores:")
-            for narrativa in narrativas[:3]:
-                print({
-                    clave: valor
-                    for clave, valor in narrativa.items()
-                    if clave.lower() in (
-                        "ev_id", "ntsb_no",
-                        "aircraft_key", "narr_type"
-                    )
-                })
-
-        
-        print("\nNARRATIVAS DE LOS 10 SUCESOS")
-
-        for fecha, evento in resultados[:10]:
-            identificador = evento.get("ev_id", "")
-            expediente = evento.get("ntsb_no", "")
-
-            relacionadas = [
-                n for n in narrativas
-                if n.get("ev_id", "") == identificador
-            ]
-
-            print("\n--------------------------")
-            print("Expediente:", expediente)
-            print("Fecha:", fecha.strftime("%d/%m/%Y"))
-            print("Narrativas encontradas:", len(relacionadas))
-
-            for narrativa in relacionadas:
-                for campo in (
-                    "narr_accp",
-                    "narr_accf",
-                    "narr_cause",
-                    "narr_inc"
-                ):
-                    contenido = narrativa.get(campo, "").strip()
-
-                    if contenido:
-                        print(
-                            campo + ":",
-                            contenido[:500].replace("\n", " ")
-                        )
-
-        print("\nTotal de sucesos de aviones:", len(resultados))
-        print("Prueba terminada. Sin publicaciones.")
+    parser = argparse.ArgumentParser(description="Revisar candidatos para la Bitácora.")
+    parser.add_argument("--ntsb-zip", type=Path, help="Usar un ZIP local de NTSB para pruebas.")
+    args = parser.parse_args()
+    hoy = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
+    fuentes = (("NTSB", revisar_ntsb, (hoy, args.ntsb_zip)), ("JST", revisar_jst, (hoy,)))
+    errores = []
+    for nombre, revisar, parametros in fuentes:
+        try:
+            revisar(*parametros)
+        except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
+            detalle = getattr(error, "stderr", None) or str(error)
+            errores.append(f"{nombre}: {detalle.strip()}")
+    print("Revisión de candidatos terminada. No se generaron publicaciones.")
+    if errores:
+        parser.exit(1, "\n".join(errores) + "\n")
 
 
 if __name__ == "__main__":
--- /dev/null
+++ b/scripts/test_actualizar_bitacora.py
@@ -0,0 +1,56 @@
+import importlib.util
+import unittest
+from datetime import date
+from pathlib import Path
+
+spec = importlib.util.spec_from_file_location("actualizar", Path(__file__).with_name("actualizar_bitacora.py"))
+m = importlib.util.module_from_spec(spec)
+spec.loader.exec_module(m)
+
+class Filtro(unittest.TestCase):
+    def setUp(self):
+        self.hoy = date(2026, 11, 17)
+        self.evento = dict(ev_id="1", ntsb_no="TEST001", ev_type="ACC", ev_date="2026-11-16", ev_city="Ciudad", ev_country="USA")
+        self.avion = dict(ev_id="1", Aircraft_Key="1", acft_category="AIR", acft_make="CESSNA", acft_model="172")
+        self.relato = dict(ev_id="1", Aircraft_Key="1", narr_accp=" ".join(["vuelo"] * 40), narr_accf="")
+    def candidatos(self):
+        return m.candidatos_ntsb([self.evento], [self.avion], [self.relato], self.hoy)
+    def test_relato_suficiente(self):
+        self.assertEqual(self.candidatos()[0][1], "TEST001")
+    def test_relato_corto(self):
+        self.relato["narr_accp"] = " ".join(["vuelo"] * 39)
+        self.assertEqual(self.candidatos(), [])
+    def test_sin_relato(self):
+        self.relato["narr_accp"] = ""
+        self.assertEqual(self.candidatos(), [])
+    def test_otra_aeronave(self):
+        self.relato["Aircraft_Key"] = "2"
+        self.assertEqual(self.candidatos(), [])
+    def test_helicoptero(self):
+        self.avion["acft_category"] = "HELI"
+        self.assertEqual(self.candidatos(), [])
+    def test_fecha_futura(self):
+        self.evento["ev_date"] = "2026-11-18"
+        self.assertEqual(self.candidatos(), [])
+    def test_fecha_invalida(self):
+        self.evento["ev_date"] = "sin fecha"
+        self.assertEqual(self.candidatos(), [])
+    def test_incidente_sin_gravedad_confirmada(self):
+        self.evento["ev_type"] = "INC"
+        self.assertEqual(self.candidatos(), [])
+    def test_sin_lugar(self):
+        self.evento["ev_city"] = ""
+        self.assertEqual(self.candidatos(), [])
+    def test_prioriza_final(self):
+        self.relato["narr_accf"] = self.relato["narr_accp"]
+        self.assertEqual(self.candidatos()[0][2], "Narrativa final NTSB")
+    def test_sin_modelo(self):
+        self.avion["acft_model"] = ""
+        self.assertEqual(self.candidatos(), [])
+    def test_orden_descendente(self):
+        anterior = dict(self.evento, ev_date="2026-11-15", ntsb_no="TEST000")
+        resultado = m.candidatos_ntsb([anterior, self.evento], [self.avion], [self.relato], self.hoy)
+        self.assertEqual([r[1] for r in resultado], ["TEST001", "TEST000"])
+
+if __name__ == "__main__":
+    unittest.main()
--- a/.github/workflows/actualizacion-bitacora.yml
+++ b/.github/workflows/actualizacion-bitacora.yml
@@ -1,33 +1,39 @@
-
-name: Actualización de Bitácora
+name: Revisión de fuentes de la Bitácora
 
 on:
   schedule:
     - cron: '0 23 * * *'
   workflow_dispatch:
+  push:
+    paths:
+      - 'scripts/**'
+      - '.github/workflows/actualizacion-bitacora.yml'
+  pull_request:
+    paths:
+      - 'scripts/**'
+      - '.github/workflows/actualizacion-bitacora.yml'
 
 permissions:
-  contents: write
+  contents: read
 
 jobs:
-  actualizar:
+  revisar:
     runs-on: ubuntu-latest
-
-    
+    timeout-minutes: 10
     steps:
       - name: Descargar repositorio
         uses: actions/checkout@v4
-
       - name: Preparar Python
         uses: actions/setup-python@v5
         with:
           python-version: '3.12'
-
+      - name: Comprobar el filtro editorial
+        run: python -m unittest discover -s scripts -p 'test_*.py' -v
       - name: Instalar lector de bases Access
+        if: github.event_name != 'pull_request'
         run: |
           sudo apt-get update
           sudo apt-get install -y mdbtools
-
-      - name: Ejecutar actualización de Bitácora
+      - name: Revisar fuentes oficiales y candidatos
+        if: github.event_name != 'pull_request'
         run: python scripts/actualizar_bitacora.py
-
