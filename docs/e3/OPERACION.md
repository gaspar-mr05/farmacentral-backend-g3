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
usar `--execute`; este vendedor no se activa por defecto. Ajustar lotes, buffers
y margen sin vender stock comprometido. Verificar `/`, `/trazabilidad`, recalls
y chat; conservar la evidencia del paso 24 y publicar GitHub antes del inicio.

El abastecimiento puede combinar ofertas de venta pequeñas y comprar lotes
mayores al faltante, respetando el límite de cantidad, desembolso, espacio,
frío y plazo. Ejecuta una adquisición y reevalúa el inventario antes de continuar;
el plan completo no constituye stock confirmado. Para sustituir una oferta buy,
primero concilia y confirma su cancelación o cierre. Las restricciones de pérdida
y presupuesto siguen aplicándose aunque exista una oferta disponible.
El presupuesto de consumo se reparte a través de las recetas según la fracción
del lote intermedio que requiere el pedido; el desembolso conserva el costo del
lote completo. Así un lote grande no se compara prematuramente con el ingreso
de un solo kit. El plan final sigue comprobando margen, desembolso y plazo.
Si el pedido recibido elegido no tiene ruta viable, el reabastecedor evalúa hasta
tres pedidos posteriores del mismo SKU. Guarda el ID de la alternativa viable
para continuar tras reinicios, sin extender el plazo de la OC anterior ni cambiar
su estado. Los aceptados conservan prioridad; no cambia de objetivo mientras hay
operaciones o compras pendientes de resolver. El log de selección es
`Replenishment selected later order`; la ejecución revalida el plan en el siguiente ciclo.
Las alternativas de una misma decisión reutilizan las cotizaciones leídas para
no multiplicar consultas al mercado; esa caché no se comparte con la ejecución.
Una oferta buy necesaria se conserva si el nuevo plan todavía pide comprar ese
SKU mediante otra oferta buy; encontrar una venta de otro insumo no la reemplaza.

Para compras con custodia pendiente, la sincronización informa qué SKU se leyó
completamente. El conciliador puede recuperar recepciones históricas sin vincular
si coinciden inequívocamente con la compra y ese SKU está completo, aunque otro
SKU cambie durante la lectura. Mantiene el requisito de inventario global completo
para ventas y comprobaciones de ausencia de recall. Los logs distinguen
`incomplete_inventory`, `ambiguous_units` y `Market custody recorded`.

### Ventas de excedentes y contraste del conteo

Si una recepción confirmada deja la reposición pendiente, revisar primero el
worker de reposición. Tras desplegar la recuperación de suministros, buscar:

```bash
sudo journalctl -u farmacentral-worker@replenish_inventory.service \
  --since "15 minutes ago" -o short-iso --no-pager \
  --grep 'Replenishment supply resumed'
```

`basis=usable_stock` permite continuar con unidades utilizables existentes; no
atribuye su origen a esa entrega. Las operaciones antiguas exigen al menos la
cantidad solicitada, sin exigir conservar además todo el stock previo. Las nuevas
guardan la cantidad útil necesaria y, con inventario inicialmente completo y sin
recepciones competidoras, pueden reconocer unidades recibidas aunque luego se
consuman (`basis=receipt_ledger`). Antes del tiempo de disponibilidad o sin respuesta
confirmada siguen pendientes. El costo se contabiliza una vez al cerrar la
operación; el diagnóstico expone `goal.last_supply` y sus datos de referencia.
No requiere migraciones ni editar estados con SQL, y no libera inventario incierto.

El stock protegido combina pedidos y buffer con los objetivos del reabastecedor
sin sumar dos veces solicitudes que ya incluyen esos compromisos. Las solicitudes
independientes sí se suman; se conservan buffers, reservas, margen y vida útil.
Después de desplegar, revisar el plan actualizado:

```bash
APP_ENV_FILE="$PWD/.env" .venv/bin/python -m scripts.sell_surplus --once
```

Si las cantidades son positivas y el plan es adecuado, instalar y habilitar el
servicio continuo (cada 120 s). El instalador sólo copia la unidad, no la inicia:

```bash
bash deploy/install-systemd-units.sh
sudo systemctl enable --now farmacentral-surplus.service
sudo journalctl -u farmacentral-surplus.service -f -n 50
```

La unidad habilitada acompaña el target de workers en reinicios y paradas. No
ejecutar otro vendedor manual en paralelo. Para detenerla:
`sudo systemctl disable --now farmacentral-surplus.service`.
Si `quantity` sigue en cero, no hay excedente bajo la política actual: revisar
buffers, pedidos, lotes y espacio. Sin la opción explícita descrita abajo, el
vendedor no genera inventario ni compra insumos. No desproteger pedidos para
sumar operaciones.

En producción, la unidad incluye `--stock-market`. Cuando no queda ningún lote
vendible, esta opción solicita a Farma Central uno de los tres insumos de acceso
directo del grupo hasta cubrir primero toda la demanda protegida y, por encima de
ella, un lote de venta. La solicitud queda registrada en `ReplenishmentState`, se
limita por el presupuesto compartido y no se repite mientras su recepción esté
pendiente o incierta. Sólo después de observar las unidades recibidas se publica
la oferta. La previsualización sin `--execute` nunca solicita inventario.

El total local cuenta cierres de `MarketTrade`, no unidades. Si difiere del portal,
contrastar ofertas por ID antes de ajustar métricas. Por ejemplo, consultar una
oferta antigua que la vista no muestra (sólo lectura remota; respetar 429):

```bash
APP_ENV_FILE="$PWD/.env" .venv/bin/python - <<'PY'
import asyncio
from app.clients.trades import TradesClient

async def main():
    async with TradesClient() as client:
        offer = await client.get_offer("6afdb7788bf79117168f0a50")
    print({"id": offer.id, "estado": offer.status, "tipo": offer.side,
           "publicador": offer.publisher_group,
           "contraparte": offer.counterparty_group, "oc": offer.order_id,
           "cantidad": offer.quantity, "cierre": str(offer.closed_at)})

asyncio.run(main())
PY
```

Para una venta publicada por otro grupo, `estado=closed` y `contraparte=3`
confirman que el grupo 3 compró; contrastar también OC y fecha del período.
Repetir para las ofertas discrepantes sin ráfagas. Conservar resultados privados;
una lista visible incompleta no justifica borrar cierres locales ni cambiar roles.

Ensayar también el SHA anterior en la copia restaurada. Para revertir aplicación,
detener procesos y usar un checkout limpio compatible con el esquema. No hacer
downgrade ni restaurar encima de la DB activa. Después del reset remoto, conservar
la época nueva: **no reactivar la DB/período anterior**. En versiones sin
`APP_ENV_FILE`, retirar `.env.local` del directorio activo y guardarlo privadamente.

## 26. Monitorear, registrar incidentes y cerrar

Si siguen aumentando los rechazos, obtener una fotografía coherente de la base
activa antes de ajustar políticas. Después de desplegar el script, ejecutar en
el servidor:

```bash
cd /home/integracion/farmacentral-backend
APP_ENV_FILE="$PWD/.env" .venv/bin/python -m scripts.diagnose_e3_orders \
  --output "/tmp/e3-diagnosis-$(date -u +%Y%m%dT%H%M%SZ).json"
```

Cruza pedidos pendientes y rechazos recientes, capacidad por plazo, stock por
ubicación, reposición/presupuestos, producción incompleta, compras/custodia y
entregas de mensajería del período configurado. No llama APIs ni LLM; PostgreSQL
impone una transacción de sólo lectura con timeout de 15 s por consulta. No
exporta payloads, teléfonos ni texto de mensajes. Conserva el JSON privado
(permisos `600`, sin sobrescribir archivos); incluye importes operativos.
Por defecto limita a 30 los detalles de cada sección, priorizando pedidos
activos; `truncated` indica recortes y `--limit 100` permite ampliarlos. Los
conteos cubren todo el período. El estado local puede estar atrasado respecto del
portal: contrastar IDs antes de concluir que faltan compras o alterar registros.
Si falla, devuelve 1 e informa sólo el tipo de error.

Para pedidos con entrega al día siguiente, configurar
`EXTERNAL_ORDER_STOCK_WAIT_SECONDS=172800` en el `.env` del servidor y reiniciar
`farmacentral-worker@fulfill_external_orders.service`. La espera sigue terminando antes
del plazo menos el margen de despacho; no permite aceptar sin capacidad real.
Cambiar `.env.example` no actualiza la configuración de producción.

La planificación consulta un libro de ventas por decisión; si llega al máximo
de 200 resultados, consulta por SKU para no perder ofertas anteriores. Las
cotizaciones compartidas sólo duran esa decisión; la ejecución mantiene su
verificación previa. Cuando termina ofreciendo comprar el kit, el diagnóstico
incluye en `sourcing[].latest_plan.reason` las alternativas descartadas por costo,
capacidad o plazo. Las ofertas abiertas existentes actualizan esa explicación
al revisarse, sin republicarse. Tras desplegar, dejar pasar un ciclo de reposición
y repetir el diagnóstico para ver la causa actual; esto no garantiza que exista
una ruta de fabricación viable.

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
