# Entrega 2 — Paso 21: operación continua y observabilidad

La API y cada worker se ejecutan en procesos separados. FastAPI persiste los
webhooks y consulta el estado local; los intérpretes, pollers y automatismos
continúan en servicios systemd. Ningún worker se inicia desde el lifespan web.

## Salud e aislamiento

| Comprobación | Comportamiento |
| --- | --- |
| `/api/health` | Proceso web vivo; no consulta dependencias. |
| `/api/health/ready` | Conexión PostgreSQL independiente, de solo lectura, `SELECT 1`; 200 o 503. |
| `scripts.check_operation` | API, DB, disco y, opcionalmente, portal, trazabilidad y systemd; no modifica datos ni arranca servicios. |

La conexión del healthcheck tiene `connect_timeout=3` y
`statement_timeout=2000`, usa `NullPool` y no espera conexiones ocupadas de
negocio. La liveness sigue respondiendo cuando falla PostgreSQL. Readiness no
asegura migraciones aplicadas, permisos de escritura ni progreso de workers.

Los clientes ya tienen timeouts: Farma Central, compra, mensajería, sFTP y
Pub/Sub usan diez segundos por defecto; OIDC tres; LLM veinte con un límite
global de llamada. Se mantienen las variables existentes. Un timeout o
espera de producción en un worker no ocupa el proceso de Uvicorn. Las rutas
sincrónicas de trazabilidad y webhooks se ejecutan en el pool de hilos de
FastAPI. La carga de DB y del equipo sigue siendo compartida.

## Supervisión y despliegue

API y workers conservan `Restart=on-failure`, con pausas de cinco y treinta
segundos respectivamente. `StartLimitIntervalSec=0` evita que varias caídas
seguidas deshabiliten permanentemente esos reinicios. Los eventos persistidos
y locks de PostgreSQL del paso 20 permiten recuperar el trabajo interrumpido.
Un servicio activo no demuestra que esté procesando correctamente: revisar
errores, plazos y eventos pendientes.

Se eliminó `PartOf=farmacentral-backend.service` del target de workers y del
timer de inventario. Reiniciar solo FastAPI mantiene esos procesos activos.
Los workers conservan `PartOf=farmacentral-workers.target`, por lo que reiniciar
el target sí los reinicia. `deploy/release.sh` reinicia API y target por separado
y comprueba readiness con curl y tiempo máximo.

Para instalar las unidades actualizadas **en el servidor**:

```bash
sudo cp deploy/farmacentral-backend.service /etc/systemd/system/
sudo cp deploy/farmacentral-worker@.service /etc/systemd/system/
sudo cp deploy/farmacentral-workers.target /etc/systemd/system/
sudo cp deploy/farmacentral-inventory-sync.service /etc/systemd/system/
sudo cp deploy/farmacentral-inventory-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now farmacentral-backend.service
sudo systemctl enable --now farmacentral-workers.target
sudo systemctl enable --now farmacentral-inventory-sync.timer
```

Para despliegues automáticos, autorizar con `visudo` los dos comandos exactos
usados por el script, conservando las demás reglas del servidor:

```sudoers
integracion ALL=(root) NOPASSWD: /usr/bin/systemctl restart farmacentral-backend.service
integracion ALL=(root) NOPASSWD: /usr/bin/systemctl restart farmacentral-workers.target
```

Configurar estas reglas antes de desplegar. `sudo -l` puede indicar que un
comando está autorizado aunque todavía requiera contraseña; no comprueba por
sí solo que `sudo -n` pueda ejecutarlo. Si un reinicio falla, el script identifica
la unidad y termina sin anunciar éxito. La instalación y Alembic pueden haber
terminado antes del fallo, y la API puede haberse reiniciado antes de fallar el
target: revisar ambos estados.

Ante `sudo: a password is required` en GitHub Actions, editar las reglas en una
sesión interactiva del servidor:

```bash
sudo visudo -f /etc/sudoers.d/farmacentral-deploy
```

Agregar las dos reglas exactas anteriores sin borrar las reglas existentes,
guardar y validar con `sudo visudo -c`. Después volver a ejecutar el job de
despliegue. No guardar contraseñas ni habilitar `NOPASSWD: ALL`.

Un pull no instala las unidades ni modifica sudoers. Reiniciar el target incluye
`process_messaging`, cuyo trabajo real puede consumir LLM: no usarlo como test
local. No se necesita ninguna nueva variable de entorno ni migración.

## Revisión de operación, solo lectura

Con el backend local activo:

```bash
.venv/bin/python -m scripts.check_operation
```

En el servidor, indicar un lote real que deba existir:

```bash
read -r -p "Lote para consultar trazabilidad: " LOT_ID
.venv/bin/python -m scripts.check_operation \
  --check-services --include-replenishment \
  --portal-url https://choripan3.ing.uc.cl \
  --traceability-lot "$LOT_ID" \
  --disk-path / --disk-path /var/log --disk-path /var/lib/postgresql
```

El chequeo usa HTTP GET y `systemctl show`, sin sudo. Cada GET tiene un límite
global de diez segundos y systemd cinco. Devuelve JSON y código 1 cuando alguna
comprobación solicitada falla o queda pendiente. Por defecto exige al menos
1 GiB y 10% libre en cada volumen; los umbrales se ajustan con
`--minimum-free-gib` y `--minimum-free-percent`. Si PostgreSQL o sus logs están
en otro volumen, agregar su ruta con `--disk-path`.

La inspección de logs se realiza aparte:

```bash
journalctl --disk-usage
sudo journalctl -u farmacentral-backend.service --since '30 minutes ago' --no-pager
sudo journalctl -u 'farmacentral-worker@*' --since '30 minutes ago' --no-pager
sudo journalctl -u farmacentral-inventory-sync.service -n 100 --no-pager
sudo -u postgres psql -d farmacentral -c 'SELECT now(), pg_database_size(current_database());'
```

Revisar la retención configurada de journald y el volumen real de PostgreSQL.
El script no borra logs, no cambia su retención y no reinicia PostgreSQL. Las
unidades conservan su dependencia de PostgreSQL: si se reinicia o detiene ese
servicio, comprobar y volver a iniciar API, target y timer si systemd los detuvo.

Los logs registran `message_id`, `recall_id` y `order_id` para relacionar recepción,
intentos, decisiones, finalización y fallos. En cumplimiento `order_id` es la
OC externa y `local_order_id` el UUID interno. Trazabilidad registra lote,
duración y tamaño del linaje; emite warning si supera diez segundos. No se
añade texto de mensajes ni credenciales a estos logs.

## Rendimiento y recuperación

La consulta de trazabilidad recorre ancestros y descendientes con CTEs
recursivas en PostgreSQL. Usa `UNION` para terminar ante ciclos y carga las
relaciones por lotes, evitando consultas por cada generación. Conserva el
contrato de respuesta, consumos unitarios y entregas.

```bash
.venv/bin/python -m scripts.test_e2 --step21
.venv/bin/python -m scripts.test_e2
```

Las pruebas usan esquemas PostgreSQL temporales y bloquean sockets externos,
también en los subprocesos de prueba. No llaman al gateway ni a otros servicios
reales. Cubren:

- Readiness con DB disponible/caída, liveness y ausencia de credenciales en errores.
- API HTTP de trazabilidad con 100 generaciones y 2.000 lotes ajenos: máximo
  15 sentencias y menos de diez segundos, incluyendo una entrega; un ciclo
  histórico termina sin repetir el origen.
- Worker simulado que persiste una interpretación y mantiene un lock mientras
  está detenido. Dos procesos reales de Uvicorn arrancan y se detienen, con
  health y trazabilidad disponibles y el evento conservado.
- Terminación abrupta de ese worker y recuperación desde un proceso nuevo:
  libera el lock, completa el evento y reutiliza la interpretación persistida.
- Casos de duplicados y recuperación del paso 20 y benchmark de recalls.

Queda por validar en el servidor: reinicios bajo systemd con eventos reales,
progreso de cada canal, comportamiento visual del portal durante trabajo de
fondo y disponibilidad durante toda la evaluación. El check puntual y las
pruebas locales no certifican esos resultados. No se ejecutaron reinicios ni
llamadas reales al LLM en el servidor para desarrollar este paso.
