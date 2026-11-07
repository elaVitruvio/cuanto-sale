# ¿Cuánto sale?

Juego diario: adiviná el precio de un producto del súper argentino en 6 intentos.

- `index.html` — el juego (una sola página).
- `scripts/actualizar_precios.py` — baja la base SEPA del día y genera `data/productos.json`.
- `.github/workflows/precios.yml` — corre el script todos los días a las 03:05 (hora argentina) y deja agendado el producto de hoy y de los 7 días siguientes.

Fuente de precios: Precios Claros – Base SEPA, Secretaría de Comercio (datos.produccion.gob.ar), licencia CC BY 4.0.
Se usa la mediana del precio de lista en las sucursales de CABA y provincia de Buenos Aires.
