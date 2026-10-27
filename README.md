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
python -m scripts.test_e2
ruff check app scripts tests
ruff format --check app scripts tests
```

Detener PostgreSQL:

```bash
docker compose down
```

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
sudo cp deploy/farmacentral-worker@.service /etc/systemd/system/
sudo cp deploy/farmacentral-workers.target /etc/systemd/system/
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
