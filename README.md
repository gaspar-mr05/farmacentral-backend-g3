# Farmacentral Backend

Esqueleto inicial del backend construido con Python 3.12, FastAPI, PostgreSQL,
SQLAlchemy 2 y Alembic. No contiene todavía lógica ni modelos del dominio.

## Requisitos

- Python 3.12
- Docker con Docker Compose (para PostgreSQL local)

## Configuración local

Crear y activar un entorno virtual:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Instalar la aplicación y las herramientas de desarrollo:

```bash
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Crear la configuración local a partir del ejemplo y reemplazar la contraseña de
ejemplo por una credencial local propia:

```bash
cp .env.example .env
```

`DATABASE_URL` es obligatoria. La aplicación muestra un error de validación al
iniciar si no está definida. Las variables `POSTGRES_*` son utilizadas solo por
Docker Compose y deben coincidir con la URL de conexión.

## PostgreSQL y migraciones

Levantar únicamente PostgreSQL:

```bash
docker compose up -d postgres
```

Aplicar las migraciones existentes:

```bash
alembic upgrade head
```

Sincronizar el catálogo, los espacios y el inventario real de Farma Central:

```bash
python -m scripts.sync_inventory
```

La sincronización es idempotente: actualiza los registros usando los identificadores
externos estables y marca como no disponibles las unidades previamente disponibles que
ya no aparecen en el inventario informado por Farma Central.

Cuando se agreguen modelos, crear una migración revisable con:

```bash
alembic revision --autogenerate -m "descripcion del cambio"
```

## Servidor de desarrollo

```bash
uvicorn app.main:app --reload
```

El estado de la aplicación queda disponible en `GET /api/health` y la documentación
interactiva en `/docs`.

## Verificaciones

Ejecutar los tests:

```bash
pytest
```

Revisar estilo y formato:

```bash
ruff check .
ruff format --check .
```

Para aplicar automáticamente el formato:

```bash
ruff format .
```

## Despliegue

Los archivos de `deploy/` preparan la aplicación para un servidor Ubuntu:

- `farmacentral-backend.service` ejecuta Uvicorn mediante systemd y lo reinicia
  ante fallos.
- `nginx.conf` publica la aplicación mediante un proxy inverso y deja Uvicorn
  accesible solo desde el servidor local.

Las credenciales y la configuración del ambiente desplegado deben guardarse en
`/opt/farmacentral-backend/.env`; ese archivo no se versiona.

Cada `push` a `main` ejecuta `.github/workflows/deploy.yml`. El workflow se
conecta al servidor con la clave guardada en el secreto `SERVER_SSH_KEY`,
actualiza el clon con `git pull --ff-only`, instala las dependencias, aplica las
migraciones, reinicia el servicio y comprueba `GET /api/health`.

## Estructura

- `app/api`: composición del router y endpoints HTTP.
- `app/core`: configuración transversal de la aplicación.
- `app/clients`: comunicación con sistemas externos como Farma Central.
- `app/db`: conexión y operaciones de persistencia con PostgreSQL.
- `app/models`: modelos persistentes de SQLAlchemy.
- `app/schemas`: contratos y estructuras de datos validadas.
- `app/services`: flujos de negocio, como la sincronización de inventario.
- `scripts`: comandos manuales de desarrollo y operación.
- `alembic`: entorno y futuras versiones de migraciones.
- `tests`: pruebas automáticas.
- `deploy`: configuración versionable de systemd y Nginx para el servidor.

Las dependencias apuntan desde la capa HTTP hacia contratos y, cuando existan,
hacia servicios. El acceso a datos quedará encapsulado en repositorios. Por ahora
no se agregan interfaces ni clases sin una necesidad concreta.
