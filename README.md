# Farmacentral Backend

Backend FastAPI con PostgreSQL, SQLAlchemy y Alembic.

## Ejecutar en local

Requisitos: Python 3.12 y Docker con Compose.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

cp .env.example .env.local
ln -sfn .env.local .env
# Completar .env.local con las credenciales de desarrollo.

docker compose up -d postgres
docker compose exec postgres pg_isready
alembic upgrade head
uvicorn app.main:app --reload
```

La API queda disponible en:

- `http://127.0.0.1:8000/api/health`
- `http://127.0.0.1:8000/docs`

Ejecutar las verificaciones:

```bash
python -m scripts.test_e3
ruff check app scripts tests
ruff format --check app scripts tests
```

Detener PostgreSQL:

```bash
docker compose down
```

### Preparación de la Entrega 3

Para observar conexión, migraciones y salud local sin modificar datos:

```bash
python -m scripts.verify_e3_baseline --api-url http://127.0.0.1:8000
```

`--inventory` añade una lectura del inventario oficial del ambiente configurado;
`--check-services` consulta systemd. Ninguna opción llama al LLM, fabrica o paga.
La captura de documentación pública se ejecuta por separado mediante
`scripts.capture_e3_contracts`; no realiza operaciones SOAP ni comerciales.

El modelo de datos del paso 2 incorpora períodos,
facturas, tareas/intentos durables y referencias de mercado. Su migración
`c7b4e2a91d60` se aplica con `alembic upgrade head`. Conserva el checkout entero
de E1 y prepara precios decimales para órdenes externas; todavía no emite ni paga
facturas automáticamente.

El cliente SOAP del paso 3 permite consultar saldo y
facturas, emitir y pagar mediante WS-Security. Para probar sólo lectura:

```bash
.venv/bin/python -m scripts.verify_billing_connection --include-invoices
```

El paso 4 incorpora la conciliación continua de facturas
y saldo con cursores durables. Aplica `alembic upgrade head` y selecciona un
período contable explícito para probarla:

```bash
.venv/bin/python -m scripts.reconcile_billing --period-id UUID_DEL_PERIODO --once
```

Sin `--once` permanece revisando; también admite `BILLING_PERIOD_ID` en la
configuración. Este worker sólo consulta el servicio remoto: no emite ni paga.

Los pasos 5 y 6 añaden emisión y pago automáticos durables. Permanecen
desactivados por defecto y se habilitan con `--emit-sales` y `--pay-invoices`, o
con `BILLING_EMIT_SALES_ENABLED` y `BILLING_PAY_INVOICES_ENABLED`.

El paso 7 calcula margen devengado, costos por canal, cobertura de facturación,
deuda pendiente, frescura de conciliación y cierres de mercado. El worker de
facturación guarda un snapshot cada diez minutos por defecto. Para capturar uno
manual —o el cierre del período— sin llamar servicios externos:

```bash
.venv/bin/python -m scripts.capture_business_metrics --period-id UUID_DEL_PERIODO
.venv/bin/python -m scripts.capture_business_metrics \
  --period-id UUID_DEL_PERIODO --closing
```

`APP_COMMIT_SHA=auto` asocia cada snapshot al commit desplegado mediante Git;
también admite un hash explícito. La migración `4f2a7c91e6d3` crea la tabla de
snapshots.

El paso 14 persiste el historial de mensajería por teléfono normalizado. Cada
mensaje conserva remitente y payload originales, dirección, fecha, grupo,
ambiente y período; las respuestas exitosas quedan vinculadas al evento que las
originó y, mediante éste, a su OC. La migración `91c4e7a2d5f0` añade el historial.
Los pasos 15 y 16 clasifican consultas y resuelven sus OCs contra el servicio
oficial. Tanto los IDs explícitos como las referencias naturales sólo consideran
pedidos anteriores del mismo teléfono, grupo, ambiente y período; además, se
verifica el proveedor de la OC oficial. Una referencia ajena o ambigua no
selecciona una OC por defecto. La respuesta conserva la evidencia usada y un
snapshot del estado oficial.

Un `409` de mensajería deja el evento fallido sin reintento automático: no prueba
que la clasificación coincida ni que el cliente haya recibido un texto. No se
fabrican fechas de respuesta ni mensajes salientes. Los intentos se guardan antes
de llamar al servicio remoto, conservando la posibilidad de recuperar una OC con
`scripts.recover_messaging_order` aunque se haya perdido su respuesta.

### Mensajería saliente — paso 17

El cliente existente incorpora dos operaciones definidas por el enunciado E3:

- `MessagingClient.respond_inquiry(message_id, order_id=...)` declara
  `status=inquiry` en `POST /messaging/messages/{messageId}/response`.
- `MessagingClient.send_reply(message, text=...)` envía el texto mediante
  `POST /messaging/messages`. Deriva `to` y `replyTo` del payload original del
  webhook, sin permitir que el llamador cambie el destinatario o el mensaje.

Ambas reutilizan autenticación, timeout y manejo de errores. Un timeout no
repite automáticamente el POST; el único reintento interno es renovar el token
ante un `401`. Un `409` de clasificación conserva su error específico; un `409`
del envío de texto es un error HTTP y no demuestra una clasificación previa.

`respond_order` devuelve ahora `MessagingResponse`, conservando `order_id` y
`test`. Si el servicio confirma `test=true` sin OC, el worker guarda esa respuesta
y termina el evento sin importar, decidir ni facturar una orden inexistente. El
prefijo del `messageId` no basta para reconocer una prueba. Si existe un
`orderId`, la OC sigue el flujo normal, incluso si la respuesta indica prueba.

El contrato E3 de mensajería quedó publicado el 13 de noviembre y su captura
está en `docs/e3/contracts/2026-11-13/`. Confirma `inquiry`, el envío de texto,
`200` para clasificación, `201` para envío, el límite de 500 caracteres y la
ventana de 30 minutos. El cliente exige esos códigos y conserva la confirmación
JSON; si la clasificación incluye estado u OC, deben coincidir con la consulta.
El contrato no publica consulta ni clave de idempotencia para un envío, por lo
que un resultado incierto no se repite automáticamente. La prueba real de
recepción contra el chat de desarrollo sigue pendiente.

El 12 de noviembre a las 18:18 de Chile se ejecutó un envío real con un webhook
auténtico recibido a las 18:16: autenticación `200`, pero el POST de texto devolvió
`404`. Hubo un solo intento, sin llamadas al LLM ni operaciones de órdenes. No se
confirmó entrega y ese `404` no permite distinguir entre una ruta no habilitada
y una referencia rechazada por el servicio. La evidencia sin datos del cliente
está en `docs/e3/contracts/2026-11-12/messaging-outbound-probe.json`.

El paso 18 conecta ambas operaciones al worker con las fases durables descritas
más abajo. La recepción real en el chat continúa pendiente; la captura del
contrato y las pruebas locales no sustituyen esa comprobación de transporte.

Pruebas locales, con servicios simulados y PostgreSQL de pruebas:

```bash
.venv/bin/python -m pytest -q tests/clients/test_messaging.py \
  tests/services/test_messaging_processing.py tests/services/test_messaging_inquiry.py \
  tests/services/test_messaging_recovery.py tests/services/test_e2_idempotency.py
```

Para probar sólo el envío HTTP, usa **Simular mensaje** en el portal de desarrollo
y guarda el payload auténtico que recibió el backend en `/tmp/mensaje-paso17.json`.
También sirve un mensaje del chat cuando esa función esté habilitada. No hace
falta introducir un texto especial: se utiliza el contenido real del simulador.

```bash
.venv/bin/python -m scripts.verify_messaging_outbound \
  --message-file /tmp/mensaje-paso17.json \
  --text "Prueba paso 17: respuesta recibida."
```

El comando exige la URL de desarrollo y un mensaje de menos de treinta minutos;
usa su teléfono y `messageId` originales, sin consultar la base ni el LLM ni
clasificar o crear una OC. Hace un solo envío y reporta errores sin repetirlo.
`status=accepted` significa que hubo una confirmación HTTP; no demuestra que el
texto apareció en un chat. La pantalla con sólo **Simular mensaje** y **Probar
conexión** no permite esa comprobación visual. **Probar conexión** verifica la
recepción del webhook/OIDC y el marcador de prueba; no prueba el envío de texto.
Para verificar la entrega real debe estar habilitado el chat. Ante un resultado incierto,
no repitas el comando a ciegas: el contrato todavía no garantiza deduplicación.
El JSON del webhook es un archivo local con datos del cliente y no se versiona.

### Consultas durables y cuota compartida — paso 18

El webhook sigue confirmando únicamente la recepción durable; la interpretación
y los envíos ocurren en el worker. Para una consulta, éste guarda la decisión,
la OC resuelta, su snapshot y el texto antes de enviar. Después confirma primero
la clasificación (`inquiry` y su OC) y luego el texto al teléfono y mensaje
originales. Una referencia ajena o ambigua se clasifica como `discarded` y recibe
una solicitud de aclaración, sin inventar una OC.

Las fases quedan en `InboundEvent.resolution_evidence.delivery`, reutilizando
el modelo existente, sin migraciones nuevas. Cada acción conserva estado,
intentos, fecha de intento, confirmación, latencia desde `receivedAt` y causa de
fallo. El historial saliente se agrega al confirmar el POST del texto;
`responded_at` requiere ambas confirmaciones antes de `receivedAt + 30 minutos`.
Un reinicio después de clasificar retoma sólo el texto guardado, sin consultar
otra vez la OC ni usar el LLM. También puede finalizar el estado local después
del plazo si ambas confirmaciones ya estaban guardadas a tiempo.

Un rechazo explícito recuperable, como `429`, conserva la acción pendiente y
su próxima fecha de intento. Un timeout, desconexión, confirmación inválida,
`409`, `5xx` o caída durante un POST deja un resultado incierto y exige revisión:
no se repite automáticamente. El contrato disponible no permite resolver esos
casos por consulta o deduplicación; una tabla local no garantiza envío exactamente
una vez. Al vencer el plazo no se inicia otro envío.

La cola atiende primero los mensajes con plazo más próximo según su fecha
original, con preferencia por consultas a igual fecha. Los nuevos valores por
defecto son `MESSAGING_POLL_INTERVAL_SECONDS=5` y `MESSAGING_BATCH_SIZE=10`.
La espera del LLM aplaza ese evento, sin detener respuestas ya interpretadas.
Los valores explícitos que ya existan en `.env` siguen teniendo prioridad.

Todos los procesos y herramientas que usen `LLMGatewayClient` comparten una
reserva en archivo, protegida con bloqueo entre procesos. La ruta por defecto
incluye grupo y gateway, independientemente de la key, modelo o período de
negocio. En los workers systemd usa `/var/lib/farmacentral`; en local recurre
a un directorio del usuario en `/tmp`. `LLM_GATEWAY_RATE_LIMIT_FILE` permite
fijar la misma ruta para todos los consumidores del host. Otros servidores o
llamadas directas que omitan el cliente deben coordinarse aparte; el gateway
aplica la cuota global del grupo.

Se conserva `max_tokens=256`, el catálogo reducido y el contexto de un solo
mensaje. Ante los límites comunicados de 5 requests/minuto y 1000 tokens/minuto,
el cliente reserva conservadoramente una ventana completa por llamada, incluido
su timeout, y espera al menos 60 segundos desde su finalización. Esto evita
estimar tokens con otro tokenizer; el gateway sigue validando el tamaño de cada
petición. `LLM_GATEWAY_MIN_INTERVAL_SECONDS` permite espaciar más las llamadas.
El archivo registra requests, tokens reales de entrada/salida/total informados
por el gateway y uso desconocido cuando falta esa información, sin guardar keys
ni prompts. El payload completo de cada interpretación conserva su `usage` en
`llm_output`.

El presupuesto de USD 2 por treinta días sigue siendo responsabilidad del
gateway: no se supone una renovación con E3 ni se calcula un costo sin tarifas.
Al recibir un error de presupuesto, el bloqueo se conserva tras reinicios. Sólo
debe reinicializarse su archivo cuando se confirme la renovación real, con los
consumidores detenidos. El 12 de noviembre se consultó el endpoint documentado
de metadatos `GET /key/info`, sin generar texto: devolvió `403`, por lo que no
se pudo confirmar saldo ni cuotas vigentes con esta credencial.

Las pruebas simuladas cubren reinicios entre confirmaciones, resultados inciertos,
plazos, destinatario original, uso compartido entre procesos y auditoría de tokens:

```bash
.venv/bin/python -m pytest -q tests/services/test_messaging_inquiry_flow.py \
  tests/services/test_messaging_processing.py tests/clients/test_llm_rate_limit.py \
  tests/clients/test_llm_gateway.py
```

### Retiros de lote y mercado — paso 19

Los retiros incluyen las unidades adquiridas en mercado, su transformación y las
entregas posteriores. El reporte agrega las reventas a otras distribuidoras con
la OC oficial y los IDs de unidad de `CustodyEvent`, conservando el historial
aunque ya no haya stock. Esas ventas no generan notificaciones de cliente final;
las entregas a clientes finales mantienen su notificación habitual.

Si falta el lote origen, el worker consulta y sincroniza el inventario oficial
completo antes de considerar un reporte vacío. Revisa también el historial
retenido: identidades de lote, recepción/producción de cada unidad, producción
pendiente y evidencia de unidades de las operaciones de mercado. Un listado
truncado, un lote provisional o una transferencia sin resolver impiden declarar
que el lote es ajeno. El resultado negativo depende de conservar ese historial;
una base borrada o incompleta no se reconstruye consultando sólo stock actual.
Cuando se comprueba ausencia, se envía un reporte vacío y no se realizan
movimientos ni notificaciones. El plazo se limita a treinta minutos desde
`issuedAt`, incluso si el payload declara una fecha posterior.

Las unidades afectadas quedan bloqueadas localmente antes de consultar ofertas.
Una oferta de venta abierta debe cancelarse y su estado terminal confirmarse
por GET antes de liberar la reserva y mover las unidades. Tras un timeout se
vuelve a consultar el estado. Si gana un cierre concurrente, se concilia la OC;
sin evidencia oficial de las unidades transferidas, el retiro queda pendiente
y no se mueve ese stock. La selección FEFO original no prueba qué unidades
transfirió el mercado. `TradeCustodyService.record_trade_units` permite registrar
esa evidencia confirmada aunque el retiro ya haya bloqueado sus reservas.

La migración `b8c2d19f460a` añade una columna JSON de evidencia a `recalls` para
guardar la comprobación de pertenencia y las entregas de mercado investigadas.
Antes de ejecutar la versión nueva:

```bash
.venv/bin/alembic upgrade head
```

`GET /api/recalls/{recall_id}` agrega `ownership` y `market_deliveries`, con OC,
oferta, contraparte e IDs de las unidades. Los retiros vacíos pueden reabrirse
si posteriormente se identifica el lote en el grupo; la sincronización bloquea
ese lote y sus derivados antes de que el worker actualice el reporte.

Las pruebas usan servicios simulados y PostgreSQL de pruebas:

```bash
.venv/bin/python -m pytest -q tests/services/test_market_recalls.py \
  tests/services/test_recall_response.py tests/services/test_recall_worker.py \
  tests/models/test_market_recall_migration.py tests/api/test_recalls.py
```

### APIs operativas y coordinación con frontend — paso 20

La trazabilidad incluye las compras y ventas de los lotes ancestros, además del
lote consultado y sus derivados. Conserva las OCs, contrapartes, SKU e IDs de
unidad; una venta del insumo no se confunde con una entrega del kit. Tanto
trazabilidad como retiros consultan PostgreSQL, sin depender de SOAP, mercado o
LLM. El catálogo y el checkout mantienen sus dependencias propias, separadas
de esos servicios.

El equipo puede consultar `/api/operations/waiting-orders`, `/offers`,
`/invoices`, `/metrics` y `/workers` bajo el mismo prefijo `/api/operations`.
Son consultas
locales de sólo lectura: no activan conciliaciones ni operaciones comerciales.
Cada una requiere `period_id`, con validación de grupo y ambiente. Las listas
usan `limit`, `offset` y `has_more`; las fechas de observación y captura permiten
distinguir lo leído localmente de una actualización remota.

Para habilitar estas consultas, configura `OPERATIONS_API_KEY` con una clave
privada independiente y envíala como `Authorization: Bearer <clave>`. Si no está
configurada, permanecen desactivadas (`503`); una credencial incorrecta produce
`401`. La clave no se incluye en el frontend público. Los esquemas y la
coordinación de contratos están documentados en `docs/API.md` y `/openapi.json`.
Este repositorio no contiene el frontend; los tipos públicos existentes se
conservan. Este paso no requiere migraciones nuevas.

Las pruebas simulan una caída de los servicios E3 y verifican compra, inicio de
checkout, trazabilidad y recalls, además de autenticación y lectura sin escrituras:

```bash
.venv/bin/python -m pytest -q tests/api/test_operations.py \
  tests/api/test_e3_frontend_availability.py
```

### Workers E3 y supervisión — paso 21

El target ejecuta `scripts.process_billing` y `scripts.process_trades`. El
primero concilia facturas y, cuando sus flags están habilitados, emite ventas y
paga deudas antes de guardar métricas; el segundo concilia ofertas y custodia.
Cada proceso usa un advisory lock por período, persiste `next_retry_at` en las
colas existentes y confirma su transacción antes de dormir.

Cada ciclo actualiza `worker_heartbeats` con inicio, último éxito o fallo,
fallos consecutivos, profundidad de cola y fecha del elemento más antiguo.
`GET /api/operations/workers?period_id=...` permite observar esos datos sin
invocar servicios externos. Un proceso `active` con heartbeat antiguo o sin
`last_success_at` no se considera evidencia de operación correcta.

Al desplegar este cambio por primera vez, instala el target actualizado antes
de ejecutar `release.sh`; el instalador también detiene los nombres antiguos
para que no queden productores duplicados:

```bash
cd /home/integracion/farmacentral-backend
./deploy/install-systemd-units.sh
sudo systemctl restart farmacentral-workers.target
```

El despliegue habitual reinicia el target y falla si los dos workers E3 no
quedan activos. Para que el ciclo financiero ejecute las mutaciones requeridas,
el entorno del servidor debe seleccionar el período y habilitarlas:

```dotenv
BILLING_PERIOD_ID=UUID_DEL_PERIODO
BILLING_EMIT_SALES_ENABLED=true
BILLING_PAY_INVOICES_ENABLED=true
```

La clave y el período no se imprimen en logs. Los timeouts de Billing, Trades,
OCs y Farma Central continúan configurándose por sus variables `*_TIMEOUT_SECONDS`.

### Esperar stock antes de rechazar una OC

Para activar la espera en el `.env` del ambiente correspondiente:

```dotenv
EXTERNAL_ORDER_STOCK_WAIT_SECONDS=3600
EXTERNAL_ORDER_STOCK_RETRY_SECONDS=30
EXTERNAL_ORDER_DISPATCH_MARGIN_SECONDS=300
```

Sin `EXTERNAL_ORDER_STOCK_WAIT_SECONDS`, o con valor `0`, se conserva el rechazo
inmediato. Con espera activa, una OC sin capacidad queda `RECEIVED` localmente y
`creada` en la plataforma. El worker de cumplimiento la reevalúa cada 30 segundos,
aproximadamente, y acepta cuando hay capacidad; si sigue faltando, rechaza al
cumplirse una hora desde `created_at` o al faltar cinco minutos para la entrega,
lo que ocurra primero. Un reinicio no renueva esa ventana.

La recepción y la respuesta de mensajería siguen sus plazos originales; su OC
puede quedar pendiente después de responder el mensaje. La espera no inicia
producciones ni añade llamadas al LLM. No requiere migraciones y no reabre OCs
rechazadas. Actualizar la configuración requiere reiniciar los workers que
reciben y deciden órdenes.

### Reposición de kits

`replenish_inventory` conserva la producción secuencial y el lock de
acondicionamiento. Antes de cada kit vuelve a priorizar el menor stock utilizable.
Hace hasta tres intentos de sincronización ante conexión, timeout o un cambio
en el conteo de inventario. Estos reintentos se limitan a lecturas, sin añadir
repetición directa de POST de abastecimiento o producción. Si la selección local detecta
insumos faltantes antes de pedir producción, resincroniza y prepara una vez más.
Un fallo local de receta permite continuar con otros kits sólo si no hay corridas
pendientes; las corridas inconclusas conservan el mecanismo de recuperación.

Los logs incluyen SKU, insumos faltantes, reintentos y duración por kit y ciclo.
Los fallos de validación persistentes siguen deteniendo el ciclo. Para anticipar
reposición, se puede configurar un mínimo de `4` y objetivo `8`, contrastando
consumo, espacio y vencimiento. `.env.example` muestra esos valores; los defaults
siguen en `2` y `6`, y los `.env` existentes no se modifican.

## Ejecutar en el servidor

El repositorio debe estar en `/home/integracion/farmacentral-backend`.
PostgreSQL se ejecuta como servicio del sistema, sin Docker.

### Configuración inicial

```bash
cd /home/integracion/farmacentral-backend

python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .

cp .env.example .env.dev
cp .env.example .env.prod
chmod 600 .env.dev .env.prod
# Completar cada archivo con las credenciales de su ambiente.

# Desarrollo queda activo inicialmente.
ln -sfn .env.dev .env

sudo systemctl enable --now postgresql
sudo -u postgres pg_isready
.venv/bin/alembic upgrade head
```

Instalar nginx y las unidades systemd:

```bash
sudo cp deploy/farmacentral-backend.service /etc/systemd/system/
sudo cp deploy/farmacentral-inventory-sync.service /etc/systemd/system/
sudo cp deploy/farmacentral-inventory-sync.timer /etc/systemd/system/
./deploy/install-systemd-units.sh
sudo cp deploy/nginx.conf /etc/nginx/sites-available/farmacentral-backend
sudo ln -sfn /etc/nginx/sites-available/farmacentral-backend \
  /etc/nginx/sites-enabled/farmacentral-backend

sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now farmacentral-backend.service
sudo systemctl enable --now farmacentral-workers.target
sudo systemctl enable --now farmacentral-inventory-sync.timer
sudo systemctl reload nginx
```

Comprobar el servidor:

```bash
curl --fail http://127.0.0.1:8000/api/health/ready
systemctl is-active farmacentral-backend.service
systemctl is-active farmacentral-workers.target
for worker in poll_sftp_orders poll_pubsub_orders process_messaging \
  fulfill_external_orders process_recalls replenish_inventory \
  process_billing process_trades; do
  systemctl is-active "farmacentral-worker@${worker}.service"
done
systemctl is-active farmacentral-inventory-sync.timer
```

### Cambiar de desarrollo a producción

Antes del cambio, completar `.env.prod`, instalar la llave Pub/Sub de producción
y comprobar que todas sus URLs y credenciales sean productivas.

Cuando se decida activar producción:

```bash
cd /home/integracion/farmacentral-backend
sudo systemctl stop farmacentral-inventory-sync.timer
ln -sfn .env.prod .env
sudo systemctl restart farmacentral-backend.service
sudo systemctl restart farmacentral-workers.target
sudo systemctl start farmacentral-inventory-sync.timer
curl --fail http://127.0.0.1:8000/api/health/ready
```

El enlace activo se comprueba con:

```bash
readlink .env
```

### Actualizar el servidor

Cada `push` a `main` ejecuta el despliegue automático. Para desplegar
manualmente:

```bash
cd /home/integracion/farmacentral-backend
git pull --ff-only origin main
./deploy/release.sh
```

Logs principales:

```bash
sudo journalctl -u farmacentral-backend.service -n 100 --no-pager
sudo journalctl -u 'farmacentral-worker@*' -n 100 --no-pager
sudo journalctl -u farmacentral-inventory-sync.service -n 100 --no-pager
```

### Idempotencia y recuperación — paso 22

Las tareas financieras se serializan por `BillingTask.id` mediante el bloqueo
asesor PostgreSQL existente, retenido entre commits y liberado al terminar el
proceso. Esto protege también llamadas desde herramientas distintas del worker.
Después de adquirirlo se relee el estado: una tarea confirmada no se vuelve a
emitir ni pagar. La unicidad local organiza el trabajo; no demuestra por sí sola
que un efecto remoto ocurrió una única vez.

| Operación | Recuperación después de una caída o respuesta perdida |
| --- | --- |
| Emisión | Conservar intento y recibo. Consultar la OC oficial; si figura facturada sin asociación verificable, conservar incertidumbre y conciliar la factura oficial. No reemitir para descubrir su ID. |
| Pago | Consultar el ID exacto en `paid` y `pending`, usando la ventana de emisión original. Un recibo guardado se verifica antes de aplicarlo. Sólo una observación válida permite avanzar; no suponer que un timeout equivale a rechazo. |
| Publicación de oferta | Mantener las reservas ante `creation_uncertain`. Asociar únicamente un ID oficial verificado y conciliar por GET; similitud de SKU, precio o fecha no prueba identidad. |
| Aceptación | Serializar por período/oferta. Si ya existe intención local, usar GET y validar cierre, OC y contraparte antes de retornar. No repetir el POST pendiente o incierto, aunque el GET aún muestre `open`. |
| Transferencia | La OC comercial no identifica unidades. Registrar sólo IDs oficiales mediante `record_trade_units`; conservar reservas hasta obtener evidencia. Los cierres sin evidencia cuentan como cola pendiente y no como éxito del worker. |
| Clasificación y texto | Recuperar las fases guardadas por separado. Un POST incierto queda para revisión; no duplicar clasificación ni texto por un `409` o un timeout. |

Para recuperar, detener primero cualquier intervención manual concurrente,
consultar las fuentes oficiales con los clientes/scripts de lectura existentes
y conservar IDs y recibos como evidencia. Una vez conciliados, los workers
retoman desde el estado confirmado. Mensajería sin consulta/deduplicación
publicada no admite reenvío seguro automático; no borrar la fase `uncertain`.
Las notificaciones de recall conservan el procedimiento existente
`scripts.resolve_recall_notification`, que requiere evidencia oficial.

Las pruebas usan servicios simulados y PostgreSQL temporal. Incluyen procesos
independientes compitiendo por la misma tarea de emisión/pago y la recuperación
de una aceptación incierta, además de las pruebas existentes de caídas,
recibos, reserva de unidades y fases de mensajería. No consumen gateway ni
realizan pagos, compras o transferencias reales.

### Suite aislada y corpus — paso 23

Ejecutar desde la raíz con dependencias de desarrollo y los binarios de servidor
PostgreSQL (`initdb`, `pg_ctl`) instalados, como usuario sin privilegios de root:

```bash
.venv/bin/python -m scripts.test_e3 --report /tmp/farmacentral-e3-results.json
.venv/bin/python -m scripts.test_e3 --corpus-only --report /tmp/messaging-corpus.json
.venv/bin/ruff check app scripts tests
.venv/bin/ruff format --check app scripts tests
```

El ejecutor crea un cluster PostgreSQL local temporal y sustituye tanto
`DATABASE_URL` como `TEST_DATABASE_URL` durante pytest. Usa los esquemas aislados
existentes, incluidos los ensayos de migración de E3. Reutiliza el bloqueo de
conexiones externas del ejecutor E2; los clientes remotos son simulados. Al
terminar, incluso si pytest falla, detiene el cluster y elimina sus archivos.
Si PostgreSQL no puede arrancar, devuelve error sin conectarse a la DB configurada.
No modifica `.env` ni necesita credenciales del curso. El ejecutor E2 se conserva
para los comandos heredados `--step20/--step21`, cuya numeración corresponde a E2.

El corpus está en `tests/fixtures/messaging/{development,evaluation}.json`:
24 mensajes sintéticos con fechas fijas, 12 por partición, textos separados y
varios teléfonos, grupos, períodos y pedidos previos. Cubre pedidos válidos,
cantidades ambiguas, fechas vencidas, consultas explícitas/naturales, referencias
ajenas y consultas ambiguas. Los casos incluyen una propuesta LLM simulada y
expectativas separadas. Se comprueba la interpretación determinista y la selección
de OC contra el historial; no se crean OCs ni se llama al gateway.

El informe JSON conserva el código de salida de pytest, casos recolectados y
casos con métricas, más `correct`, `total` y `accuracy` por partición/categoría
para intención, OC seleccionada, SKU, cantidad y fecha, cuando corresponden.
No mezcla desarrollo y evaluación ni contabiliza campos no aplicables como
aciertos. Las métricas reflejan el comportamiento con propuestas fijas, **no la
precisión de un LLM real ni el cumplimiento del curso**. Un caso que falla antes
de medirse no entra en el denominador; hay que comprobar el código de salida y
que los casos medidos coincidan con los recolectados, no sólo el porcentaje.

Cobertura de regresión reutilizada por el comando completo:

| Escenario | Pruebas |
| --- | --- |
| Creación tardía, ventanas solapadas y deuda antigua que cambia de estado | `test_billing_reconciliation.py` |
| Decimales, intereses antes/después de seis horas y pago incierto | `test_invoice_payments.py`, `test_sales_invoicing.py` |
| Oferta expirada o ganada por otro, cierres y conciliación | `test_trade_reconciliation.py`, `test_scarcity_procurement.py` |
| Reservas concurrentes, stock comprometido y escasez | `test_e3_market_concurrency.py`, `test_trade_custody.py`, `test_procurement_planning.py` |
| Recepción en pulmón y capacidad agotada | `test_e3_buffer_receipt.py`, `test_procurement_planning.py` |
| Productor recuperable e inventario cambiante | `production/test_e3_replenishment.py`, `inventory/test_resilient_collection.py` |
| Recall ajeno/mercado y webhooks HTTP autenticados | `test_market_recalls.py`, `tests/api/test_recalls.py`, `tests/api/test_messaging.py` |
| Reinicios y procesos financieros competidores | `test_e3_idempotency.py`, `test_messaging_inquiry_flow.py` |

Las rutas sin prefijo en la tabla son relativas a `tests/services/`.
Las pruebas automatizadas no envían pagos, aceptaciones ni mensajes reales.
La validación oficial de punta a punta sigue correspondiendo al paso 24.

### Validación real en desarrollo — paso 24

El procedimiento y el estado de la evidencia están en
[la guía de operación E3](docs/e3/OPERACION.md#24-validar-recorridos-reales-en-desarrollo).
El observador `scripts.validate_e3_flow` consulta sólo desarrollo, exige período
explícito, usa una transacción de lectura y conserva como pendientes las pruebas
manuales de chat, verdad/diff y reinicios. `--list-ids` permite descubrir períodos
y objetivos desde la DB del servidor sin iniciar operaciones. No reemplaza la
validación oficial con resultados simulados ni elimina la protección de E2.

En esta sesión se verificaron readiness, lectura SOAP y una única consulta
sintética autorizada al gateway (465 tokens). Los recorridos completos siguen
pendientes de acceso a la DB del servidor y de evidencia oficial por flujo.

La conciliación de facturas de mercado puede recuperar el vínculo con la OC
cuando la descripción oficial contiene una única referencia explícita
`order <ID de 24 caracteres hexadecimales>`. Antes de guardarlo verifica período,
rol comprador/vendedor, grupo, contraparte, principal exacto y fecha de cierre.
No infiere asociaciones por coincidencia de monto ni suma intereses al principal
para compararlo con la OC. Una descripción ausente, desconocida o ambigua deja
el vínculo pendiente; una contradicción se reporta como error. El vínculo se
recupera al volver a observar la factura, incluso si ya está pagada, sin crear
otra tarea de pago para una factura `paid`. El observador E3 conserva también
la descripción oficial en su evidencia privada.

### Preparación de producción y reset — paso 25

El procedimiento está en [la guía de operación E3](docs/e3/OPERACION.md#25-preparar-producción-y-una-época-nueva): respaldo y
restauración, DB nueva por época, instalación de unidades, límites y ensayo en
desarrollo. `scripts.check_e3_readiness` revisa configuración sin llamadas remotas;
`scripts.prepare_e3_epoch` verifica una DB migrada vacía y sólo crea el período
con `--create` y referencia explícita del reset confirmado. No borra historial.
Systemd y el deploy fijan `APP_ENV_FILE` para excluir `.env.local` del servidor.
La activación de producción y el ensayo remoto aún requieren comprobación real.

### Operación y métricas durante la evaluación — paso 26

[La guía de operación E3](docs/e3/OPERACION.md#26-monitorear-registrar-incidentes-y-cerrar) explica cómo activar el timer de observación
cada dos minutos, revisar alertas y conservar evidencia privada por período.
`scripts.monitor_e3` mide ambos sitios y consulta la DB en sólo lectura, sin
acciones de negocio. `--summary` calcula disponibilidad sobre muestras reales,
separa ensayos y muestra huecos. El cierre reutiliza la captura financiera
existente; instalar el monitor no acredita las 60 horas de operación real.
