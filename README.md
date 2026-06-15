# NECROSIS — Python + Multiplayer exact MP

Versión basada en el HTML original de NECROSIS, servida desde Python con WebSocket para sincronización multiplayer visual.

## Ejecutar

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Abrir:

```text
http://127.0.0.1:8000
```

## Cambios de esta versión

- Se eliminó la tienda de armaduras de la pantalla Personaje.
- Se agregó una sección única **Armaduras — equipar** dentro de Personaje.
- La armadura inicial aparece siempre como opción equipable.
- Las armaduras obtenidas como loot pasan automáticamente al inventario de armaduras al volver al menú.
- Equipar armadura recalcula HP, MP y bolsillos.
- Si la nueva armadura tiene menos bolsillos, los objetos sobrantes van al stash.
- Se mantiene el acceso a mejoras de armadura desde la sección de equipamiento.



## Cambios de balance

- Gólem nerfeado: daño dividido por 2, velocidad de ataque dividida por 2 y velocidad de movimiento igual a la del Caminante.
