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
- Los webhooks `/api/messaging` y `/api/recalls` exigen un Bearer OIDC de la
  plataforma. `/api/operations/*` exige una clave Bearer privada independiente;
  los demás endpoints actuales no exigen autenticación. Las
  credenciales de Farma Central y de checkout se utilizan internamente y no
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
| `GET` | `/api/health/ready` | Comprobar API y disponibilidad de PostgreSQL. |
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
| `POST` | `/api/messaging` | Recibir y persistir mensajes autenticados, sin duplicados. |
| `POST` | `/api/recalls` | Recibir y persistir retiros autenticados, sin duplicados. |
| `GET` | `/api/recalls` | Listar los retiros y su estado de procesamiento. |
| `GET` | `/api/recalls/{recall_id}` | Consultar un retiro y las unidades de stock identificadas. |
| `GET` | `/api/operations/waiting-orders` | Diagnóstico privado de OC pendientes de decisión. |
| `GET` | `/api/operations/offers` | Ofertas locales y estado de conciliación. |
| `GET` | `/api/operations/invoices` | Facturas persistidas del período. |
| `GET` | `/api/operations/metrics` | Último snapshot de métricas del período. |

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

### `GET /api/health/ready`

Abre una conexión independiente de solo lectura y ejecuta `SELECT 1` en
PostgreSQL. Limita la conexión a tres segundos y la consulta a dos segundos;
no espera el pool usado por las peticiones de negocio. No consulta sFTP,
Pub/Sub, precios, Farma Central ni LLM.

Devuelve `200` con `{"status":"ok","database":"ok"}` cuando responde PostgreSQL,
o `503` con `{"status":"unavailable","database":"unavailable"}` si falla. No
expone credenciales ni el error interno y envía `Cache-Control: no-store`.
`/api/health` sigue respondiendo incluso si PostgreSQL está caído. Esta prueba
no verifica migraciones, capacidad de escritura ni la salud de los workers.

```bash
curl --fail --max-time 6 http://127.0.0.1:8000/api/health/ready
```

## Catálogo de venta

### `GET /api/catalog`

Devuelve todos los productos de categoría `kit`, incluyendo los que tienen
stock cero. Para cada kit combina:

- nombre y SKU guardados en PostgreSQL;
- precio vigente entregado por el servicio de precios;
- cantidad de unidades con estado `available`, no vencidas y ubicadas en un
  espacio marcado como vendible;
- lotes que componen ese stock y el vencimiento efectivo más próximo de cada
  lote.

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
    "next_expiry_at": "2026-10-08T01:02:46Z",
    "lots": [
      {
        "external_lot_id": "L-KIT-RESP-ADULTO-261006-29e4",
        "stock": 12,
        "next_expiry_at": "2026-10-08T01:02:46Z"
      }
    ],
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

`market_transfers` conserva compras y ventas de los lotes consultado, ancestros
y descendientes. Cada entrada identifica `trade_id`, `external_offer_id`,
`external_order_id`, `role` (`buyer/seller`), `counterparty_id`, `lot_id`, `sku`,
`external_unit_id` y `occurred_at`. Así, al consultar un kit también se observa
la compra de mercado del insumo usado para producirlo. `deliveries` mantiene
únicamente las entregas del lote consultado y sus descendientes; las ventas
de lotes ancestros no se presentan como entregas del kit consultado.
La consulta usa PostgreSQL y no llama a SOAP, mercado ni gateway.
`current_units` incluye también las unidades físicas bloqueadas por retiro,
en cuarentena o vencidas; su presencia en trazabilidad no significa que estén
disponibles para venta. Las unidades consumidas o despachadas siguen figurando
en su historial y no se incluyen como stock actual.

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
  "external_order_id": null,
  "external_customer_id": null,
  "delivery_deadline": null,
  "external_status": null,
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
- `dispatched`: unidades despachadas.
- `received`: OC externa recibida, pendiente de decisión.
- `accepted`: OC externa aceptada localmente.
- `rejected`: OC externa rechazada localmente.
- `expired`: OC externa vencida.

Errores:

- `404 Not Found`: el SKU no está disponible para venta.
- `409 Conflict`: stock insuficiente.
- `502 Bad Gateway`: no fue posible obtener precios vigentes.
- `422 Unprocessable Entity`: comprador, items o cantidades inválidas.

### `GET /api/orders/{order_id}`

Consulta un pedido persistido. La respuesta tiene la misma estructura que la
creación del pedido.

El modelo admite además pedidos de `sftp`, `pubsub` y `messaging`. Su identidad
oficial se guarda en `external_order_id`, el cliente en `external_customer_id`
y el plazo en `delivery_deadline`. `external_status` puede ser `creada`,
`aceptada`, `rechazada`, `anulada`, `vencida` o `cumplida`, independientemente del
estado local `status`. Estos cuatro campos pueden ser NULL para pedidos web;
`buyer_email` puede ser NULL para pedidos externos. La creación mediante
`POST /api/orders` sigue reservada al canal web.

Para una OC procesada, `decision_at` indica cuándo se aceptó o rechazó y
`decision_reason` registra el stock, la producción confirmada y los compromisos
considerados. Ambos campos son NULL mientras no exista una decisión.

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

### Precisión monetaria de órdenes (Entrega 3)

Los importes de órdenes (`total`, `items[].unit_price` e `items[].line_total`)
conservan su formato JSON entero cuando no tienen fracción. Los nuevos importes
fraccionarios de OC externas se devuelven como strings decimales exactos, por
ejemplo `"1.123456789"`, para no perder precisión mediante float. Esto no cambia
el catálogo ni el formato de los importes enteros existentes del portal.

La fuente `market` identifica ventas propias del mercado. Las compras del grupo
se registran como operaciones comerciales, no como órdenes locales de venta.

El checkout mantiene importes enteros: iniciar el pago de una orden con total
fraccionario devuelve `409` antes de comunicarse con el servicio de pagos.

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

Para OCs externas aceptadas se usa `external_order_id` al entregar cada unidad.
Se guarda una intención antes de llamar a `/farma-central/dispatch`. Si se pierde
la respuesta, se consulta el avance oficial y se comprueba la ausencia de esa
unidad en despacho antes de registrar la entrega localmente. Una discrepancia
devuelve `409` y conserva la intención para revisión, sin duplicar el despacho.
La imposibilidad de consultar el estado oficial devuelve `503`.

Errores: `404` para un pedido inexistente, `409` para estado/asignaciones/unidades
o ubicación de despacho inválidos, `503` para conexión o timeout de Farma Central
y `502` para rechazo externo o confirmación inválida.

`GET /api/traceability/{lot_id}` incluye además `deliveries` para las unidades
entregadas del lote consultado y sus descendientes. Cada registro representa una
unidad (`quantity: 1`) e incluye `lot_id`, `order_id`, `buyer_name`, `buyer_email`,
`sku`, `external_unit_id` y `dispatched_at`. Las entregas parciales confirmadas
aparecen incluso si quedan otras unidades pendientes del pedido. Las unidades
despachadas conservan su historial y no vuelven al stock disponible al sincronizar.
`buyer_email` puede ser NULL para entregas de pedidos externos sin correo.

## Webhook de mensajería

### `POST /api/messaging`

Exclusivo para la plataforma de mensajería. Requiere
`Authorization: Bearer <token OIDC de Google>` con audiencia igual a
`MESSAGING_OIDC_AUDIENCE` (o `APP_PUBLIC_URL` + `/api/messaging`) y el correo de
`MESSAGING_OIDC_SERVICE_ACCOUNT` verificado. La firma, el emisor y la vigencia
se verifican usando `google-auth`; la audiencia no depende de los headers de
la solicitud.

```json
{
  "messageId": "msg_01JB7Q2F4R8S",
  "sender": {
    "name": "Farmacia Los Aromos",
    "phone": "+56 9 5555 0123"
  },
  "text": "Necesito dos kits respiratorios para hoy antes de las 7.",
  "receivedAt": "2026-10-27T16:05:00Z"
}
```

Todos los campos del ejemplo son obligatorios. Las cadenas deben contener texto
y `receivedAt` debe ser una fecha ISO 8601 con zona horaria.

Devuelve `204 No Content` después de confirmar el mensaje en PostgreSQL como
evento `messaging`, pendiente de interpretación. Un duplicado por `messageId`
devuelve también `204`, conservando el payload original y su estado. No se llama
al LLM ni se crea una OC en esta operación.

El worker posterior distingue pedidos, consultas y mensajes descartables.
Tanto los IDs explícitos como las referencias naturales deben identificar una OC
anterior del mismo teléfono, grupo, ambiente y período. Sólo entonces se consulta
el estado oficial y se verifica el proveedor. Se guarda el snapshot usado; una
referencia ajena o ambigua no permite revelar el estado de una OC.

El worker confirma la clasificación `inquiry` con la OC resuelta y luego envía
el texto de E3. `send_reply` deriva `to` y `replyTo` del mensaje original. Guarda
previamente el texto y las fases en `resolution_evidence.delivery`; tras un
reinicio retoma el envío pendiente sin reinterpretar ni clasificar otra vez.
Una referencia sin resolución segura se clasifica como `discarded` y recibe
una aclaración. Ambas confirmaciones deben llegar antes de `receivedAt + 30
minutos`; sólo entonces se registra `responded_at`.

Cada fase guarda intentos, fechas, latencia y errores. Si el POST tiene resultado
incierto, queda para revisión sin repetirse automáticamente. La cuota del LLM
se coordina entre procesos que comparten su archivo de reserva y no detiene
las respuestas ya interpretadas. La documentación pública consultada el 12 de
noviembre aún no incluye el contrato remoto nuevo; ver la captura, configuración
y límites de la validación en el README.

Las respuestas de prueba con `test=true` y sin `orderId` se registran sin intentar
crear o importar una OC. Un `409` por sí solo no confirma una respuesta anterior
ni la entrega de un texto y queda para revisión, sin reintentos automáticos.

Errores: `401` para Bearer ausente o token inválido, `422` para payload inválido
y `503` si no se pueden verificar certificados o confirmar la persistencia.
Un error no confirma la recepción: la plataforma puede reintentar el mismo ID.


## Consulta pública de retiros

`GET /api/recalls` devuelve una lista ordenada por emisión descendente. Cada
retiro incluye `recall_id`, `batch`, `sku`, `reason`, `issued_at`, `deadline`,
`status` y `reported_at`. Se muestran también los avisos persistidos que todavía
no tienen investigación: `received` si están pendientes, `investigating` durante
procesamiento y `failed` si ocurrió un error.

`GET /api/recalls/{recall_id}` agrega `units`, con `product_id`, `sku`, `batch`,
`original_location`, `current_location` y `quarantined_at`. Antes de identificar
el lote, `units` puede estar vacío, incluido en un retiro fallido; no demuestra
que el retiro carezca de productos afectados. Devuelve `404` si no existe el ID.
También incluye `ownership` con la evidencia de pertenencia y
`market_deliveries` con las entregas de mercado investigadas: `trade_id`,
`custody_event_id`, `external_order_id`, `external_offer_id`, `counterparty_id`,
`unit_id`, `external_unit_id`, `batch`, `sku` y `dispatched_at`.
Las reventas se reportan con su OC oficial, sin notificar a la distribuidora
compradora. Un reporte vacío confirmado conserva `ownership.kind=unaffected`
y la fecha de verificación del inventario y del historial; un retiro aún sin
investigar devuelve `ownership=null`.
La sincronización bloquea también los derivados tardíos. Si queda plazo, el
worker actualiza la investigación y el reporte para incorporarlos.

## Diagnóstico operativo privado — paso 20

Estas cuatro rutas consultan exclusivamente datos persistidos. No concilian,
interpretan, compran, publican ofertas, emiten facturas ni pagan. Exigen:

```http
Authorization: Bearer <OPERATIONS_API_KEY>
```

Configura `OPERATIONS_API_KEY` con una clave privada independiente en el entorno
del backend. Si está ausente, las rutas devuelven `503`; una credencial ausente
o incorrecta devuelve `401`. Las respuestas exitosas y los errores de
autenticación incluyen `Cache-Control: no-store`. La clave no debe incluirse
en URLs ni en el código de un frontend público; su uso corresponde al equipo
o a un servidor autenticado del frontend.

Todas requieren `period_id` (UUID). El período debe existir y corresponder al
grupo y ambiente configurados para facturación; de lo contrario se devuelve
`404`. Una consulta no crea períodos. Las listas admiten `limit` (1–100,
por defecto 50) y `offset` (entero no negativo, por defecto 0). Los parámetros
inválidos producen `422`. El contrato común de las listas es:

```json
{
  "period_id": "5b62dc1c-87fd-474d-9252-364e15eab51f",
  "read_at": "2026-11-12T23:00:00Z",
  "data_source": "persisted",
  "items": [],
  "limit": 50,
  "offset": 0,
  "has_more": false
}
```

`read_at` es la fecha de lectura local, no una confirmación remota. `has_more`
permite solicitar la siguiente página aumentando `offset`; la paginación no
congela los datos si los workers los modifican entre solicitudes.

- `GET /api/operations/waiting-orders`: OC externas con estado local `received`,
  creadas dentro de las fechas del período, ordenadas por plazo más próximo.
  Incluye `id`, `external_order_id`, `status`, `external_status`,
  `decision_reason`, `delivery_deadline`, `created_at` y `updated_at`.
  Comprende tanto las que esperan su primera decisión como las aplazadas por
  stock; la razón persistida distingue esos casos. No devuelve datos del comprador.
- `GET /api/operations/offers`: ofertas del período, ordenadas por actualización
  local descendente. Incluye `id`, `external_offer_id`, `external_order_id`,
  `side`, `publisher_group_id`, `counterparty_group_id`, `sku`, `quantity`,
  `unit_price`, `status`, `updated_at`, `last_checked_at`, `reconciled_at`,
  `next_retry_at`, `http_status` y `has_error`. No expone cuerpos de errores.
  `status` es el último estado local, no una garantía de que siga abierto en el
  servicio remoto. `last_checked_at` corresponde al último intento de
  conciliación; `http_status` y `has_error` permiten distinguir sus fallos.
  Las fechas de conciliación nulas indican que esa etapa no está registrada.
- `GET /api/operations/invoices`: facturas del período, ordenadas por emisión
  descendente. Incluye `id`, `external_invoice_id`, `external_order_id`, `side`,
  `counterparty_id`, `channel`, `status`, `principal`, `interest`, `total`,
  `issued_at`, `external_updated_at`, `observed_at` y `paid_at`. Los importes de
  facturas y ofertas son cadenas decimales exactas, sin redondeo a `float`.
- `GET /api/operations/metrics`: devuelve `period_id`, `read_at`,
  `data_source=persisted`, `state`, `captured_at`, `commit_sha` y `metrics`.
  `state=not_captured` conserva los tres últimos campos en `null` cuando aún no
  hay snapshot. `state=captured` entrega el último `BusinessMetrics` guardado:
  ingresos, costos, intereses, margen, cobertura de facturación, pagos pendientes,
  conciliación y conteos de mercado. No recalcula el snapshot ni simula una
  actualización. `metrics.reconciliation` distingue una conciliación incompleta
  y conserva su fecha de cobertura; `captured_at` permite evaluar la antigüedad.

Las rutas sólo admiten GET. El diagnóstico no habilita operaciones manuales de
pago o compra. El portal, creación de órdenes, checkout, trazabilidad y retiros
no dependen de estas rutas ni de que se configure su clave. El flujo de compra
conserva sus dependencias propias: precios vigentes de Farma Central, checkout
y confirmación de despacho; no utiliza SOAP, ofertas de mercado ni LLM.

El repositorio contiene únicamente el backend. Los contratos públicos mantienen
sus campos existentes; `market_transfers` amplía el recorrido observado usando
el mismo tipo. Los nuevos DTO privados están en `app/schemas/operations.py` y en
`/openapi.json` para coordinar su consumo con el equipo de frontend. No se añade
un chat propio ni una pantalla financiera pública.
Si el frontend utiliza una unión cerrada para `current_units.status`, debe admitir
también `recall_pending`, `quarantined` y `expired` para representar el stock físico
bloqueado sin mostrarlo como vendible.
