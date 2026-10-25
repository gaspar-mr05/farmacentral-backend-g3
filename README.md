# Farmacentral Backend

API de integración construida con Python 3.12, FastAPI, PostgreSQL, SQLAlchemy
y Alembic.

## Configuración

La aplicación lee `.env` y luego `.env.local`. Copia el ejemplo y completa sus
valores antes de iniciar:

```bash
cp .env.example .env
```

Variables importantes:

- `DATABASE_URL`: conexión SQLAlchemy a PostgreSQL. En local normalmente usa
  `localhost:5432`.
- `POSTGRES_DB`, `POSTGRES_USER` y `POSTGRES_PASSWORD`: crean el contenedor de
  PostgreSQL local y deben coincidir con `DATABASE_URL`.
- `FARMA_CENTRAL_BASE_URL`: URL base HTTPS entregada para Farma Central.
- `FARMA_CENTRAL_API_SECRET`: secreto del grupo. No debe versionarse.
- `FARMA_CENTRAL_FTP` y `FARMA_CENTRAL_GROUP`: datos asignados al grupo.
- `MARKET_PRICE_CACHE_TTL_SECONDS`: duración máxima de la caché compartida de
  precios (30 segundos por defecto), para evitar consultas duplicadas.
- `MARKET_PRICE_MAX_RETRY_WAIT_SECONDS`: espera máxima para reintentar una
  consulta de precios limitada por tasa (2 segundos por defecto).
- `CHECKOUT_BASE_URL`: URL base de la pasarela de pagos (`dev` o `prod`).
- `APP_PUBLIC_URL`: URL pública del backend, usada por checkout para retornar el
  resultado del pago. En producción debe usar HTTPS y el dominio asignado.
- `FRONTEND_PUBLIC_URL`: URL opcional del frontend. Si se define, el retorno de
  checkout redirige a `/payment-result` en esa URL.
- `CORS_ORIGINS`: orígenes permitidos separados por coma. Puede quedar vacío si
  frontend y backend comparten dominio.

## Ejecutar en local

Requisitos: Python 3.12 y Docker con Docker Compose.

Desde la raíz del repositorio:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

cp .env.example .env
# Editar .env antes de continuar.

docker compose up -d postgres
docker compose exec postgres pg_isready
alembic upgrade head
uvicorn app.main:app --reload
```

La API queda disponible en:

- Salud: `http://127.0.0.1:8000/api/health`
- Documentación: `http://127.0.0.1:8000/docs`
- Guía de endpoints del repositorio: [`docs/API.md`](docs/API.md)

Para detener PostgreSQL local:

```bash
docker compose down
```

## Ejecutar en el servidor proporcionado

El proyecto vive en `/home/integracion/farmacentral-backend`. En el servidor,
PostgreSQL es un servicio del sistema: no se usa Docker.

### Primera instalación

Con el repositorio ya clonado en esa ruta:

```bash
cd /home/integracion/farmacentral-backend

python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .

cp .env.example .env
chmod 600 .env
# Editar .env con las credenciales del servidor.

sudo systemctl enable --now postgresql
sudo -u postgres pg_isready
.venv/bin/alembic upgrade head

sudo cp deploy/farmacentral-backend.service /etc/systemd/system/
sudo cp deploy/farmacentral-inventory-sync.service /etc/systemd/system/
sudo cp deploy/farmacentral-inventory-sync.timer /etc/systemd/system/
sudo cp deploy/farmacentral-worker@.service /etc/systemd/system/
sudo cp deploy/farmacentral-workers.target /etc/systemd/system/
sudo cp deploy/nginx.conf /etc/nginx/sites-available/farmacentral-backend
sudo ln -sfn /etc/nginx/sites-available/farmacentral-backend \
  /etc/nginx/sites-enabled/farmacentral-backend
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now farmacentral-backend.service
sudo systemctl reload nginx
```

Verificar:

```bash
sudo systemctl status farmacentral-backend.service --no-pager
curl --fail http://127.0.0.1:8000/api/health
```

### Frontend y nginx

nginx sirve el frontend (repositorio `farmacentral-frontend-g3`) en `/` y reenvía `/api`, `/docs`, `/redoc` y `/openapi.json` al backend. El
workflow del frontend publica cada build en
`/var/www/farmacentral-frontend/releases/<sha>` y apunta el enlace `current` a
la versión nueva.

`deploy/nginx.conf` incluye el bloque HTTPS con el certificado que creó
`certbot --nginx`. Para aplicar un cambio en ese archivo:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/farmacentral-backend
sudo nginx -t
sudo systemctl reload nginx
```

En `.env`, `APP_PUBLIC_URL` y `FRONTEND_PUBLIC_URL` deben ser
`https://choripan3.ing.uc.cl/` para que checkout vuelva al portal después del
pago.

### Actualizar el servidor

Cada `push` a `main` despliega mediante GitHub Actions. Para actualizar
manualmente:

```bash
cd /home/integracion/farmacentral-backend
git pull --ff-only origin main
./deploy/release.sh
```

El script instala dependencias, ejecuta migraciones, reinicia el backend y
comprueba su estado.

Comandos útiles:

```bash
sudo systemctl restart farmacentral-backend.service
sudo journalctl -u farmacentral-backend.service -n 100 --no-pager
sudo systemctl list-timers farmacentral-inventory-sync.timer
sudo journalctl -u farmacentral-inventory-sync.service -n 100 --no-pager
```

## Sincronizar inventario

Con PostgreSQL activo y las variables de Farma Central configuradas:

```bash
# Local, con el entorno virtual activado:
python -m scripts.sync_inventory

# Servidor:
cd /home/integracion/farmacentral-backend
.venv/bin/python -m scripts.sync_inventory
```

La sincronización es idempotente: actualiza catálogo, espacios e inventario sin
duplicar los registros existentes.

En el servidor, `farmacentral-inventory-sync.timer` inicia la primera
sincronización unos 30 segundos después de activarse y programa la siguiente
aproximadamente un minuto después de que termine la anterior. De esta forma no
se superponen dos ejecuciones del mismo servicio. Cada ejecución también mueve
las unidades refrigeradas expuestas y vuelve a consultar su vencimiento
efectivo cuando hubo movimientos.

El timer no selecciona dev o prod por sí mismo: usa `FARMA_CENTRAL_BASE_URL`,
las credenciales y `DATABASE_URL` del `.env` cargado por el servicio. No se debe
apuntar una base de datos productiva a Farma Central dev. Para ejecutar esta
automatización contra dev se debe usar una instalación y una base de datos dev
separadas.

Antes de habilitarlo por primera vez, comprobar la URL sin mostrar el secreto y
ejecutar una sincronización inmediata:

```bash
cd /home/integracion/farmacentral-backend
.venv/bin/python -c \
  'from app.core.config import get_settings; print(get_settings().farma_central_base_url)'
sudo systemctl start farmacentral-inventory-sync.service
sudo journalctl -u farmacentral-inventory-sync.service -n 100 --no-pager
```

Si la ejecución termina correctamente y el ambiente es el esperado, habilitar
la programación periódica:

```bash
sudo systemctl enable --now farmacentral-inventory-sync.timer
sudo systemctl status farmacentral-inventory-sync.timer --no-pager
```

La instalación de los archivos de systemd es un paso manual único porque el
workflow de despliegue no tiene permisos para escribir en
`/etc/systemd/system`. Si esos archivos cambian, deben copiarse nuevamente y
ejecutarse `sudo systemctl daemon-reload`. Los despliegues posteriores reinician
el backend, y systemd reinicia junto a él el timer y los workers instalados.

Si aparece `FarmaCentralConnectionError`, el problema ocurre antes de acceder a
PostgreSQL: el backend no pudo abrir una conexión con
`FARMA_CENTRAL_BASE_URL`. Comprueba que la URL de `.env` sea la entregada para
el proyecto y que el servidor pueda resolver y alcanzar su host:

```bash
.venv/bin/python -c \
  'from app.core.config import get_settings; print(get_settings().farma_central_base_url)'
getent hosts HOST_DE_FARMA_CENTRAL
curl --verbose --connect-timeout 10 https://HOST_DE_FARMA_CENTRAL
```

No publiques `.env` ni el secreto de la API al compartir la salida.

## Producción de kits

El flujo descubre las fórmulas y todos los kits vendibles directamente desde
Farma Central. Produce dependencias de forma recursiva, respeta los tamaños de
lote y la capacidad del área de acondicionamiento, y registra las unidades
consumidas y los lotes de salida.

Para probar un kit en dev se puede usar el abastecimiento sandbox:

```bash
python -m scripts.complete_kit_production \
  --sku KIT-RESP-ADULTO \
  --quantity 1 \
  --raw-material-source sandbox
```

Para producir 30 unidades de todos los kits usando abastecimiento normal:

```bash
python -m scripts.complete_kit_production --quantity 30
python -m scripts.verify_kit_production --minimum 30
```

Al terminar, `complete_kit_production` mueve al buffer las unidades que quedaron
en acondicionamiento para que los kits aparezcan como stock vendible. El comando
`python -m scripts.empty_packaging` permite repetir ese paso de forma segura.

El comando rechaza una URL de producción salvo que se agregue explícitamente
`--allow-production`. Dev y producción deben usar instalaciones y bases de datos
separadas para no mezclar sus linajes.

## Verificaciones de desarrollo

```bash
pytest
ruff check app scripts tests
ruff format --check app scripts tests
```

### Paso 20: recorrido completo de Entrega 1

```bash
pytest tests/e2e/test_entrega1.py -q
```

Estas tres pruebas parten con un insumo disponible, registran recepción y
movimiento, producen un intermedio y un kit, consultan catálogo, crean un pedido
y verifican pagos exitosos, cancelados y con error. El pago exitoso continúa con
asignación, despacho y trazabilidad desde el kit hasta los insumos y desde el
insumo hasta el cliente. Los otros pagos no permiten asignar ni despachar.

Usan PostgreSQL configurado por `TEST_DATABASE_URL` (el mismo valor predeterminado
de las demás pruebas), revierten sus datos al terminar y simulan Farma Central,
precios y checkout. Este recorrido automatizado no verifica las APIs externas
reales; esa validación debe hacerse en dev.

## Entrega 2: verificar la base de Entrega 1

El paso 1 de `IIC3103_Entrega2_Backend_Paso_a_Paso.pdf` valida la base existente.
Para comprobar una unidad ya despachada, con la base correspondiente configurada:

```bash
python -m scripts.verify_e1_traceability --unit-id ID_EXTERNO_DE_UNIDAD
```

El comando sólo consulta PostgreSQL: comprueba la asociación a pedido/cliente,
los eventos de custodia y el recorrido desde los lotes de entrada hasta la
entrega. No llama servicios externos ni consume el gateway LLM. Una historia
incompleta termina con un mensaje de error y código de salida distinto de cero.

Las verificaciones automatizadas siguen ejecutándose con `pytest`; usan un
esquema temporal aislado y UTC, sin depender de la zona horaria de PostgreSQL.
El resultado del ensayo real de desarrollo queda en
[`docs/ENTREGA2_PASO1_VALIDACION.md`](docs/ENTREGA2_PASO1_VALIDACION.md).

## Entrega 2: cliente de órdenes de compra

`PurchaseOrdersClient` autentica con `FARMA_CENTRAL_GROUP` y
`FARMA_CENTRAL_API_SECRET`. Reutiliza el token y lo renueva una vez ante un 401.
Por defecto, usa `/ordenes-compra/` en el mismo host de
`FARMA_CENTRAL_BASE_URL`, por lo que sigue el ambiente dev o producción configurado.
Se puede cambiar mediante `PURCHASE_ORDERS_BASE_URL`; el timeout se configura con
`PURCHASE_ORDERS_TIMEOUT_SECONDS` (10 segundos por defecto).

```python
from app.clients.purchase_orders import PurchaseOrdersClient


async def consultar_orden(order_id: str):
    async with PurchaseOrdersClient() as client:
        return await client.get_order(order_id)


async def aceptar_orden(order_id: str):
    async with PurchaseOrdersClient() as client:
        await client.update_order_state(order_id, "aceptada")
```

Las respuestas se validan con esquemas propios, separados del pedido local de
SQLAlchemy. El servicio permite solicitar `aceptada`, `rechazada` o `anulada`;
valida que el grupo y el estado actual permitan la transición. Una actualización
exitosa devuelve HTTP 204. `cumplida` y `vencida` son estados administrados por el
servicio y no se envían con este método.

Las excepciones exportadas por `app.clients.purchase_orders` distinguen
autenticación, timeout, conexión, errores HTTP y respuestas inválidas. Los errores
HTTP conservan `status_code` y, para 429, `retry_after_seconds` cuando el servidor
envía un valor numérico. Ante timeout, 429 o 5xx el cliente devuelve el error al
llamador; no repite automáticamente una actualización cuyo resultado sea incierto.
Los logs identifican la OC y el resultado sin incluir credenciales ni tokens.

Las pruebas del cliente no necesitan red ni PostgreSQL:

```bash
pytest tests/clients/test_purchase_orders.py tests/core/test_config.py -q
```

La validación realizada y la comprobación pendiente con una OC real se detallan en
[`docs/ENTREGA2_PASO2_VALIDACION.md`](docs/ENTREGA2_PASO2_VALIDACION.md).

## Entrega 2: pedidos externos y eventos persistentes

El paso 3 extiende `Order` con `external_order_id`, `external_customer_id`,
`delivery_deadline` y `external_status`. El canal `source` admite `web`, `sftp`,
`pubsub` y `messaging`. El estado local incorpora `received`, `accepted`,
`rejected` y `expired`; el estado externo conserva los valores oficiales en
español, como `creada`, `aceptada` y `cumplida`.

`external_order_id` es único entre todos los canales y se almacena como 24
caracteres hexadecimales en minúsculas. Los pedidos web conservan ese campo en
NULL y pueden coexistir sin una OC externa. Los pedidos externos pueden guardar
el ID oficial del cliente sin inventar un correo. El correo sigue siendo
obligatorio para crear pedidos web y `POST /api/orders` acepta únicamente
`source=web`; la ingestión externa se implementa en los pasos siguientes.

`InboundEvent` guarda el canal (`sftp`, `pubsub`, `messaging` o `recall`), una
referencia externa obligatoria, el payload JSON, las fechas de recepción y
procesamiento, y su estado (`pending`, `processing`, `processed` o `failed`).
Puede persistirse antes de que exista un pedido y asociarse después mediante
`order_id`. Incluye contador de intentos, último error y fechas del último
intento y del próximo reintento para el futuro worker.

La restricción UNIQUE de `(source, external_event_id)` impide guardar dos veces
el mismo evento, incluso con recepciones simultáneas. Los adaptadores deberán
tratar ese conflicto como una entrega repetida. Dos notificaciones de canales
distintos pueden asociarse al mismo pedido; la unicidad global de
`Order.external_order_id` impide crear una segunda OC local. Estas restricciones
preparan la deduplicación; el procesamiento y los reintentos se desarrollan en
los pasos posteriores.

Para actualizar la base configurada con Alembic:

```bash
.venv/bin/alembic upgrade head
```

La migración `a6d8e2c49130` conserva los pedidos y las relaciones de Entrega 1.
El downgrade se bloquea si hay pedidos o eventos de Entrega 2, para evitar perder
su información. Las etiquetas añadidas a los enums existentes de PostgreSQL
permanecen al revertir, igual que en la migración de despacho.

Pruebas del paso 3, con `TEST_DATABASE_URL` apuntando a PostgreSQL:

```bash
pytest tests/models/test_e2_orders.py tests/models/test_inbound_event.py \
  tests/models/test_e2_migration.py tests/api/test_e2_order_data.py -q
```

Verifican restricciones, inserciones concurrentes, persistencia entre sesiones,
asociación posterior del evento, migración desde Entrega 1, downgrade y consultas
de pedidos/trazabilidad sin correo. Crean esquemas temporales que se eliminan al
terminar y no llaman servicios externos.

## Entrega 2: recepción de pedidos por sFTP

El paso 4 se ejecuta separado de FastAPI. Instala las dependencias del proyecto y
aplica las migraciones existentes antes de ejecutarlo:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/alembic upgrade head
```

Configuración en `.env` o `.env.local`:

```dotenv
SFTP_HOST=choripan10.ing.uc.cl
SFTP_PORT=22
SFTP_DIRECTORY=.
SFTP_TIMEOUT_SECONDS=10
SFTP_POLL_INTERVAL_SECONDS=30
SFTP_BATCH_SIZE=100
SFTP_MAX_FILE_BYTES=65536
```

El usuario se deriva del grupo y del ambiente de `FARMA_CENTRAL_BASE_URL`:
`grupo3_desarrollo` en dev y `grupo3_produccion` en producción. `SFTP_USERNAME`
permite indicar otro usuario. `SFTP_PASSWORD` guarda la contraseña específica de
la casilla; si se omite, se intenta usar `FARMA_CENTRAL_FTP`. La contraseña sFTP
puede ser distinta del secreto de las APIs.

El cliente verifica la clave pública del servidor con `~/.ssh/known_hosts`.
`SFTP_KNOWN_HOSTS_FILE` permite usar otro archivo con las claves de confianza.
Una clave desconocida o distinta provoca un error de conexión. Este comportamiento
usa la verificación de servidor de [AsyncSSH](https://asyncssh.readthedocs.io/en/latest/api.html#asyncssh.connect).

Para revisar una sola vez la casilla y procesar eventos pendientes:

```bash
.venv/bin/python -m scripts.poll_sftp_orders --once
```

Para mantener el polling periódico mientras el proceso esté ejecutándose:

```bash
.venv/bin/python -m scripts.poll_sftp_orders
```

Cada revisión lee hasta 100 archivos XML regulares nuevos, de hasta 64 KiB por
defecto. Cierra la conexión sFTP antes de consultar las órdenes. No elimina ni
renombra archivos remotos. Los archivos ilegibles se vuelven a intentar en otra
revisión; un timeout termina la conexión para liberar la sesión.

Los archivos recibidos se guardan primero en `InboundEvent`, con su ruta completa,
XML en base64 y SHA-256. La ruta es la referencia única de una notificación
inmutable: repetir una lectura no sobrescribe el evento ni genera otra consulta.
El parser extrae únicamente `order/id`, valida su formato y rechaza DTD y entidades
XML. El SKU y la cantidad del archivo se ignoran; el detalle se obtiene mediante
el cliente oficial de órdenes de compra.

El detalle oficial genera un `Order` con canal `sftp`, cliente, plazo, estado
externo y un item con SKU, cantidad y precio oficiales. Se comprueba que el grupo
sea el proveedor. El modelo de ventas actual usa pesos enteros: un precio
fraccionario deja el evento fallido sin redondear importes. Si la OC ya existe,
se asocia el evento al pedido existente conservando sus datos y decisiones.
La actualización unificada de órdenes corresponde al paso 6.

La creación del pedido y la marca `processed` se confirman juntas. Los eventos
pendientes sobreviven a reinicios; los bloqueos de filas evitan procesarlos
simultáneamente. Los errores de conexión a la API, autenticación, 429 y 5xx se
reintentan desde PostgreSQL. Un 429 pausa el lote y respeta `Retry-After`; los
fallos de autenticación también pausan el lote. Un XML inválido, un proveedor
incorrecto o un rechazo HTTP definitivo quedan `failed` con su error y sin
reintento automático. Los logs identifican la ruta y la OC sin incluir secretos.

`--once` termina con código 1 si falla la conexión, la base o algún evento de esa
revisión. El modo periódico espera entre revisiones y continúa ante fallos
recuperables. La instalación como servicio permanente corresponde al paso 19.
Este proceso consulta OCs y registra pedidos; la aceptación/rechazo y el despacho
se desarrollan en los pasos 7 y 8. No utiliza el gateway LLM.

Pruebas de la integración, con `TEST_DATABASE_URL` configurada:

```bash
pytest tests/clients/test_sftp_orders.py tests/schemas/test_sftp_orders.py \
  tests/services/test_sftp_orders.py tests/scripts/test_poll_sftp_orders.py \
  tests/core/test_config.py -q
```

Usan un servidor sFTP local real, respuestas HTTP simuladas y PostgreSQL aislado.
Cubren duplicados, recuperación entre sesiones, archivos inválidos, errores de
API, límites de tamaño/lote, esperas y cierre de conexiones ante timeouts.

Validación real en dev, realizada el 20 de octubre de 2026: la casilla
`grupo3_desarrollo` permitió leer `/pedidos` usando `SFTP_PASSWORD`. Una revisión
importó 100 archivos como 100 eventos procesados y 100 OCs distintas en PostgreSQL
local de desarrollo, sin errores. Las 100 OCs históricas consultadas ya estaban
`vencida` en la API oficial. Al volver a leer esos mismos archivos, el registro
insertó cero eventos y cero pedidos adicionales. La trazabilidad previa de
Entrega 1 siguió reconstruyendo sus 109 lotes ancestros y 110 enlaces de producción.
La prueba no cambió estados externos ni llamó al gateway LLM.

## Entrega 2: recepción de pedidos por Pub/Sub

El paso 5 usa una suscripción pull. Primero descarga la llave del ambiente desde
`GET /ordenes-compra/pubsub/key`, guárdala fuera del repositorio y restringe sus
permisos:

```bash
mkdir -p keys
# Guardar la respuesta JSON como keys/grupo3-dev.json
chmod 600 keys/grupo3-dev.json
```

Luego configura la ruta en `.env`:

```dotenv
PUBSUB_CREDENTIALS_FILE=/ruta/absoluta/keys/grupo3-dev.json
PUBSUB_PROJECT_ID=taller-integracion-310700
PUBSUB_POLL_INTERVAL_SECONDS=5
PUBSUB_BATCH_SIZE=100
```

El tópico y la suscripción se derivan del grupo y de
`FARMA_CENTRAL_BASE_URL`. Para el grupo 3 en desarrollo son
`dev.proyecto.2026-2.grupo3` y `grupo3-dev`. Se pueden sobrescribir con
`PUBSUB_TOPIC_ID` y `PUBSUB_SUBSCRIPTION_ID`. El worker crea la suscripción si
no existe, por lo que la configuración es reproducible.

Para probar una revisión:

```bash
.venv/bin/python -m scripts.poll_pubsub_orders --once
```

Para mantener el polling mientras el proceso esté activo:

```bash
.venv/bin/python -m scripts.poll_pubsub_orders
```

Cada mensaje se guarda primero como `InboundEvent`, usando el `messageId` como
identidad única y conservando `message.data` en base64. Sólo después del commit
se envía el ack a Pub/Sub. El worker acepta como contenido un ID directo o un
JSON con `id` u `orderId`; siempre consulta el detalle oficial antes de crear
la OC local.

Si falla el ack, Pub/Sub puede volver a entregar el mensaje, pero la restricción
única evita otro evento. Si dos mensajes distintos contienen la misma OC,
`Order.external_order_id` evita crear dos pedidos. Los eventos ya persistidos se
pueden procesar incluso si Pub/Sub está temporalmente indisponible.

Pruebas del paso 5:

```bash
pytest tests/clients/test_pubsub_orders.py \
  tests/schemas/test_pubsub_orders.py \
  tests/services/test_pubsub_orders.py \
  tests/scripts/test_poll_pubsub_orders.py -q
```

La instalación del worker como servicio permanente se realizará junto con los
demás procesos autónomos de la Entrega 2.

## Entrega 2: pipeline unificado de órdenes

El paso 6 centraliza la lógica de sFTP y Pub/Sub en `OrderIngestionService`.
Ambos adaptadores extraen el `orderId`, pero el servicio común siempre consulta
el detalle oficial y crea o actualiza el pedido identificado por
`external_order_id`.

Cuando la misma OC vuelve a llegar, se mantienen un solo `Order` y un solo item,
y se actualizan cliente, SKU, cantidad, precio, plazo y estado externo. El canal
de la primera recepción queda en `Order.source`; cada canal adicional permanece
registrado en sus propios `InboundEvent`. El evento se enlaza al pedido y se
marca procesado en la misma transacción que persiste la OC.

Este pipeline sólo ingiere datos. La decisión de aceptar o rechazar una OC sigue
separada y corresponde al paso 7.

Pruebas del paso 6:

```bash
pytest tests/services/test_order_ingestion.py \
  tests/services/test_sftp_orders.py tests/services/test_pubsub_orders.py -q
```

## Entrega 2: decisión y procesamiento de órdenes

El paso 7 agrega `OrderProcessingService.process_order(order_id)`. La política
es intencionalmente conservadora: acepta una OC sólo si todavía está dentro de
plazo y la cantidad solicitada puede cubrirse con stock vendible vigente más
producción ya confirmada para antes del vencimiento. También descuenta las
cantidades aún sin asignar prometidas a otras OCs aceptadas. Las unidades ya
reservadas o entregadas no se descuentan otra vez; no supone producción futura que
aún no haya sido solicitada.

La decisión se envía al sistema oficial como `aceptada` o `rechazada` y se
guarda localmente en `status`, `external_status`, `decision_at` y
`decision_reason`. El bloqueo del producto evita prometer el mismo stock a dos
órdenes procesadas en paralelo. Reprocesar una orden decidida no vuelve a
enviar la transición.

Los adaptadores sFTP y Pub/Sub ejecutan este servicio después de la ingestión.
La OC, su decisión y el evento procesado se confirman en una misma transacción.

Para aplicar el cambio de base de datos y ejecutar las pruebas del paso:

```bash
alembic upgrade head
pytest tests/services/test_order_processing.py \
  tests/services/test_sftp_orders.py tests/services/test_pubsub_orders.py -q
```

## Entrega 2: cumplimiento y despacho de órdenes externas

El paso 8 reutiliza la reserva FEFO y la custodia de Entrega 1 para las OCs
externas aceptadas. Cada unidad se mueve al espacio de despacho y luego se
entrega con `POST /farma-central/dispatch`, indicando su ID y el
`external_order_id` de la OC. Las unidades entregadas parcialmente quedan
confirmadas una a una, por lo que un reintento continúa con las restantes.

Antes de cada entrega externa se persiste una intención de despacho por unidad.
Si se pierde la respuesta, el reintento compara el contador oficial de la OC
con esa intención y verifica la ausencia de la unidad en el espacio de despacho.
Sólo entonces registra la entrega sin repetirla. Si la evidencia es ambigua
(incluido un listado truncado o una unidad vencida), conserva la reserva y
reporta el conflicto; no supone que la entrega ocurrió. Todos los despachos de
la OC deben pasar por este servicio para mantener esa correlación.
Los despachos ambiguos anteriores a esta migración, sin intención persistida,
requieren revisión de sus unidades; el contador agregado no permite reconstruir
por sí solo sus entregas individuales.

Al recibir el estado oficial `vencida`, `anulada` o `rechazada`, se liberan las
reservas sin entregar y se conservan las entregas confirmadas. Las intenciones
sin resolver se reconcilian antes de liberar esas unidades. Una notificación
duplicada conserva el estado oficial actualizado y la auditoría original.

Aplicar la migración de intenciones antes de iniciar el backend o el worker:

```bash
.venv/bin/alembic upgrade head
```

El worker revisa primero las OCs con el vencimiento más cercano. Si la producción
aceptada todavía no está disponible, conserva la orden pendiente para una
revisión posterior. Al completar las unidades consulta nuevamente la OC oficial
y guarda su estado final.

Para ejecutar una revisión:

```bash
.venv/bin/python -m scripts.fulfill_external_orders --once
```

Para mantener el seguimiento periódico mientras el proceso esté activo:

```bash
.venv/bin/python -m scripts.fulfill_external_orders
```

El intervalo y el tamaño máximo de cada lote se configuran con
`EXTERNAL_ORDER_POLL_INTERVAL_SECONDS` y `EXTERNAL_ORDER_BATCH_SIZE`.

Pruebas del paso 8:

```bash
pytest tests/services/test_external_order_fulfillment.py \
  tests/services/test_external_order_regressions.py \
  tests/api/test_order_fulfillment.py tests/api/test_order_dispatch.py \
  tests/clients/test_farma_central.py -q
```

## Entrega 2: webhook de mensajería (paso 9)

`POST /api/messaging` recibe el mensaje de la plataforma y responde `204` después
de confirmar su persistencia en PostgreSQL. Conserva `messageId`, `sender`, `text`
y `receivedAt` en el `payload` de `InboundEvent`, con origen `messaging` y estado
`pending`. `InboundEvent.received_at` registra la recepción local; el `receivedAt`
original se conserva para calcular el plazo de respuesta en los siguientes pasos.
Se reutiliza la tabla existente, por lo que este paso no agrega una migración.

El webhook valida el Bearer OIDC de Google: firma, emisor, audiencia exacta,
vigencia, correo de la cuenta de servicio y `email_verified=true`. La cuenta
publicada en el [contrato de mensajería](https://dev.proyecto.2026-2.tallerdeintegracion.cl/messaging/docs)
es `mensajeria@taller-integracion-310700.iam.gserviceaccount.com`. No se usa la
llave de Pub/Sub ni se necesita una llave privada para verificar estos tokens.
Los certificados públicos se descargan con timeout y se mantienen en caché según
sus cabeceras HTTP. Los fallos de autenticación devuelven `401`; los fallos
temporales al verificar certificados o guardar en PostgreSQL devuelven `503`
para permitir el reintento.

La restricción única `(source, external_event_id)` deduplica por `messageId`,
también con entregas concurrentes. Un duplicado devuelve `204` sin sobrescribir
el mensaje original ni cambiar su procesamiento. No se llama al LLM, no se crea
una OC y no se responde a la API de mensajería durante este request: eso pertenece
a los pasos 10–13.

Configuración del endpoint público del grupo 3:

```dotenv
MESSAGING_OIDC_AUDIENCE=https://choripan3.ing.uc.cl/api/messaging
MESSAGING_OIDC_SERVICE_ACCOUNT=mensajeria@taller-integracion-310700.iam.gserviceaccount.com
OIDC_TIMEOUT_SECONDS=3
```

Si se omite la audiencia, se deriva de `APP_PUBLIC_URL` y `/api/messaging`.
Debe coincidir exactamente con la URL pública a la que envía la plataforma,
incluyendo esquema y path. No se deriva de `Host` ni de `X-Forwarded-Host`.
Reiniciar el backend después de cambiar la configuración. El acceso público y
el envío de un webhook real se verifican después de desplegar; los tests locales
usan tokens RSA de prueba y certificados simulados, sin consumir el LLM.

Instalar las nuevas dependencias y ejecutar los tests del paso:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q tests/core/test_oidc.py tests/core/test_config.py \
  tests/api/test_messaging.py tests/services/test_messaging.py
```

Las pruebas cubren firma y claims inválidos, payload incompleto, deduplicación,
fallos de persistencia, reintentos, recepción en menos de 15 segundos y
persistencia entre sesiones. No se registran tokens ni texto de mensajes en los
logs del webhook.

## Entrega 2: auditoría de mensajería (paso 10)

`InboundEvent` conserva el mensaje original en `payload` y reutiliza `status`
como estado de procesamiento. Para auditar los pasos posteriores también guarda
la salida del LLM, la interpretación validada (`sku`, cantidad y fecha), la
decisión `order`/`discarded`, su motivo, el ID externo de la OC y `responded_at`.
El `order_id` existente vincula el evento con la OC local cuando ésta ya fue
ingerida. Todos los campos nuevos son opcionales para no afectar eventos de
sFTP, Pub/Sub ni mensajes aún pendientes.

Aplicar la migración y ejecutar las pruebas del modelo:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m pytest -q tests/models/test_inbound_event.py
```

## Entrega 2: cliente del gateway LLM (paso 11)

`LLMGatewayClient` envía texto a `POST <LLM_GATEWAY_BASE_URL>/chat/completions`
con autenticación Bearer y solicita `response_format=json_schema` con
`strict=true`, según la [documentación de LiteLLM](https://docs.litellm.ai/docs/completion/json_mode).
Usa `httpx`, sin dependencia del SDK de un proveedor. Los modelos habilitados por
el curso son `gemini-flash`, `gemini-flash-lite` y `gpt-oss-20b`. Completar en
`.env.local` la URL y clave del grupo; por ejemplo, para usar `gemini-flash-lite`:

```dotenv
LLM_GATEWAY_BASE_URL=https://HOST_DEL_GATEWAY/v1/
LLM_GATEWAY_API_KEY=CLAVE_DEL_GRUPO
LLM_GATEWAY_MODEL=gemini-flash-lite
LLM_GATEWAY_TIMEOUT_SECONDS=20
LLM_GATEWAY_MAX_TOKENS=256
```

La URL debe incluir el prefijo de API publicado por el gateway (`/v1/` si
corresponde). Elegir un modelo con soporte para salida estructurada `json_schema`.
La API existente puede iniciar sin estas variables; instanciar el cliente sin
URL, clave o modelo produce `LLMGatewayError`. La clave usa
`SecretStr` y el cliente no registra texto, prompts ni respuestas en logs.

Todas las claves del grupo comparten **5 requests por minuto**, **1.000 tokens
por minuto entre entrada y salida** y **USD 2 cada 30 días**, según las reglas
publicadas del gateway. El máximo de 256 tokens limita sólo la salida; el texto,
catálogo, instrucciones y esquema también cuentan en el consumo de entrada.
Este valor es un punto de partida: hay que comprobar el consumo real en `usage`
con el modelo y catálogo elegidos. El cliente no envía `reasoning_effort` y no
reintenta automáticamente al agotar tasa, tokens o presupuesto. La programación
del worker deberá respetar el cupo compartido; este cliente no implementa un
limitador global entre procesos o claves del grupo. Mantener la clave fuera del
repositorio y, ante una filtración, solicitar su revocación al equipo docente.

`MessageInterpretationService.propose(message)` consulta los SKU y nombres actuales de
productos `KIT` en la base local sincronizada, incluidos los que no tienen stock.
Envía sólo esos datos, el texto y `receivedAt` convertido a `America/Santiago`.
No envía teléfono, nombre del remitente, precios, inventario ni credenciales de
Farma Central. Un catálogo vacío produce `LLMGatewayError` sin llamar al
gateway. Conviene ejecutar la sincronización de inventario antes de usarlo.

La respuesta contiene una `proposal` con `is_order`, `sku`, `quantity`,
`delivery_date`, `confidence`, `is_ambiguous` y `reason`. SKU, cantidad y fecha
pueden ser `null` para expresar información faltante. `reason` puede ser vacío
para pedidos claros. Se rechazan campos extra,
tipos incorrectos, cantidades no positivas, confianza fuera de `[0, 1]`, fechas
sin zona horaria, negativas del modelo y respuestas truncadas. La existencia del
SKU, la coherencia entre campos y la vigencia del plazo se revisan en la validación
determinista del paso 12; una propuesta con JSON válido todavía no es un pedido.

El resultado incluye `raw_output`, con la respuesta completa y su uso de tokens,
para que el procesamiento posterior pueda guardarlo en `InboundEvent.llm_output`.
`LLMGatewayInvalidResponseError.raw_output` conserva también la salida inválida.
La consulta `propose` no modifica el mensaje persistido ni crea órdenes.
El webhook mantiene su confirmación rápida; la llamada se integrará al worker
en el paso 13.

Se distinguen `LLMGatewayTimeoutError`, `LLMGatewayConnectionError`,
`LLMGatewayHTTPError` y `LLMGatewayBudgetExceededError`. Se reconocen los errores
de presupuesto de LiteLLM y `insufficient_quota`; un 429 común conserva
`retry_after_seconds` y no se confunde con presupuesto agotado. El cliente hace
un solo intento, con timeout total y por operación HTTP y un límite de tokens de
salida. El worker futuro deberá decidir el backoff o descarte sin crear una OC
a partir de un fallo del modelo.

Pruebas del contrato HTTP, respuestas válidas/ambiguas, validación estricta,
errores, presupuesto y catálogo sin efectos sobre órdenes:

```bash
.venv/bin/python -m pytest -q tests/clients/test_llm_gateway.py \
  tests/core/test_config.py tests/services/test_llm_proposals.py
```

La prueba del servicio requiere PostgreSQL con `TEST_DATABASE_URL` configurada
para la base de pruebas local. Todas las llamadas LLM están simuladas y no
consumen presupuesto.

## Entrega 2: interpretación validada (paso 12)

`MessageInterpretationService.interpret(event, now=...)` interpreta un
`InboundEvent` persistido de mensajería y guarda `decision=order` o `discarded`.
Reutiliza el cliente del paso 11 y los campos de auditoría del paso 10; no agrega
tablas, dependencias ni variables de configuración.

La política exige una propuesta de pedido nuevo, sin ambigüedad y con confianza
de al menos `0.8`. Verifica que el SKU siga siendo un `KIT` del catálogo local,
que no contradiga SKU explícitos del texto y que exista una cantidad única,
positiva e identificable. Admite cifras, números en español de uno a veinte,
`un par`, `media docena` y `una docena`, asociados a kits, unidades, un SKU o una
referencia como `dos del de la diabetes`. No transforma cantidades aproximadas
o alternativas en cantidades exactas. Consultas de estado, ayuda, cancelaciones
y negaciones explícitas se descartan.

El plazo debe ser posterior tanto a `receivedAt` del payload original como al
momento de validación. Se contrasta con fechas ISO, `en N horas/minutos`, o
`hoy`/`mañana` con hora explícita, usando `America/Santiago` y su cambio de hora.
Por ejemplo, `hoy antes de las 7 de la tarde` y `mañana a las 09:00` tienen
plazos verificables. Expresiones vagas como `mañana a primera hora`, horas sin
AM/PM ni formato de 24 horas, y fechas fuera de esta gramática limitada se
descartan; no se adivina una hora para completar el pedido.

En caso de timeout, presupuesto agotado, salida inválida o cualquier error
controlado del gateway, descarta con un motivo estable y sin reintentos ni
fallback que invente un pedido. Guarda la salida cruda o el tipo de fallo en
`llm_output`, la decisión y el motivo; sólo guarda SKU, cantidad y fecha en los
campos `interpreted_*` si pasan la validación. Conserva el payload original.
Una decisión ya guardada se reutiliza sin otra llamada al LLM.

El servicio hace `flush`; el llamador debe confirmar la transacción con
`session.commit()`. Mantiene el evento pendiente de respuesta: el envío a la
plataforma, `responded_at`, la creación de la OC y el worker corresponden al
paso 13. La disponibilidad de stock y la aceptación comercial no afectan esta
interpretación de lo que pidió el cliente.

```bash
.venv/bin/python -m pytest -q tests/services/test_message_interpretation.py \
  tests/services/test_llm_proposals.py tests/clients/test_llm_gateway.py
```

Las pruebas usan el LLM simulado y PostgreSQL de pruebas; no consumen el cupo del
gateway. Cubren el ejemplo del enunciado, cantidades, ambigüedad, no pedidos,
fechas relativas, cruce de medianoche en Chile, horario de invierno, errores y
auditoría persistente sin creación de órdenes.

## Entrega 2: respuesta de mensajería y creación de OC (paso 13)

`MessagingProcessingService` toma eventos de mensajería pendientes, ejecuta la
interpretación del paso 12 y responde una sola vez a
`POST /messaging/messages/{messageId}/response`. Para un descarte envía el motivo;
para un pedido envía SKU, cantidad y fecha de entrega, y guarda el `orderId`
devuelto por la plataforma.

La respuesta y su `orderId` se confirman en PostgreSQL antes de consultar la OC.
Así, si falla después el sistema de órdenes, el reintento continúa desde la
ingestión sin responder nuevamente el mensaje. La OC se incorpora con canal
`messaging` y pasa inmediatamente por la política común de aceptación o rechazo
del paso 7. El worker de cumplimiento del paso 8 se encarga posteriormente de
las OCs aceptadas.

Los timeouts, fallos de conexión, autenticación, `429` y errores de servidor se
reintentan. Los errores definitivos, incluido el vencimiento de la ventana de 30
minutos, quedan auditados sin reintento. Un `409` detiene nuevos envíos: un
descarte se considera resuelto; en un pedido se registra el fallo porque la API
no ofrece una consulta para recuperar el `orderId` de una respuesta anterior.

La URL de mensajería se deriva del ambiente de Farma Central y se puede
sobrescribir. El intervalo predeterminado procesa un mensaje cada 12 segundos
para respetar el límite compartido de cinco llamadas LLM por minuto:

```dotenv
# MESSAGING_BASE_URL=https://dev.proyecto.2026-2.tallerdeintegracion.cl/messaging/
MESSAGING_TIMEOUT_SECONDS=10
MESSAGING_POLL_INTERVAL_SECONDS=12
MESSAGING_BATCH_SIZE=1
```

Para ejecutar una revisión o mantener el proceso activo:

```bash
.venv/bin/python -m scripts.process_messaging --once
.venv/bin/python -m scripts.process_messaging
```

Se deben configurar previamente las variables del gateway LLM descritas en el
paso 11. La instalación de los workers como servicios permanentes sigue
reservada para el paso 19.

Pruebas del paso:

```bash
.venv/bin/python -m pytest -q tests/clients/test_messaging.py \
  tests/services/test_messaging_processing.py \
  tests/scripts/test_process_messaging.py \
  tests/services/test_message_interpretation.py
```

## Entrega 2: webhook de retiros de lote (paso 14)

`POST /api/recalls` recibe los avisos de retiro, verifica el Bearer OIDC de
Google y confirma con `204` sólo después de persistir el evento. La audiencia
predeterminada es la URL pública exacta terminada en `/api/recalls`; el
remitente esperado se configura de forma independiente para poder ajustarlo a
la cuenta publicada por la plataforma.

El payload exige `recallId`, lote, SKU, motivo, `issuedAt` y `deadline`, con
fechas que incluyan zona horaria y un plazo posterior a la emisión. Se conserva
completo en `InboundEvent`, con origen `recall` y estado `pending`. La restricción
única existente sobre fuente e ID hace idempotentes también las entregas
concurrentes: un duplicado responde `204` sin sobrescribir el aviso original.

No se consulta Farma Central ni se inicia cuarentena dentro del request. El
análisis de lotes afectados, las notificaciones, el reporte y el polling de
respaldo pertenecen a los pasos siguientes.

Configuración del servidor del grupo 3:

```dotenv
RECALL_OIDC_AUDIENCE=https://choripan3.ing.uc.cl/api/recalls
RECALL_OIDC_SERVICE_ACCOUNT=mensajeria@taller-integracion-310700.iam.gserviceaccount.com
```

Si se omite la audiencia, se deriva de `APP_PUBLIC_URL`. Los fallos de token
devuelven `401`, los fallos temporales al verificar certificados o persistir
devuelven `503`, y un payload inválido devuelve `422`.

Pruebas del paso:

```bash
.venv/bin/python -m pytest -q tests/api/test_recalls.py \
  tests/services/test_recalls.py tests/core/test_config.py \
  tests/core/test_oidc.py
```

## Entrega 2: modelo persistente de recalls (paso 15)

La investigación y las acciones de cada retiro se conservan en un modelo
separado del evento de entrada. `Recall` guarda el lote de origen, SKU, motivo,
plazo, estado y el último reporte enviado. Puede enlazarse con el
`InboundEvent` del webhook sin exigirlo, de modo que también admite recalls
recuperados por el mecanismo de respaldo.

Las tablas relacionadas registran los lotes afectados y su generación, las
unidades encontradas en stock y su ubicación original, las unidades ya
entregadas con su OC y cliente, y la notificación única de cada OC. Las claves
foráneas compuestas impiden asociar una unidad a un lote distinto del suyo o a
un lote que no fue incorporado en la investigación del mismo recall.

Este paso sólo incorpora persistencia e integridad. El recorrido del linaje,
la cuarentena, las llamadas externas y la API de consulta corresponden a los
pasos 16–18.

Aplicar la migración y ejecutar las pruebas del modelo:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m pytest -q tests/models/test_recalls.py
```

## Entrega 2: investigación recursiva del recall (paso 16)

`RecallInvestigationService(session).investigate(recall_id)` investiga un retiro
ya recibido, usando exclusivamente PostgreSQL. Si el webhook sólo dejó un
`InboundEvent`, crea y enlaza el `Recall` del paso 15 en la misma transacción.
También admite un `Recall` registrado por otro canal. El lote debe existir con
el `batch` externo exacto y coincidir con el SKU del aviso; no se seleccionan
otros lotes del mismo producto. Un lote todavía no sincronizado produce un error
controlado y puede investigarse después de sincronizar el inventario.

Una CTE recursiva sigue `ProductionInput` → `ProductionRun.output_lot_id`, con
consumo positivo y salida identificada. `UNION` evita repetir lotes en ciclos o
rutas compartidas. La consulta devuelve sólo el origen y sus descendientes:
excluye antecesores, ramas hermanas e ingredientes que sólo participaron en una
misma receta. El resultado incluye todas las aristas afectadas del grafo; para
el árbol persistente de `RecallLot` se elige un padre determinista y la distancia
mínima al origen, con generación cero para el lote retirado.

El objeto `RecallInvestigation` contiene:

- `lots` y `production_links`: lotes afectados, SKU, generación y linaje.
- `stock_units`: unidades físicas `available`, `reserved`, `quarantined` o
  `expired`, con identificador y ubicación actual y original. Incluye recepción,
  acondicionamiento, despacho, bodega externa y cuarentena, aunque no sean áreas
  vendibles. No se filtra por fecha de vencimiento para investigar un retiro.
- `orders`: OCs o ventas web con entregas afectadas, cliente y unidades concretas.
  La evidencia es `OrderUnit.dispatched_at`, incluso si la orden está parcialmente
  cumplida. Una reserva sin despacho no genera un cliente afectado. Las ventas
  web conservan su correo y un `external_order_id` nulo.
- `elapsed_ms`: tiempo total de consulta y persistencia.

Las unidades consumidas, despachadas o ausentes del inventario no se consideran
stock. Las relaciones de consumo y entrega prevalecen sobre un estado local
desactualizado. Las consultas se restringen a los IDs de lote afectados y el
nuevo índice `ix_units_lot_id` permite localizar sus unidades. El stock se bloquea
durante la investigación para que no se consuma o despache mientras se clasifica;
la espera por bloqueos está limitada a tres segundos y cada sentencia a quince.

Se guardan `RecallLot`, `RecallUnit`, `RecallOrder` y `RecallDelivery` sin duplicar
registros al reintentar. Las investigaciones de un mismo retiro se serializan.
Se conserva la primera ubicación identificada, los tiempos de cuarentena, las
notificaciones y el reporte anterior. Los snapshots históricos no se borran si
una unidad cambia de estado: **el objeto devuelto contiene el stock actual** y
debe usarse para decidir las siguientes acciones. Un snapshot con lotes ajenos
al linaje se rechaza en vez de incorporarlo al resultado.

El retiro queda en `investigating`; no se declara completado por descubrir los
afectados. Los movimientos, las notificaciones y el reporte pertenecen al paso
17; los endpoints para frontend pertenecen al paso 18. Esta investigación no
llama a Farma Central ni al LLM.

Aplicar la migración y ejecutar una investigación local:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.investigate_recall RECALL_ID
```

El script imprime el objeto en JSON. Si el retiro no existe, el origen no coincide
o hay un error de persistencia, termina con código distinto de cero y revierte
los cambios de esa investigación.

```bash
.venv/bin/python -m pytest -q tests/services/test_recall_investigation.py \
  tests/scripts/test_investigate_recall.py
.venv/bin/python -m pytest -q -s \
  tests/services/test_recall_investigation_performance.py
```

La prueba de volumen usa un esquema PostgreSQL aislado con **2.101 lotes,
20.303 unidades y 2.099 vínculos de producción**. Investiga 101 lotes afectados
en una cadena de 100 generaciones, 300 unidades en stock y tres entregas. Una
medición local obtuvo aproximadamente **60 ms y 19 sentencias SQL**; el test
verifica un máximo de 20 sentencias y cinco segundos en ese escenario. Los
tiempos dependen del equipo y de la carga de la base.

## Entrega 2: workers autónomos (paso 19)

Los procesos de sFTP, Pub/Sub, mensajería, cumplimiento de OCs, recalls y reposición se
ejecutan separados de FastAPI. Cada uno reutiliza el loop y el estado durable en
PostgreSQL de su integración; si una API externa falla, espera su intervalo antes
de volver a intentar y no bloquea los demás canales.

El worker de recalls consulta `GET /farma-central/recalls` como respaldo del
webhook. Persiste retiros nuevos sin duplicarlos y procesa eventos pendientes o
fallidos antes del deadline. Una respuesta interrumpida queda reintentable; un
retiro vencido queda fallido sin generar llamadas tardías.

Para ejecutar una revisión manual:

```bash
.venv/bin/python -m scripts.process_recalls --once
```

En el servidor, instalar una vez la plantilla y el target de systemd:

```bash
sudo cp deploy/farmacentral-backend.service /etc/systemd/system/
sudo cp deploy/farmacentral-inventory-sync.timer /etc/systemd/system/
sudo cp deploy/farmacentral-worker@.service /etc/systemd/system/
sudo cp deploy/farmacentral-workers.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now farmacentral-inventory-sync.timer
sudo systemctl enable --now farmacentral-workers.target
sudo systemctl restart farmacentral-backend.service
```

El target inicia seis servicios independientes. El backend los agrupa para que
un despliegue los reinicie sin ejecutar comandos privilegiados adicionales:

```bash
systemctl status farmacentral-workers.target --no-pager
systemctl list-units 'farmacentral-worker@*' --no-pager
sudo journalctl -u 'farmacentral-worker@*' -n 100 --no-pager
```

Antes de habilitarlos deben estar configuradas las credenciales sFTP, Pub/Sub y
LLM descritas en los pasos anteriores. Los intervalos de recalls se pueden
ajustar con `RECALL_POLL_INTERVAL_SECONDS` y `RECALL_BATCH_SIZE`.

### Reposición autónoma de kits

`scripts.replenish_inventory` cuenta stock actual disponible, sin asignaciones,
con batches oficiales, linaje completo y vida útil suficiente. La producción
histórica, el stock vencido, reservado, consumido y bloqueado por recalls no
cubren el mínimo. Reutiliza insumos y acondicionados aptos; solicita los faltantes
mediante abastecimiento oficial, fabrica según la receta y mueve cada kit
terminado al buffer. Recupera las producciones pendientes antes de iniciar otras.
No llama al LLM.

La configuración opcional de `.env` y `.env.local` es:

```dotenv
REPLENISHMENT_POLL_INTERVAL_SECONDS=60
REPLENISHMENT_MINIMUM_STOCK=2
REPLENISHMENT_TARGET_STOCK=6
REPLENISHMENT_MINIMUM_LIFETIME_SECONDS=14400
```

Son valores por kit: reponer bajo 2 unidades útiles hasta 6, con al menos cuatro
horas de vida restante. Ajustarlos al consumo, los plazos de las OCs y la capacidad
del buffer. El tamaño de lote puede redondear la cantidad producida hacia arriba.
El stock físico disponible también limita la reposición: si la vida útil real es
menor al umbral configurado, se registra una advertencia y se espera consumo o
vencimiento en lugar de comprar indefinidamente. Las unidades en cuarentena
permanecen excluidas y no se reutilizan.

Una conexión dedicada mantiene un bloqueo PostgreSQL durante todo el ciclo de
producción, incluso entre commits. El mismo bloqueo protege la producción manual
y el vaciado de acondicionamiento. Un segundo proceso espera al próximo ciclo;
un comando manual informa que el área está ocupada.

Para instalar el sexto worker en un servidor que ya tenía los cinco anteriores,
copiar nuevamente el target y recargar systemd:

```bash
sudo cp deploy/farmacentral-workers.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart farmacentral-backend.service
sudo systemctl start farmacentral-worker@replenish_inventory.service
systemctl status farmacentral-worker@replenish_inventory.service --no-pager
sudo journalctl -u farmacentral-worker@replenish_inventory.service -n 100 --no-pager
```

La ejecución manual `.venv/bin/python -m scripts.replenish_inventory --once`
**compra y fabrica realmente**; no forma parte de las pruebas automatizadas.
En local, ejecutar ese worker como otro proceso si se requiere reposición.

Pruebas específicas:

```bash
.venv/bin/python -m pytest -q tests/services/test_recall_worker.py \
  tests/scripts/test_process_recalls.py tests/clients/test_farma_central.py
```

### Recuperación y bloqueo de stock

La respuesta al recall guarda el estado `recall_pending` para todo el stock
afectado en la misma transacción que la investigación, antes de llamar a Farma
Central. Ese estado excluye venta, asignación, movimientos normales y consumo.
Tras confirmar el movimiento se guarda `quarantined` y su evento de custodia.
La sincronización conserva ambos estados y no restaura ubicaciones de una
lectura anterior; las unidades nuevas de lotes ya bloqueados también quedan
como `recall_pending`. Esto incluye descendientes enlazados después de completar
el retiro, aunque ya haya vencido. Antes del vencimiento, el worker detecta lotes,
stock o entregas nuevos y actualiza la investigación, la cuarentena y el reporte.
Sólo vuelve a notificar una OC ya notificada si cambió su conjunto de productos.
Un retiro sin cambios no genera reportes ni notificaciones repetidos. Después del
plazo se conserva el bloqueo local sin enviar reportes tardíos.

La API pública muestra `failed` si el evento falló antes de poder persistir la
investigación; no presenta ese caso como una recepción todavía pendiente.

Un despacho con intención persistida se reconcilia con el conteo oficial y el
inventario del área de despacho: se registra la entrega si ya ocurrió o se
libera la intención si se prueba que la unidad sigue allí, sin despacharla de
nuevo. Antes de notificar y reportar se actualiza la investigación para incluir
las entregas confirmadas durante el procesamiento. Las acciones externas se
detienen al vencer el deadline.

El worker de cumplimiento también revisa OCs locales `received`, actualiza su
estado oficial y aplica la política normal de aceptación/rechazo. Las aceptadas
continúan hacia asignación y despacho.

Si Mensajería creó una OC pero se perdió la respuesta, su API puede devolver
`409` sin el ID original y no ofrece un endpoint de consulta de mensajes.
La recuperación automática de ese ID no está disponible. Una vez identificado
el ID de la OC de forma independiente, ejecutar:

```bash
.venv/bin/python -m scripts.recover_messaging_order MESSAGE_ID ORDER_ID
```

El comando valida proveedor, cliente, SKU, cantidad y plazo contra la
interpretación persistida, vincula la OC y la acepta o rechaza con la política
normal. No vuelve a responder el mensaje, no crea otra OC y no llama al LLM.
Un ID incompatible se rechaza sin guardar cambios.

El frontend del repositorio `farmacentral-frontend-g3` incorpora `/recalls` y
`/recalls/:recallId` para consultar listado, estado y unidades con SKU e ID.

Estas verificaciones usan PostgreSQL con esquemas temporales y clientes externos
simulados. No habilitar `process_messaging` para ejecutar pruebas: el worker real
sí consume LLM. El tamaño máximo de salida y el intervalo de procesamiento no
equivalen a un presupuesto diario local; el cupo monetario sigue dependiendo del
gateway del grupo.

### Recalls inválidos e identidades de lote pendientes

El polling valida cada retiro por separado. Un elemento con `batch`, `sku` o
`reason` nulos incrementa `invalid`, registra los campos inválidos sin volcar su
contenido y no bloquea los retiros válidos ni los webhooks ya persistidos.
`scripts.process_recalls --once` devuelve un código de error si encontró elementos
inválidos, aunque haya completado los demás retiros.

Antes de desplegar esta recuperación, aplicar la migración
`8c62e71f4b09` con `.venv/bin/alembic upgrade head`. Agrega exclusivamente la
tabla de auditoría `lot_identity_repairs`; no cambia asociaciones históricas por
sí sola.

Cuando el inventario oficial vuelve a informar `batch` para IDs de unidades ya
conocidos, la sincronización recupera el lote provisional `unreported:` o
`production:`. Mantiene el UUID cuando puede renombrarlo y, cuando consolida
varios lotes provisionales, conserva las unidades, entregas y referencias de
consumo/producción. Registra por unidad el identificador previo y el oficial.
Rechaza evidencia incompleta, SKUs incompatibles, agrupaciones de varias
producciones y consolidaciones que reescribirían snapshots de recalls.

Para detectar identidades pendientes, sin escribir ni llamar a ninguna API:

```bash
.venv/bin/python -m scripts.audit_lot_identities
.venv/bin/python -m scripts.audit_lot_identities --sku API-LOSARTAN-50
```

El comando devuelve `1` cuando quedan unidades con lotes provisionales, y `0`
cuando no encuentra ninguna en el alcance consultado. Un resultado `0` no
demuestra por sí solo que exista el lote exacto de un recall: también deben
comprobarse sus datos y trazabilidad.

Si las unidades históricas ya no aparecen en Farma Central y no existe un
respaldo con su relación unidad/lote, su `batch` no se puede deducir del SKU,
vencimiento o del nombre del recall. Esos datos siguen pendientes de una fuente
oficial; no se crea un lote ficticio ni se asignan todas las unidades del SKU al
retiro. La respuesta del recall continúa fallando explícitamente en ese caso.

### Validar un flujo real de la entrega 2 sin consumir LLM

Para las pruebas automatizadas, levantar PostgreSQL local y ejecutar:

```bash
.venv/bin/python -m scripts.test_e2
```

El comando usa `TEST_DATABASE_URL`, o `DATABASE_URL` de la aplicación si no está
definida. Exige PostgreSQL local, ejecuta la suite con esquemas temporales y
bloquea conexiones externas de los sockets de Python. Los clientes externos,
incluido el LLM, se prueban con respuestas simuladas. Requiere las dependencias
de desarrollo (`pip install -e '.[dev]'`). No inicia los workers reales.

Ejecutar desde la raíz del backend y con la configuración de **desarrollo**.
`scripts.validate_e2_flow` utiliza `.env` y `.env.local` con la misma precedencia
que la aplicación. No necesita abrir el frontend. La ejecución inicial consulta
PostgreSQL y `/api/health`, sin cambiar datos ni llamar a servicios externos:

```bash
.venv/bin/python -m scripts.validate_e2_flow
```

La salida muestra las migraciones, las cantidades históricas producidas por SKU
(`inventory.completed_quantities`) y cuántas unidades históricas siguen sin lote
oficial. Los IDs de OC y recall faltantes quedan como `pending`.
Si falta una migración, aplicarla con `.venv/bin/alembic upgrade head`.

Para encontrar los IDs recientes sin copiar ejemplos ni consultar APIs:

```bash
.venv/bin/python -m scripts.validate_e2_flow --list-ids
```

Muestra hasta 20 OCs por canal y los últimos 20 avisos de mensajería y recalls,
con sus estados. No imprime el texto de los mensajes ni credenciales. Una OC
`rejected` permite comprobar la decisión, pero para demostrar cumplimiento se
necesita una `dispatched`. Un aviso del botón "Probar conexión" del portal no
reemplaza un recall simulado sobre un lote real.

Para comprobar un mensaje **ya recibido**, sin volver a llamar al gateway:

```bash
read -r -p "ID del mensaje recibido: " MESSAGE_ID
.venv/bin/python -m scripts.validate_e2_flow --message-id "$MESSAGE_ID"
```

Verifica la respuesta dentro de 30 minutos, la decisión y su auditoría. Si fue un
pedido, comprueba su interpretación persistida, la OC vinculada y la decisión en
el servicio oficial. No interpreta ni responde mensajes nuevos, no inicia el
worker de mensajería y no permite deducir que la interpretación del texto sea
correcta: esa comparación sigue siendo manual. Si el mensaje está pendiente,
lo informa; el script no hace avanzar el procesamiento.

Para revisar los cinco workers del paso 19, ejecutar **en el servidor**:

```bash
.venv/bin/python -m scripts.validate_e2_flow --check-workers
```

Consulta `systemctl show` sin sudo y exige servicios cargados, activos, en estado
`running` y con reinicio automático. No inicia ni reinicia servicios. El sexto
worker de reposición se comprueba sólo al agregar `--include-replenishment`.
Si systemd no está disponible en el equipo, la comprobación queda pendiente.

El checklist conjunto se ejecuta con IDs reales obtenidos de `--list-ids`:

```bash
read -r -p "ID de OC de sFTP: " OC_SFTP
read -r -p "ID de OC de Pub/Sub: " OC_PUBSUB
read -r -p "ID del mensaje recibido: " MESSAGE_ID
read -r -p "ID del recall real: " RECALL_ID
.venv/bin/python -m scripts.validate_e2_flow \
  --step19 \
  --order-id "$OC_SFTP" --order-id "$OC_PUBSUB" \
  --message-id "$MESSAGE_ID" --recall-id "$RECALL_ID" \
  --wait-seconds 180 \
  --output "/tmp/validacion-e2-$(date +%Y%m%d-%H%M%S).json"
```

`--step19` agrega una comprobación de cobertura: migraciones, salud, una OC
cumplida por sFTP y otra por Pub/Sub, un mensaje auditado, un recall válido,
listado/detalle públicos coherentes con la DB y los cinco workers. No exige que
las OCs pertenezcan al mismo recall ni que el recall tenga entregas o derivados.
Un rechazo oficial coherente pasa como decisión válida con `fulfilled: false`;
queda pendiente la evidencia de cumplimiento de ese canal. Una OC de mensajería
también puede consultarse con `--order-id` para verificar su despacho.

Este checklist no certifica toda la entrega: no valida una firma OIDC real,
el tiempo de ACK de un webhook real, la solución esperada del portal, el reinicio
conservando estado ni 36 horas de disponibilidad. Esas limitaciones se registran
en `not_verified`. Las pruebas automatizadas cubren los contratos y casos de
error con servicios simulados; la validación real cubre los IDs indicados.

Para preparar productos, elegir un kit del catálogo y una **meta acumulada**
mayor que su producción histórica. Por ejemplo, si ya se produjeron 30 unidades,
esta ejecución solicita las 2 restantes hasta llegar a 32, redondeadas al tamaño
de lote. Repetir la misma meta evita volver a solicitar kits ya completados:

```bash
.venv/bin/python -m scripts.validate_e2_flow \
  --prepare --sku KIT-ANALGESIA --target 32
```

`--prepare` sí compra insumos, consume materiales, fabrica y mueve unidades entre
acondicionamiento y buffer. Reutiliza stock con batches oficiales y trazabilidad
completa, solicita los insumos faltantes mediante abastecimiento normal y deja
los productos terminados en buffer. No usa el sandbox de creación de unidades.
Si otro proceso está fabricando, el bloqueo compartido impide iniciar esta ejecución.
La cantidad preparada debe alcanzar para las OCs y dejar unidades en stock para
comprobar también la cuarentena. Una respuesta fallida de abastecimiento puede
haber creado insumos: revisar inventario antes de repetir la preparación.

Con los workers ya activos, recibir una OC real por sFTP y otra por Pub/Sub.
Desde el portal del curso, generar un **nuevo** recall de desarrollo dentro de
su plazo. El script no puede originar esas OCs como proveedor ni generar recalls
mediante las APIs documentadas. El portal selecciona el lote; comprobar que el
retiro corresponda al material de este flujo. Un retiro histórico vencido no se
reactiva y los batches históricos desconocidos siguen necesitando evidencia
oficial. Reemplazar los tres IDs del comando:

```bash
.venv/bin/python -m scripts.validate_e2_flow \
  --order-id ID_OC_SFTP --order-id ID_OC_PUBSUB \
  --recall-id ID_RECALL_NUEVO --wait-seconds 180 \
  --output /tmp/validacion-e2.json
```

Se pueden observar las OCs antes de generar el retiro omitiendo `--recall-id`.
El script consulta cada 10 segundos, deja trabajar a los workers existentes y no
consume ni confirma mensajes de las colas. Comprueba cumplimiento oficial de
las OCs, plazo, trazabilidad y custodia; para el recall compara el reporte con
la investigación, las notificaciones y el stock observado en la cuarentena
oficial. Cada OC y recall se valida por separado: se puede ejecutar sólo
`--recall-id` o sólo `--order-id`. Un recall sin stock, sin entregas o sin derivados
puede ser válido; esas listas no se exigen artificialmente. Cuando los IDs
corresponden al mismo flujo, también se registra su vinculación y los canales
cubiertos. Un error reintentable permanece pendiente mientras el retiro tenga plazo.
También consulta `GET /api/recalls` y `GET /api/recalls/{recall_id}` para contrastar
el listado y los IDs/SKU del detalle con los datos persistidos. Un fallo de una
consulta se registra por separado; los errores transitorios pueden reintentarse
durante `--wait-seconds` sin ocultar las demás comprobaciones.
Las lecturas de cuarentena están limitadas a 200 unidades por SKU; si no puede
confirmar todas las unidades, deja la comprobación pendiente.

La evidencia JSON distingue `passed`, `pending`, `failed` y `skipped` para ámbitos
no solicitados o vinculaciones que no corresponden. El código de salida
es `0` para las comprobaciones solicitadas aprobadas, `1` ante fallas y `2` si
queda algo pendiente. Un `0` no certifica toda la entrega: aún se debe comparar
con la solución esperada del portal y validar operación continua. La consulta
de auditoría de mensajería **no llama al LLM ni inicia su worker**.
Los archivos de evidencia existentes no se sobrescriben; usar otra ruta en cada
ejecución. No es necesario subirlos a Git.

Pruebas aisladas del validador y del servicio de producción:

```bash
.venv/bin/python -m pytest -q tests/scripts/test_validate_e2_flow.py \
  tests/scripts/test_e2_checks.py tests/scripts/test_test_e2.py \
  tests/services/production/test_kits.py
```

## Entrega 2: idempotencia y reintentos (paso 20)

Se mantienen las claves únicas y los workers existentes. El procesamiento de un
mensaje o recall conserva un lock PostgreSQL entre commits; las decisiones de OC
se refrescan bajo lock y no se sobrescriben ítems con unidades asignadas. Una
respuesta interrumpida reconcilia cuarentenas pendientes antes de repetir movimientos.

Las notificaciones de recall guardan su intención antes de enviarse. Si se pierde
la confirmación y no se conoce el resultado, se bloquea el reenvío para evitar
duplicados. `scripts.resolve_recall_notification` registra evidencia para que el
worker continúe antes del deadline. No se agregan migraciones ni variables.

```bash
.venv/bin/python -m scripts.test_e2 --step20
```

La [guía del paso 20](docs/ENTREGA2_PASO20.md) documenta claves, escenarios de caída,
límites de las APIs y comandos de recuperación. Las pruebas usan PostgreSQL local
y servicios simulados; no llaman al LLM.
