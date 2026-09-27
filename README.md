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

## Verificaciones de desarrollo

```bash
pytest
ruff check app scripts tests
ruff format --check app scripts tests
```
