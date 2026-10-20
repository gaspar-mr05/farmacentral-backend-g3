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
el timer solo si ya estaba habilitado; no lo activan automáticamente en un
ambiente nuevo.

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

Las excepciones de `app.clients.purchase_orders_exceptions` distinguen
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
