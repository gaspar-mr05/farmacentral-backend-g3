# Entrega 3: validación, preparación y operación (pasos 24–26)

Estado al 13/11/2026: herramientas implementadas; validación completa en dev,
arranque remoto, activación de producción y observación real pendientes.
Las pruebas locales no acreditan los recorridos oficiales ni las 60 horas.

Los comandos se ejecutan desde `/home/integracion/farmacentral-backend`.
Reemplazar los marcadores en mayúsculas por datos reales. Conservar credenciales,
respaldos y evidencia financiera fuera del repositorio, con permisos privados.

## 24. Validar recorridos reales en desarrollo

Se verificaron readiness HTTP 200, lectura SOAP y una llamada autorizada al
gateway: `gemini-flash-lite`, 465 tokens (368 entrada, 97 salida). No se emitieron
ni pagaron facturas. Evidencia pública: [readiness](evidence/2026-11-13/public-probes.json)
y [gateway](evidence/2026-11-13/gateway-probe.json). La DB local y SSH no estuvieron
accesibles; no hubo reinicios. Nuevas pruebas del gateway deben respetar el
presupuesto compartido informado por el grupo: USD 1, minimizando consumo.

El observador exige endpoints de **dev** y DB en sólo lectura; no ejecuta acciones
de negocio ni llama al LLM. Primero descubrir períodos e IDs y luego elegirlos:

```bash
.venv/bin/python -m scripts.validate_e3_flow \
  --environment dev --list-ids --output /tmp/e3-periods.json
.venv/bin/python -m scripts.validate_e3_flow \
  --environment dev --period-id UUID_PERIODO --list-ids \
  --output /tmp/e3-targets.json
.venv/bin/python -m scripts.validate_e3_flow \
  --environment dev --period-id UUID_PERIODO \
  --order-id OC_OFICIAL --trade-id UUID_TRADE_LOCAL \
  --message-id ID_MENSAJE --recall-id ID_RECALL \
  --output /tmp/e3-observations.json
```

Repetir las opciones para cubrir sFTP y Pub/Sub; compra y venta de mercado;
consultas explícitas y naturales; estados parciales/rechazados/vencidos; recall
de lote comprado y ajeno. Conservar OC, unidades/linaje, despachos en plazo,
facturas/pagos oficiales, confirmaciones de consulta y texto antes de 30 minutos,
recepción visual en chat y verdad/diff del portal. Una factura de venta pendiente
no prueba cobro. El chat de prueba no acredita una OC real en producción.

Coordinar un reinicio y registrar IDs/fases/timestamps antes y después para
comprobar recuperación sin duplicaciones. Ante un resultado incierto, conciliar
antes de repetir un pago, emisión, aceptación o envío.
Los archivos del observador son privados (0600), no se sobrescriben: salida 1
indica fallo y 2, validación pendiente. `step24_status` sigue pendiente hasta
completar la revisión manual, aunque los chequeos seleccionados pasen.

## 25. Preparar producción y una época nueva

Desde el **16 de noviembre**, cotejar con el portal URLs y secretos, WSDL/dirección
SOAP, casilla sFTP/known_hosts, Pub/Sub y permisos, OIDC, DB y grupo. La evaluación
va del **18 a las 10:00 al 20 a las 22:00**, hora de Chile.

Systemd y el deploy fijan `APP_ENV_FILE` para leer sólo `.env`, sin `.env.local`.
Las variables ya exportadas tienen prioridad. No usar `source .env`. Configurar
`APP_ENV=prod` (o `dev` para ensayos), presupuesto explícito
`REPLENISHMENT_SPENDING_LIMIT`, mínimos/objetivos/vida útil y habilitación
intencional de `BILLING_EMIT_SALES_ENABLED` y `BILLING_PAY_INVOICES_ENABLED`.
Revisar los valores por defecto de facturación: 60 s de revisión, 600 s de
sincronización completa y 45 requests/min compartidas.

```bash
export APP_ENV_FILE=/home/integracion/farmacentral-backend/.env
.venv/bin/python -m scripts.check_e3_readiness \
  --environment prod --expected-api-host HOST_OFICIAL \
  --wsdl-file /ruta/privada/billing.wsdl
```

Este chequeo es local: no acredita autenticación, permisos remotos, saldo,
heartbeats ni reset. Si se publican hosts distintos por servicio, revisar el
comprobador antes de aprobar la configuración.

**Inventario vacío no confirma un reset.** Conservar el aviso oficial. Para el
cambio, pausar despliegues automáticos y detener API, workers, inventario y
monitor, incluidos procesos manuales. Registrar SHA, configuración, período y
versión Alembic; respaldar la DB anterior y ensayar su restauración en otra DB:

```bash
sudo systemctl stop farmacentral-backend.service farmacentral-workers.target
sudo systemctl stop farmacentral-inventory-sync.timer farmacentral-inventory-sync.service
sudo systemctl stop farmacentral-monitor.timer farmacentral-monitor.service
umask 077
pg_dump --format=custom --dbname=DB_ANTERIOR --file=/ruta/privada/antes-e3.dump
createdb DB_ENSAYO_RESTAURACION
pg_restore --exit-on-error --no-owner --dbname=DB_ENSAYO_RESTAURACION /ruta/privada/antes-e3.dump
```

Usar parámetros libpq y `.pgpass` privado, sin contraseñas en comandos. Verificar
Alembic e IDs conocidos en la copia; conservar original y respaldo. Crear con el
administrador una **DB nueva** con permisos de aplicación y seleccionarla en
`DATABASE_URL`. No copiar tareas/eventos/reservas anteriores: cambiar sólo el
período no los aísla, pues algunas tablas no tienen separación por época.

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.prepare_e3_epoch \
  --environment prod --code e3-evaluation \
  --starts-at 2026-11-18T10:00:00-03:00 --ends-at 2026-11-20T22:00:00-03:00
```

El comando sólo verifica DB migrada vacía. Tras confirmar el reset, repetir con
`--create --reset-reference 'REFERENCIA_REAL_DEL_AVISO'`, guardar el resultado y
copiar `period_id` a `BILLING_PERIOD_ID`. No repetir la creación ni iniciar
procesos antes de terminar la configuración. Para dev usar otra DB/código/fechas.

```bash
bash deploy/install-systemd-units.sh
sudo systemctl start farmacentral-backend.service farmacentral-workers.target
sudo systemctl enable --now farmacentral-inventory-sync.timer
curl --fail http://127.0.0.1:8000/api/health/ready
```

Exigir ciclos completos y heartbeats recientes de facturación/mercado en
`/api/operations/workers?period_id=UUID_NUEVO`, usando la clave de operaciones
privadamente. Revisar logs de los demás workers; `active` no prueba progreso.
El deploy requiere las reglas sudoers documentadas para reiniciar sin contraseña.

Ensayar en dev un reset coordinado del sandbox: compras directas sólo permitidas,
ofertas buy, cuatro insumos restringidos, producción, primeras compras/ventas y
pedidos cumplidos. Revisar excedentes con `scripts.sell_surplus --once` antes de
usar `--execute`; este vendedor no se activa con el target. Ajustar lotes, buffers
y margen sin vender stock comprometido. Verificar `/`, `/trazabilidad`, recalls
y chat; conservar la evidencia del paso 24 y publicar GitHub antes del inicio.

Ensayar también el SHA anterior en la copia restaurada. Para revertir aplicación,
detener procesos y usar un checkout limpio compatible con el esquema. No hacer
downgrade ni restaurar encima de la DB activa. Después del reset remoto, conservar
la época nueva: **no reactivar la DB/período anterior**. En versiones sin
`APP_ENV_FILE`, retirar `.env.local` del directorio activo y guardarlo privadamente.

## 26. Monitorear, registrar incidentes y cerrar

Configurar en `.env` las URLs públicas canónicas (pueden tener hosts distintos),
con HTTPS y sin credenciales/query; luego activar el timer:

```dotenv
E3_CUSTOMER_SITE_URL=https://HOST_PUBLICO/
E3_TRACEABILITY_SITE_URL=https://HOST_PUBLICO/trazabilidad
E3_MONITOR_OUTPUT=/var/lib/farmacentral/e3-monitor.jsonl
```

```bash
sudo systemctl enable --now farmacentral-monitor.timer
journalctl -u farmacentral-monitor.service -n 20 --no-pager
.venv/bin/python -m scripts.monitor_e3 --summary \
  --output /var/lib/farmacentral/e3-monitor.jsonl
```

Cada 120 s se guardan dos GET HTTP (sin redirecciones, timeout 10 s), código,
éxito 2xx y duración hasta headers, además de lectura de DB y alertas: pagos
desde cinco horas/seis horas vencidos, tareas financieras estancadas, heartbeats,
facturación faltante, 429 de facturación/mercado, stock/reservas/ofertas, órdenes,
consultas sin texto y recalls pendientes. No ejecuta operaciones de negocio ni
LLM, y no envía notificaciones. Si falla la DB, conserva HTTP y devuelve 1; con
DB observada devuelve 0 incluso con alertas: revisar su detalle en JSONL.

Para una medición manual usar `scripts.monitor_e3 --customer-url URL_CLIENTES
--traceability-url URL_TRAZABILIDAD --output /tmp/e3-monitor.jsonl` mediante
`.venv/bin/python -m`. Systemd exporta las URLs del `.env`; para el CLI pasarlas
explícitamente. El resumen separa evaluación, fuera de evaluación y muestras sin
verificar, por período; muestra disponibilidad observada y huecos, sin inventar
muestras. No prueba JavaScript/checkout ni disponibilidad externa al servidor.
Stock y estados son locales; un 429 persistido no cuenta todos los rechazos.

Acordar turnos y revisar alertas, plazos, saldo, deuda y cuotas de mensajería/LLM.
Mantener conciliación automática y copias privadas del JSONL y evidencia oficial.
Por incidente registrar inicio/fin UTC, período, síntoma, IDs, causa, responsable,
SHA/cambio y prueba de recuperación. Dejar causas no confirmadas pendientes.

Al cierre, preservar estados oficiales sin detener abruptamente los servicios.
Después del fin del período, completar la conciliación y capturar:

```bash
.venv/bin/python -m scripts.reconcile_billing --read-only --full --once
.venv/bin/python -m scripts.capture_business_metrics --closing \
  > /ruta/privada/e3-closing.json
```

`--once` puede requerir más ciclos para completar el escaneo. La captura exige
conciliación reciente y completa e intereses verificables al corte; ante rechazo,
guardar evidencia sin modificar fechas/importes ni forzar resultados. Un pago
posterior al corte sigue pendiente al cierre. El JSONL posterior es observación,
no cierre oficial. Los pasos quedan completos con recorridos y disponibilidad
reales, ingresos/costos/intereses al corte e incidentes con recuperación registrada.
