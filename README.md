# Sistema de stock unificado para Gesio

Junta el stock de **Bimbidreams** y **Cambrass** en un solo CSV y lo publica en
una URL fija para que **Gesio** lo lea cada día y actualice PrestaShop.

Todo el trabajo pesado lo hace este sistema gratis en GitHub. Gesio solo tiene
que leer una URL.

---

## Qué hace, en cristiano

1. Cada día (automático) descarga los dos feeds de los proveedores.
2. Si un producto **desaparece** del feed de un proveedor, le pone **stock 0**
   (esto arregla el "stock fantasma" de Bimbidreams, que borra la fila sin avisar).
3. Junta todo en **un solo fichero** `docs/stock.csv` con el formato que pide Gesio:
   - separador `;`
   - textos entre comillas `"`
   - columnas `SKU` y `stock` (el SKU es el EAN)
4. Publica ese fichero en una URL fija. Gesio la lee y actualiza el stock.

**Freno de seguridad:** si un feed llega roto o a medias, el sistema NO publica
nada y avisa por email. Así Gesio sigue usando el fichero bueno del día anterior
en vez de poner el catálogo entero a 0.

---

## Puesta en marcha (una sola vez)

1. **Crear una cuenta de GitHub** (gratis) si no la tienes. Activa el **2FA**
   (verificación en dos pasos) en Settings → Password and authentication.
2. **Crear un repositorio nuevo PÚBLICO** y subir esta carpeta entera
   (`stock_unificado.py`, la carpeta `.github`, este README y las carpetas
   `masters` y `docs`).
3. **Dar de alta las URLs de los proveedores como Secrets** (van cifradas, no se
   ven en el repo): **Settings → Secrets and variables → Actions → New repository
   secret**. Crear dos:
   - Nombre `BIMBI_FEED_URL` → valor: la URL del feed de Bimbidreams.
   - Nombre `CAMBRASS_FEED_URL` → valor: la URL del feed de Cambrass.

   (Para pruebas en local, esas mismas URLs van en el fichero `secrets.local.env`,
   que NO se sube nunca.)
4. En el repo: **Settings → Pages → Source: "Deploy from a branch" → rama `main`,
   carpeta `/docs` → Save.** En un par de minutos tendrás la URL pública:

   ```
   https://TU-USUARIO.github.io/TU-REPO/stock.csv
   ```

   > Alternativa sin tocar Pages (funciona al instante): la URL "raw"
   > `https://raw.githubusercontent.com/TU-USUARIO/TU-REPO/main/docs/stock.csv`

4. **Dar esa URL a Gesio** como origen de la importación programada.
5. En el repo: pestaña **Actions → "Stock unificado Gesio" → Run workflow** para
   lanzarlo a mano la primera vez y comprobar que genera el fichero.

A partir de ahí corre solo todos los días.

---

## Ajustes que quedan por confirmar

- **Hora del cron** (`.github/workflows/stock.yml`): ponerla ~1 h ANTES de la hora
  a la que Gesio lee el fichero. La hora del fichero está en **UTC**
  (05:00 UTC ≈ 07:00 en España en verano).
- **¿Cabecera sí o no?** El fichero sale hoy con una primera fila `"SKU";"stock"`.
  Si el importador de Gesio NO salta la primera fila, esa cabecera se colaría como
  un producto falso. Si Gesio lo pide sin cabecera, cambiar en `stock_unificado.py`:
  `OUT_HEADER = None`.

---

## Tocar la configuración

Todo lo ajustable está arriba del todo de `stock_unificado.py` (sección `CONFIG`):
formato de salida, días que se sigue mandando el 0 a un desaparecido
(`RETENCION_DIAS`), umbrales del freno de seguridad (`MIN_FILAS`), y qué hacer si
un EAN está en los dos proveedores (`COMBINAR`, por defecto el mayor de los dos).

## Probar en local

```
python stock_unificado.py
```

Genera/actualiza `docs/stock.csv` y los maestros de `masters/`.

> **Nunca abrir los CSV con Excel:** convierte los EAN a notación científica y los
> destruye. Mirarlos con el Bloc de notas o dejarlos en paz.
