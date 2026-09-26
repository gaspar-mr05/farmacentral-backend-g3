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

Cuando se agreguen modelos, crear una migración revisable con:

```bash
alembic revision --autogenerate -m "descripcion del cambio"
```

## Servidor de desarrollo

```bash
uvicorn app.main:app --reload
```

El estado de la aplicación queda disponible en `GET /health` y la documentación
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

## Estructura

- `app/api`: composición del router y endpoints HTTP.
- `app/core`: configuración transversal de la aplicación.
- `app/db`: base declarativa, engine y fábrica de sesiones de SQLAlchemy.
- `app/integrations`: futuros clientes y adaptadores de sistemas externos.
- `app/models`: futuros modelos persistentes de SQLAlchemy.
- `app/repositories`: futuras consultas y operaciones de persistencia.
- `app/schemas`: contratos de validación y serialización de la API.
- `app/services`: futuros servicios y casos de uso.
- `alembic`: entorno y futuras versiones de migraciones.
- `tests`: pruebas automáticas.

Las dependencias apuntan desde la capa HTTP hacia contratos y, cuando existan,
hacia servicios. El acceso a datos quedará encapsulado en repositorios. Por ahora
no se agregan interfaces ni clases sin una necesidad concreta.
