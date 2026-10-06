# API de Farmacentral Backend

Esta guía describe los endpoints HTTP disponibles en el backend, sus entradas,
respuestas y principales errores.

## Convenciones generales

- URL local base: `http://127.0.0.1:8000`.
- Todos los endpoints de negocio usan el prefijo `/api`.
- Los cuerpos de entrada y salida usan JSON.
- Las fechas usan el formato ISO 8601, por ejemplo
  `2026-10-02T12:00:00Z`.
- Los UUID deben enviarse en su representación textual estándar.
- Actualmente la API propia no exige autenticación. Las credenciales de Farma
  Central y de checkout son utilizadas internamente por el backend y nunca
  deben enviarse desde el frontend.
- FastAPI devuelve `422 Unprocessable Entity` automáticamente cuando un path,
  query param o cuerpo no cumple el esquema esperado.

La documentación interactiva generada por FastAPI está disponible en:

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- Esquema OpenAPI: `GET /openapi.json`

## Resumen

| Método | Ruta | Propósito |
| --- | --- | --- |
| `GET` | `/api/health` | Comprobar que el backend está activo. |
| `GET` | `/api/catalog` | Consultar kits, precio vigente y stock vendible. |
| `GET` | `/api/products` | Listar el catálogo local completo. |
| `PATCH` | `/api/products/{product_id}` | Mover una unidad entre espacios. |
| `GET` | `/api/inventory/available` | Listar unidades locales disponibles. |
| `POST` | `/api/supply-requests` | Solicitar insumos a Farma Central. |
| `GET` | `/api/traceability/{lot_id}` | Consultar el linaje completo de un lote. |
| `POST` | `/api/orders` | Crear un pedido usando precio y stock vigentes. |
| `GET` | `/api/orders/{order_id}` | Consultar un pedido. |
| `POST` | `/api/orders/{order_id}/fulfillment` | Asignar unidades a un pedido pagado. |
| `POST` | `/api/orders/{order_id}/dispatch` | Despachar unidades asignadas y cerrar la venta. |
| `POST` | `/api/orders/{order_id}/payments` | Iniciar un pago para un pedido. |
| `GET` | `/api/payments/{payment_id}/return/{result}` | Confirmar el resultado de un pago. |

## Salud

### `GET /api/health`

Comprueba que el proceso de FastAPI está respondiendo. No consulta PostgreSQL ni
los servicios externos.

Respuesta `200 OK`:

```json
{
  "status": "ok"
}
```

Ejemplo:

```bash
curl http://127.0.0.1:8000/api/health
```

## Catálogo de venta

### `GET /api/catalog`

Devuelve todos los productos de categoría `kit`, incluyendo los que tienen
stock cero. Para cada kit combina:

- nombre y SKU guardados en PostgreSQL;
- precio vigente entregado por el servicio de precios;
- cantidad de unidades con estado `available`, no vencidas y ubicadas en un
  espacio marcado como vendible.

El precio no se almacena indefinidamente: se vuelve a consultar al procesar la
solicitud.

Respuesta `200 OK`:

```json
[
  {
    "sku": "KIT-RESP-ADULTO",
    "name": "Kit respiratorio adulto",
    "price": 9990,
    "stock": 12,
    "price_updated_at": "2026-10-02T12:00:00Z"
  }
]
```

Errores:

- `502 Bad Gateway`: la respuesta del servicio de precios es inválida o falta
  el precio de algún kit.
- `503 Service Unavailable`: el servicio de precios no responde, excede el
  tiempo máximo de espera o limita temporalmente las solicitudes. Cuando el
  proveedor informa cuánto esperar, la respuesta incluye `Retry-After`.

Ejemplo:

```bash
curl http://127.0.0.1:8000/api/catalog
```

## Productos e inventario

### `GET /api/products`

Lista todos los productos sincronizados en PostgreSQL. A diferencia de
`/api/catalog`, incluye insumos, productos acondicionados y kits, y no consulta
precios ni stock.

Valores posibles de `category`:

- `insumo`
- `acondicionado`
- `kit`

Respuesta `200 OK`:

```json
[
  {
    "sku": "API-AMOXI-500",
    "name": "API Amoxicilina 500 mg",
    "category": "insumo",
    "batch_size": 50,
    "requires_refrigeration": false
  }
]
```

### `PATCH /api/products/{product_id}`

Mueve una unidad concreta a otro espacio de Farma Central y registra el
movimiento en el modelo local de custodia.

`product_id` es el identificador externo de la unidad, no el SKU del producto
ni el UUID interno de PostgreSQL.

Cuerpo:

```json
{
  "store": "BODEGA-GENERAL"
}
```

`store` es el código del espacio de destino y no puede estar vacío.

Respuesta `200 OK`:

```json
{
  "product_id": "UNIT-12345",
  "from_store": "RECEPCION",
  "to_store": "BODEGA-GENERAL",
  "moved": true
}
```

Si la unidad ya se encuentra en el destino, no se solicita otro movimiento y
la respuesta contiene `"moved": false`.

Errores:

- `404 Not Found`: no existe la unidad o el espacio de destino.
- `409 Conflict`: la unidad no tiene estado `available`.
- `502 Bad Gateway`: Farma Central rechazó el movimiento o devolvió una
  respuesta inválida.
- `422 Unprocessable Entity`: `store` está vacío o el cuerpo es inválido.

Ejemplo:

```bash
curl --request PATCH \
  http://127.0.0.1:8000/api/products/UNIT-12345 \
  --header 'Content-Type: application/json' \
  --data '{"store":"BODEGA-GENERAL"}'
```

### `GET /api/inventory/available`

Lista las unidades locales cuyo estado es `available`. Puede filtrar por SKU,
código de ubicación o ambos. Este endpoint refleja el inventario operativo; el
stock apto para venta debe consultarse en `/api/catalog`, donde además se
validan vencimiento y ubicación vendible.

Query params opcionales:

| Parámetro | Tipo | Descripción |
| --- | --- | --- |
| `sku` | string | SKU exacto del producto. |
| `location_code` | string | Código exacto del espacio actual. |

Los filtros, cuando se envían, no pueden estar vacíos.

Respuesta `200 OK`:

```json
[
  {
    "unit": {
      "external_unit_id": "UNIT-12345",
      "status": "available"
    },
    "product": {
      "sku": "KIT-RESP-ADULTO",
      "name": "Kit respiratorio adulto",
      "category": "kit",
      "batch_size": 1,
      "requires_refrigeration": false
    },
    "lot": {
      "external_lot_id": "LOT-123",
      "expires_at": "2027-01-15T00:00:00Z"
    },
    "location": {
      "code": "BODEGA-GENERAL",
      "name": "Bodega general",
      "is_refrigerated": false
    }
  }
]
```

Ejemplos:

```bash
curl 'http://127.0.0.1:8000/api/inventory/available'
curl 'http://127.0.0.1:8000/api/inventory/available?sku=KIT-RESP-ADULTO'
curl 'http://127.0.0.1:8000/api/inventory/available?location_code=BODEGA-GENERAL'
```

## Abastecimiento

### `POST /api/supply-requests`

Solicita insumos a Farma Central. El backend valida el producto, solicita el
desafío de fabricación, resuelve el Proof of Work y envía la solicitud final.

Cuerpo:

```json
{
  "sku": "API-AMOXI-500",
  "quantity": 50
}
```

Reglas:

- `sku` debe existir localmente y corresponder a un `insumo`.
- `quantity` debe estar entre 1 y 5000.
- La cantidad debe ser múltiplo del `batch_size` del producto.

Respuesta `201 Created`:

```json
{
  "sku": "API-AMOXI-500",
  "quantity": 50,
  "available_at": "2026-10-02T15:30:00Z"
}
```

`available_at` indica cuándo debería estar disponible el abastecimiento; no
significa que las unidades ya hayan sido sincronizadas en PostgreSQL.

Errores:

- `404 Not Found`: el SKU no existe localmente.
- `422 Unprocessable Entity`: no es un insumo, la cantidad no respeta el tamaño
  de lote o el cuerpo es inválido.
- `409 Conflict`: expiró el desafío o Farma Central informó un conflicto.
- `502 Bad Gateway`: Farma Central rechazó la solicitud o respondió con datos
  inválidos.
- `503 Service Unavailable`: Farma Central no está disponible.

Ejemplo:

```bash
curl --request POST \
  http://127.0.0.1:8000/api/supply-requests \
  --header 'Content-Type: application/json' \
  --data '{"sku":"API-AMOXI-500","quantity":50}'
```

## Trazabilidad

### `GET /api/traceability/{lot_id}`

Obtiene la trazabilidad recursiva de un lote. `lot_id` puede ser el UUID interno
de PostgreSQL o el identificador externo informado por Farma Central.

La respuesta incluye:

- información del lote consultado;
- unidades actuales, sus estados, vencimientos efectivos y ubicaciones;
- lotes ancestros consumidos para producirlo;
- lotes descendientes producidos a partir de él;
- enlaces de producción y las unidades específicas consumidas.

Valores posibles de `origin`:

- `own_production`
- `farma_central`
- `other_distributor`

Respuesta `200 OK` abreviada:

```json
{
  "lot": {
    "id": "5b62dc1c-87fd-474d-9252-364e15eab51f",
    "external_lot_id": "LOT-KIT-001",
    "product_sku": "KIT-RESP-ADULTO",
    "product_name": "Kit respiratorio adulto",
    "origin": "own_production",
    "expires_at": "2027-01-15T00:00:00Z",
    "quantity": 1,
    "requires_refrigeration": false
  },
  "current_units": [
    {
      "external_unit_id": "UNIT-KIT-001",
      "status": "available",
      "effective_expires_at": "2027-01-15T00:00:00Z",
      "location": {
        "code": "BODEGA-GENERAL",
        "name": "Bodega general"
      }
    }
  ],
  "ancestors": [],
  "descendants": [],
  "production_links": []
}
```

Cada elemento de `production_links` tiene esta forma:

```json
{
  "production_run_id": "2a61a149-bb29-43bf-a5e6-00529bdd904c",
  "input_lot_id": "d34501f6-9741-446a-8cbb-11d927d820d5",
  "output_lot_id": "5b62dc1c-87fd-474d-9252-364e15eab51f",
  "quantity_consumed": 3,
  "consumed_unit_ids": ["UNIT-INPUT-1", "UNIT-INPUT-2", "UNIT-INPUT-3"],
  "requested_at": "2026-10-02T14:00:00Z",
  "completed_at": "2026-10-02T14:05:00Z"
}
```

Errores:

- `404 Not Found`: no existe el lote.

## Pedidos

### `POST /api/orders`

Crea una intención de compra independiente del pago. Antes de guardar el
pedido, el backend vuelve a consultar el catálogo, valida stock y captura el
precio vigente de cada producto.

Cuerpo:

```json
{
  "buyer_name": "Ada Lovelace",
  "buyer_email": "ada@example.com",
  "source": "web",
  "items": [
    {
      "sku": "KIT-RESP-ADULTO",
      "quantity": 2
    }
  ]
}
```

Reglas:

- Debe existir al menos un item.
- Cada cantidad debe ser mayor o igual a 1.
- Un mismo SKU no puede repetirse en el pedido.
- Actualmente `source` solo admite `web` y puede omitirse porque ese es su
  valor por defecto.
- Solo se pueden comprar kits presentes en `/api/catalog`.
- El total y los precios unitarios siempre los calcula el backend. El frontend
  no debe enviarlos.
- Crear el pedido no marca ni reserva unidades como vendidas.

Respuesta `201 Created`:

```json
{
  "id": "03de4a0c-b143-4481-9377-3076cbdb2a8b",
  "buyer_name": "Ada Lovelace",
  "buyer_email": "ada@example.com",
  "source": "web",
  "status": "pending_payment",
  "total": 19980,
  "items": [
    {
      "sku": "KIT-RESP-ADULTO",
      "quantity": 2,
      "unit_price": 9990,
      "line_total": 19980,
      "assigned_units": []
    }
  ],
  "created_at": "2026-10-02T15:00:00Z"
}
```

Estados posibles del pedido:

- `pending_payment`: esperando confirmación de pago.
- `paid`: pago confirmado exitosamente.
- `cancelled`: pago cancelado por el usuario.
- `payment_error`: pago con error o sesión expirada.

Errores:

- `404 Not Found`: el SKU no está disponible para venta.
- `409 Conflict`: stock insuficiente.
- `502 Bad Gateway`: no fue posible obtener precios vigentes.
- `422 Unprocessable Entity`: comprador, items o cantidades inválidas.

### `GET /api/orders/{order_id}`

Consulta un pedido persistido. La respuesta tiene la misma estructura que la
creación del pedido.

Errores:

- `404 Not Found`: no existe el pedido.
- `422 Unprocessable Entity`: `order_id` no es un UUID válido.

Ejemplo:

```bash
curl http://127.0.0.1:8000/api/orders/03de4a0c-b143-4481-9377-3076cbdb2a8b
```

### `POST /api/orders/{order_id}/fulfillment`

Asigna unidades físicas a los items de un pedido pagado. La selección usa FEFO:
prioriza las unidades con vencimiento efectivo más cercano, siempre que estén
vigentes, disponibles y en una ubicación vendible. Cada unidad queda con estado
`reserved` para prepararla para despacho.

La operación es transaccional e idempotente. Bloquea el pedido y las unidades
candidatas durante la selección, y la base de datos impide que una misma unidad
sea asignada a más de un pedido. Repetir la solicitud conserva la asignación
original.

Respuesta `200 OK`: usa el mismo formato de `GET /api/orders/{order_id}`. Cada
item incluye sus unidades concretas con esta estructura:

```json
{
  "sku": "KIT-RESP-ADULTO",
  "quantity": 2,
  "unit_price": 9990,
  "line_total": 19980,
  "assigned_units": [
    {
      "unit_id": "2423037a-e1fe-4acd-873d-ebf83c09879e",
      "external_unit_id": "UNIT-12345",
      "lot_id": "55d4c013-f4a0-4879-b77d-803630080c6e",
      "assigned_at": "2026-10-02T15:03:00Z"
    }
  ]
}
```

Errores:

- `404 Not Found`: no existe el pedido.
- `409 Conflict`: el pedido no está pagado, no hay stock suficiente o la
  asignación persistida es inconsistente.
- `422 Unprocessable Entity`: `order_id` no es un UUID válido.

## Pagos

### `POST /api/orders/{order_id}/payments`

Inicializa una sesión en la pasarela de pago para el total calculado del pedido.
El backend se autentica ante checkout, registra el pago local y devuelve la URL
a la que debe redirigirse el navegador.

No requiere cuerpo.

Respuesta `201 Created`:

```json
{
  "id": "92968112-ce1d-4553-b396-f4bca0db6917",
  "order_id": "03de4a0c-b143-4481-9377-3076cbdb2a8b",
  "external_transaction_id": "68abc123def456789012",
  "amount": 19980,
  "status": "pending",
  "payment_url": "https://dev.proyecto.2026-2.tallerdeintegracion.cl/checkout/..."
}
```

El frontend debe redirigir al usuario a `payment_url`. Una sesión pendiente
impide crear otra sesión simultánea para el mismo pedido. Después de una
cancelación, error o expiración se puede iniciar un nuevo intento.

Errores:

- `404 Not Found`: no existe el pedido.
- `409 Conflict`: el pedido ya está pagado o ya tiene un pago pendiente.
- `502 Bad Gateway`: checkout rechazó la inicialización o devolvió datos
  inválidos.
- `503 Service Unavailable`: checkout no está disponible.

Ejemplo:

```bash
curl --request POST \
  http://127.0.0.1:8000/api/orders/03de4a0c-b143-4481-9377-3076cbdb2a8b/payments
```

### `GET /api/payments/{payment_id}/return/{result}`

Es la URL de retorno utilizada por checkout después de que el usuario paga,
cancela o encuentra un error. Normalmente el navegador llega a este endpoint
por redirección de la pasarela; no es necesario que el frontend lo invoque
antes.

Path params:

| Parámetro | Descripción |
| --- | --- |
| `payment_id` | UUID local retornado al iniciar el pago. |
| `result` | Uno de `success`, `error` o `cancelled`. |

El valor de `result` no se considera una confirmación confiable. El backend
consulta el pago directamente en checkout y valida su estado, monto y grupo
antes de actualizar PostgreSQL.

Respuesta `200 OK`:

```json
{
  "id": "92968112-ce1d-4553-b396-f4bca0db6917",
  "order_id": "03de4a0c-b143-4481-9377-3076cbdb2a8b",
  "external_transaction_id": "68abc123def456789012",
  "amount": 19980,
  "status": "success",
  "created_at": "2026-10-02T15:02:00Z"
}
```

Si `FRONTEND_PUBLIC_URL` está configurada, este endpoint responde con una
redirección `303` a `/payment-result` del frontend e incluye `payment_id`,
`order_id` y `status` como parámetros de consulta.

Estados posibles del pago:

- `pending`: todavía no existe un resultado final.
- `success`: pago exitoso; el backend asigna unidades FEFO, las despacha y el
  pedido pasa a `dispatched`. Si Farma Central falla durante el despacho, el
  pedido permanece `paid` y repetir el retorno retoma la operación pendiente.
- `cancelled`: cancelado por el usuario; el pedido pasa a `cancelled`.
- `error`: checkout informó un error; el pedido pasa a `payment_error`.
- `obsolete`: la sesión expiró; el pedido pasa a `payment_error`.

La confirmación es idempotente. Si el pago ya tiene un estado final, repetir el
retorno conserva ese resultado y no vuelve a completar el pedido.

Errores:

- `404 Not Found`: no existe el pago local.
- `422 Unprocessable Entity`: UUID o `result` inválido.
- `502 Bad Gateway`: checkout devolvió un estado, monto o grupo inválido.
- `503 Service Unavailable`: no fue posible consultar checkout.

## Formato de errores

Los errores controlados y de validación siguen el formato estándar de FastAPI.
Un error de negocio normalmente contiene:

```json
{
  "detail": "Descripción del error"
}
```

Los errores de validación `422` contienen una lista en `detail` indicando la
ubicación y causa de cada dato inválido.


### `POST /api/orders/{order_id}/dispatch`

Despacha las unidades concretas asignadas mediante `fulfillment`. El pedido debe
estar pagado, tener todas sus unidades asignadas y conservar las unidades
pendientes en estado `reserved`, sin vencer. Antes del primer despacho, ejecutar
las migraciones y `python -m scripts.sync_inventory` para tener las ubicaciones
locales. El servicio consulta `checkOut` directamente en Farma Central y requiere
un único sector de despacho que exista en la base local.

```bash
curl --fail -X POST http://127.0.0.1:8000/api/orders/ORDER_ID/dispatch
```

Devuelve `200 OK` con `OrderResponse`. Al completar todas las entregas, `status`
es `dispatched`; cada elemento de `items[].assigned_units` incluye `dispatched_at`
(fecha UTC, o `null` mientras no se despache).

El servicio comprueba si cada unidad ya está en despacho antes de moverla y
confirma su ID externo en el inventario del destino después del movimiento.
Registra el movimiento, el evento de custodia `DISPATCHED` asociado al pedido,
el estado de la unidad y su fecha en una transacción local por unidad. El estado
final del pedido se guarda junto con la última entrega. Las solicitudes concurrentes
se serializan mediante bloqueos del pedido y las unidades.

La API externa y PostgreSQL no comparten una transacción: si falla una unidad,
las entregas anteriores permanecen registradas y el pedido sigue `paid`.
Repetir el endpoint retoma las pendientes; una respuesta perdida después del
movimiento se reconcilia consultando el destino antes de volver a mover.
Repetir un pedido completo devuelve su estado sin nuevos movimientos ni eventos.

Errores: `404` para un pedido inexistente, `409` para estado/asignaciones/unidades
o ubicación de despacho inválidos, `503` para conexión o timeout de Farma Central
y `502` para rechazo externo o confirmación inválida.

`GET /api/traceability/{lot_id}` incluye además `deliveries` para las unidades
entregadas del lote consultado y sus descendientes. Cada registro representa una
unidad (`quantity: 1`) e incluye `lot_id`, `order_id`, `buyer_name`, `buyer_email`,
`sku`, `external_unit_id` y `dispatched_at`. Las entregas parciales confirmadas
aparecen incluso si quedan otras unidades pendientes del pedido. Las unidades
despachadas conservan su historial y no vuelven al stock disponible al sincronizar.
