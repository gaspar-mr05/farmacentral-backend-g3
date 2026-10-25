# Entrega 2 — Paso 20: idempotencia y recuperación

Se reutilizan la ingestión, los workers y el modelo del equipo. No hay nuevas
migraciones ni variables de entorno.

## Identidad de las operaciones

| Operación | Protección y recuperación |
| --- | --- |
| sFTP | Evento único por `(sftp, path)`. Conserva XML y SHA-256 originales. Un evento pendiente se procesa desde PostgreSQL sin releer el archivo. |
| Pub/Sub | Evento único por `(pubsub, messageId)`. `ackId` identifica cada entrega. Se confirma después de persistir; una redelivery no crea otra OC. |
| Orden externa | `external_order_id` único, en minúsculas, compartido por los tres canales. Conserva el canal inicial y reconcilia decisiones oficiales. |
| Mensajería | Evento único por `(messaging, messageId)`. Reutiliza una interpretación ya persistida y continúa desde la OC vinculada. |
| Recall | Evento único por `(recall, recallId)` y `Recall.recall_id` único. Webhook y polling confluyen en el mismo retiro. |
| Despacho | `OrderUnit.unit_id` único e intención durable `dispatch_expected_quantity`. Se consulta cantidad oficial y presencia física antes de reintentar. |
| Cuarentena | Snapshot único por recall/unidad. Un movimiento pendiente de una respuesta interrumpida se reconcilia contra el inventario oficial de cuarentena. |
| Notificación | Fila única por `RecallOrder`, payload e intentos persistidos antes del POST. Confirmación posterior o resultado incierto explícito. |
| Reporte | Último payload y `reported_at` por recall. Puede reemplazarse antes del deadline. |

Los locks de fila siguen protegiendo órdenes y unidades. Además, un lock
PostgreSQL por mensaje o recall permanece adquirido durante los commits internos.
Otro worker no toma el evento sólo porque haya pasado el minuto de recuperación
mientras el primero sigue activo. Al terminar el proceso se libera la conexión;
el evento puede recuperarse desde su estado durable. La recuperación manual usa
el mismo lock.

Las decisiones de OC se refrescan bajo lock para evitar una segunda aceptación
desde una sesión obsoleta. Una lectura remota `creada` no revierte un estado más
avanzado. No se pueden cambiar SKU, cantidad o precio de ítems con unidades
asignadas; las actualizaciones de órdenes aún no asignadas siguen disponibles.

## Respuestas perdidas

En mensajería se vuelve a comprobar el plazo después de interpretar. Si se perdió
el `orderId` y la plataforma responde `409`, se detiene la respuesta automática
y se utiliza la recuperación ya existente con el ID oficial identificado:

```bash
.venv/bin/python -m scripts.recover_messaging_order MESSAGE_ID ORDER_ID
```

Una interpretación persistida no se vuelve a solicitar. Si el proceso muere
antes de persistirla, no se puede garantizar que la llamada al modelo no se repita.

Un movimiento pendiente encontrado en la cuarentena oficial se registra
localmente sin otro PATCH. Si un listado truncado no contiene la unidad, esa
ausencia no se interpreta como prueba: se conserva el bloqueo y el diagnóstico.

El [contrato de Farma Central](https://dev.proyecto.2026-2.tallerdeintegracion.cl/farma-central/openapi.json)
permite reemplazar reportes. Para notificaciones, el contrato consultado no
ofrece consulta de recibos ni garantiza deduplicación. Se conserva en el campo
JSON existente `RecallNotification.response.delivery_state`:

- `pending`: intención persistida antes de enviar.
- `confirmed`: confirmación recibida o respaldada por evidencia.
- `rejected`: rechazo explícito o prueba de que el envío no ocurrió.
- `uncertain`: no se puede conocer si el servicio recibió el envío.

Una intención `pending` abandonada por una caída también se considera incierta.
Los fallos históricos sin resultado registrado no se suponen rechazados.
Un resultado incierto deja el evento fallido sin `next_retry_at`; no se reenvía
a ciegas y se conserva la cuarentena. Un `429` permite reintento y respeta
`Retry-After`; un error permanente como `404` no genera un bucle de reintentos.

## Resolver una notificación incierta

Se necesita prueba de recepción o de no recepción. La ausencia de un log no
demuestra que el envío no ocurrió. Sin evidencia, se deja el resultado incierto.

```bash
read -r -p "ID del recall: " RECALL_ID
read -r -p "ID de la OC afectada: " ORDER_ID
read -r -p "Referencia de la evidencia oficial: " NOTIFICATION_EVIDENCE
```

Para una recepción confirmada, registrar su hora real con zona horaria:

```bash
read -r -p "Hora confirmada (ISO 8601 con zona): " NOTIFICATION_SENT_AT
.venv/bin/python -m scripts.resolve_recall_notification "$RECALL_ID" "$ORDER_ID" \
  --outcome sent --sent-at "$NOTIFICATION_SENT_AT" \
  --evidence "$NOTIFICATION_EVIDENCE"
```

Sólo si hay prueba de que la plataforma **no recibió** el envío:

```bash
.venv/bin/python -m scripts.resolve_recall_notification "$RECALL_ID" "$ORDER_ID" \
  --outcome not-sent --evidence "$NOTIFICATION_EVIDENCE"
```

El comando sólo escribe en PostgreSQL. Guarda las referencias de evidencia en
`response.resolutions`, no permite modificar una confirmación previa ni aceptar
una hora futura o fuera de la ventana del recall. Si queda plazo, deja el evento
pendiente para que el worker continúe. Después del deadline sólo registra la
auditoría (`requeued: false`); no permite enviar acciones tardías.

## Pruebas

```bash
.venv/bin/python -m scripts.test_e2 --step20
```

Usa PostgreSQL local con esquemas temporales, APIs simuladas y el bloqueo de
conexiones externas de sockets Python. No inicia workers reales ni consume LLM.

Incluye archivos repetidos, Pub/Sub con nuevo `ackId`, webhooks duplicados,
insertos concurrentes, caídas después de decidir oficialmente y antes del commit
local, workers activos que superan el minuto de recuperación, interpretación
persistida, respuesta de mensajería perdida y recuperación explícita del ID,
movimientos ya realizados, notificaciones inciertas con y sin recepción real
simulada, reportes reemplazados y despachos con confirmaciones perdidas.

Las notificaciones confirmadas no se repiten si el conjunto de productos no
cambió. Un reporte corregido puede incorporar productos nuevos antes del plazo.
Las pruebas demuestran los escenarios simulados; la comparación con el portal
y la operación continua siguen siendo verificaciones reales separadas.
