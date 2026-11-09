# Entrega 3 — Paso 1: base E2, contratos y cambio de período

Revisión del 9 de noviembre de 2026 sobre `9ea0423`, más los scripts, pruebas y documentos de esta auditoría. Este paso prepara la implementación de E3; no implementa todavía mercado, facturación ni consultas de estado.

## Resultado y alcance

- **829 pruebas automatizadas aprobadas** sobre PostgreSQL temporal, con conexiones externas bloqueadas excepto loopback. Cero llamadas al LLM. La base temporal se detuvo y eliminó al terminar.
- Migraciones ensayadas desde una base vacía hasta `8c62e71f4b09`, con lectura de estado en una transacción de sólo lectura.
- **Inventario de desarrollo consultado:** 71 productos en catálogo, 7 espacios y 273 unidades sumadas de los conteos observados. Esto no prueba disponibilidad vendible, linaje ni conservación de cada unidad.
- El PostgreSQL configurado localmente (`localhost:5432/farmacentral`) y la API local (`127.0.0.1:8000`) estaban apagados/no accesibles. No se reinstalaron, arrancaron ni migraron para ocultar ese resultado. La prueba de migraciones corresponde a otra base temporal.
- No se conectó por SSH ni se inspeccionaron los procesos productivos. No se fabricó, movió, compró, vendió, facturó o pagó nada en servicios del curso. La lectura autenticada de inventario sólo obtuvo catálogo, espacios y conteos.
- Se guardaron documentación pública y WSDL con fecha, URL, estado HTTP y SHA-256. Mercado sigue pendiente según su fecha de publicación; las nuevas operaciones de mensajería aún no aparecen en la especificación observada.

Evidencia estructurada: [ENTREGA3_PASO1_EVIDENCIA.json](ENTREGA3_PASO1_EVIDENCIA.json). Versiones públicas: [manifest.json](e3/contracts/2026-11-09/manifest.json).

## 1. Matriz de requisitos y responsables

`Probado aislado` significa ejecución automatizada con clientes simulados y DB de prueba. No sustituye una validación real del servidor o del período E3. Los problemas reproducidos son hallazgos, no correcciones ya aplicadas.

| Requisito | Estado al revisar | Evidencia concreta | Responsable / siguiente acción |
| --- | --- | --- | --- |
| Conexión al PostgreSQL operativo local | Pendiente: no disponible | Diagnóstico `database: failed`, `OperationalError` | Ambos: levantar el servicio habitual y repetir lectura; no reinstalar PostgreSQL |
| Esquema y migraciones | Probado en base temporal | Upgrade completo y head `8c62e71f4b09`; snapshot read-only | B: comprobar `current` en la DB operativa cuando esté encendida |
| API local y servicios | API no accesible; unidades systemd del proyecto no instaladas aquí | `api_readiness: failed`; servicios locales `LoadState=not-found` | Ambos: validar donde realmente estén desplegados; auditoría no inspecciona otros PID |
| Ambiente activo del diagnóstico | Observado: desarrollo | `configuration` del JSON, sin usuarios, claves ni query strings | A: repetir en servidor con su configuración real antes de E3 |
| Catálogo e inventario oficial dev | Lectura correcta de conteos | 71 productos, 7 espacios, 273 unidades observadas | A: reconsultar al preparar E3; no usar esta foto como inventario después del reset |
| Ingestión sFTP | Probado aislado | `tests/services/test_sftp_orders.py` | A: confirmar OC real E3 cuando se habilite su generación |
| Ingestión Pub/Sub y ack durable | Probado aislado | `tests/services/test_pubsub_orders.py` | A: mismo alcance; topic/subscription reales se verifican en su ambiente |
| Mensajería E2, duplicados y recuperación | Probado aislado | `test_messaging_processing.py`, `test_messaging_recovery.py`, tests HTTP/OIDC | Ambos: conservarlo y ampliar en pasos 14–18 |
| Producción y linaje hasta cliente | Probado aislado | `tests/e2e/test_entrega1.py`, `tests/services/production`, `tests/services/traceability` | Ambos: evidencia real de E3 pendiente tras reset |
| FEFO y reserva sin doble asignación | Probado aislado | `tests/api/test_order_fulfillment.py` | B: excluir además las futuras reservas de mercado |
| Vencimiento y stock no utilizable | Probado aislado | `test_replenishment_uses_current_traceable_stock`, tests de catálogo/fulfillment | B: mantener en nueva política por SKU |
| Frío, movimientos y capacidad/pulmón | Probado aislado | `tests/services/inventory/test_cold_chain.py`, `test_movements.py` y pruebas de supply | A: no hubo nueva inspección de unidades reales |
| Recalls E2: investigación, cuarentena y reporte | Probado aislado | `test_recall_response.py`, `test_recall_worker.py`, `test_recall_investigation_performance.py` | Ambos: ampliar a mercado y reporte vacío, paso 19 |
| Reinicio y persistencia | Probado con procesos locales de prueba | `test_continuous_operation.py`, `test_e2_idempotency.py` | Ambos: reinicio real del servidor no ejecutado |
| Inventario cambiante aborta todo el productor | **Reproducido; pendiente de corregir** | `test_unrelated_inventory_movements_exhaust_replenishment_snapshot_retries` | A+B: corrección acotada en `fix/e2-operational-gaps` o paso 13; medir sin relajar identidad/stock |
| 429 en cumplimiento de OC | **Reproducido; no hay espera durable por Retry-After** | `test_oc_429_keeps_order_but_current_worker_retries_before_retry_after` | A+B: programar próxima revisión/backoff, antes de aumentar frecuencia de consultas |
| Mercado y reservas comerciales | No implementado; contrato completo pendiente | No hay cliente trades ni entidades de oferta; docs HTTP 400 el 9/11 | A: contrato el 11; B: pasos 8–13 y migraciones del paso 2 |
| Dinero decimal y rol comprador | Brecha confirmada por revisión | `upsert_external_order` exige ser proveedor y rechaza precios no enteros; modelo local usa Integer | B: paso 2, sin truncar importes ni romper checkout |
| Facturación y conciliación continua | No implementado; SOAP disponible | WSDL con cuatro operaciones; sin cliente SOAP ni worker financiero | A: paso 3; B: pasos 2 y 4–7 |
| Enlace de factura con OC tras respuesta perdida | **Contrato por confirmar** | BillingDetails no incluye `order_id` ni equivalente explícito | A+B: consulta C1; no unir por monto o fecha |
| Cobro automático de ventas a clientes | **Fuentes contradictorias** | Enunciado p. 5 vs. guía pública SOAP capturada | A: consulta C2; comprobar en dev con OC cumplida cuando corresponda |
| Consultas de estado y texto al cliente | No implementado; ampliación anunciada el 12 | Enum actual order/discarded; ausencia de POST /messaging/messages en OpenAPI | A: confirmar contrato; B: historial y selección por teléfono |
| Lote ajeno y ventas a otras distribuidoras en recall | Brecha confirmada por revisión | Investigación actual falla si falta lote origen; notificador recorre todas las entregas | A+B: paso 19, diferenciando historial incompleto de lote realmente ajeno |
| Separación E2/E3 tras reset | Procedimiento preparado; no ejecutado | Sección 4 de este documento | Ambos: ensayo en dev y cambio controlado tras confirmación del curso |

Las reproducciones nuevas comprueban el fallo y que no se introducen datos inválidos o despachos dobles. No presentan ese fallo como comportamiento deseado ni certifican que el pendiente se haya resuelto. Sus expectativas deben actualizarse junto con la futura corrección.

## 2. Contratos observados y decisiones permitidas

### Facturación SOAP

Fuentes: [documentación](https://dev.proyecto.2026-2.tallerdeintegracion.cl/billing/docs) y [WSDL](https://dev.proyecto.2026-2.tallerdeintegracion.cl/soap/billing?wsdl), capturados públicamente el 9/11, sin operaciones SOAP ejecutadas.

- Namespace observado: `http://billing.tallerdeintegracion.cl`; autenticación documentada WS-Security UsernameToken, grupo y secreto. Las credenciales existentes deben reutilizarse según contrato; no se añadieron variables vacías al `.env` durante esta auditoría.
- Operaciones: `emitInvoice`, `payInvoice`, `getInvoices`, `getBankStatement`. `getInvoices` separa `side=client/supplier`, `status=pending/paid` y rango de creación; el rango documentado no supera tres días.
- La guía declara 60 solicitudes/minuto por IP. Tener un cliente por worker no convierte ese límite compartido en uno por proceso.
- `BillingDetails`: `id`, `client`, `supplier`, `channel`, `description`, `status`, `price`, `interest`, `totalPrice`, `createdAt`, `updatedAt`. Los importes aparecen como `s:float`; la contabilidad local deberá convertir y conservar precisión decimal con una política explícita, no truncar a int.
- No hay un campo OC explícito en esa respuesta. `PurchaseOrder.invoiced` (`facturado`) es un booleano: puede indicar que el efecto de emisión ocurrió, pero no recupera por sí solo el ID de factura.
- La unicidad remota de factura por OC y una tarea local única pueden impedir doble emisión, pero no bastan para prometer reconstrucción exacta de la asociación tras perder la respuesta.
- El interés documentado es simple: seis horas de gracia y 1% del principal por cada hora completa posterior. Los costos e intereses impagos al cierre también cuentan en el margen.

**Diseño financiero que se puede iniciar:** revisar continuamente facturas de ambos lados/estados, conservar pendientes antiguos, emitir ventas cumplidas, pagar recibidas pronto y separar saldo bancario de margen. Los intervalos se definirán en pasos 4–7, respetando el límite SOAP. La falta de una UI financiera no permite dejar esa revisión para intervención humana.

### Mercado

[Documentación](https://dev.proyecto.2026-2.tallerdeintegracion.cl/trades/docs) respondió HTTP 400 el 9/11. Su disponibilidad está anunciada para el 11/11. El script registra `pending`, no guarda el cuerpo de error como si fuera un contrato.

Se pueden planificar las reglas que sí están en el enunciado: ofertas completas, precio 0,5–1,5 veces el vigente, expiración a los sesenta minutos, reserva de unidades en despacho, transferencia centralizada con lote original, `orderId` del cierre y polling sin webhook. Autenticación, campos detallados de unidades, recuperación de POST inciertos y paginación se confirman con la especificación publicada.

Grupo 3: acceso directo a EXC-LACTOSA-DC, LAM-BLISTER-PVC y EXC-SUERO-FISIO; mercado o elaborados para SOB-PAPEL-EST, FRA-VIDRIO-120, VIA-VIDRIO-10 y EXC-JARABE-BASE. No se probó un abastecimiento real sujeto a esa restricción: comienza en desarrollo el 11.

### Mensajería

La OpenAPI capturada el 9/11 contiene sólo `/messaging/auth` y `/messaging/messages/{messageId}/response`; la respuesta de clasificación permite `order` y `discarded`. No aparece aún `inquiry` ni el envío nuevo de texto. Eso es una disponibilidad pendiente del 12, no autorización para conservar el descarte de consultas en E3.

El enunciado define dos acciones dentro de treinta minutos desde `receivedAt`: clasificar como consulta con la OC correcta y enviar texto a `sender.phone` con `replyTo`. El cliente y el worker actuales deben ampliarse. Guardar historial por teléfono es necesario para referencias naturales; nombre o último pedido global no bastan. No usar el chat de prueba productivo como prueba de creación de OC real.

## 3. Consultas técnicas pendientes — listas para presentar al curso

No se enviaron mensajes a terceros. Backend A lidera obtener respuesta oficial; B incorpora esa respuesta en las pruebas y el diseño.

| ID | Pregunta exacta | Por qué importa | Seguimiento |
| --- | --- | --- | --- |
| C1 | Si `emitInvoice(order_id)` tuvo efecto pero se perdió la respuesta, ¿qué campo/filtro o consulta oficial permite recuperar el `invoice_id` correspondiente a esa OC? El WSDL actual no incluye esa relación. Si está en `description`, ¿cuál es su formato garantizado? | Recuperar y auditar una emisión única sin asociar otra factura por heurísticas | A+B, antes de cerrar pasos 3 y 5; adjuntar WSDL/hash del manifest |
| C2 | En E3, ¿las facturas de sFTP, PubSub y mensajería se pagan automáticamente al emitir, como indica el enunciado p. 5, o sólo se autopagan las de cliente 999, como dice `/billing/docs`? | Cobro, conciliación, margen y pruebas reales | A; tratar el enunciado como requisito y confirmar el contrato efectivo, sin implementar una suposición silenciosa |
| C3 | En el mercado publicado el 11, ¿qué datos identifican las unidades de una transferencia y cómo se reconcilia crear/aceptar una oferta tras timeout? ¿Qué paginación y límites existen? | Custodia y compra/venta sin duplicados | A+B, revisar primero documentación nueva y preguntar lo que siga sin respuesta |
| C4 | En la ampliación del 12, ¿qué respuesta e ID devuelve el envío de texto y qué recuperación o deduplicación admite? ¿Cómo se marca exactamente una respuesta del chat sin OC real? | Reinicio entre inquiry y envío, y pruebas sin falsa OC | A+B, fixtures de contrato antes de integrar paso 18 |

## 4. Procedimiento de separación de período y reset

**Preparación del paso 1; ejecución posterior al aviso oficial del curso.** El reset afecta el estado externo antes del 18/11 a las 10:00 de Chile. No se ejecutó aquí un borrado, cambio de DB ni paro de servicios.

1. Registrar commit desplegado, head de migración, nombres de DB y ambiente efectivo de cada proceso, referencias de casilla/suscripción y evidencia final E2. No volcar `Environment` completo ni secretos a logs.
2. Respaldar DB e informes E2 fuera del repositorio, con permisos restringidos, y comprobar una restauración en una DB de ensayo. Incluir `.env`/llaves en el respaldo privado apropiado, nunca en los documentos versionados.
3. Ensayar un arranque vacío en dev. Para aislar períodos sin una nueva migración todavía, la opción sencilla es **una DB operativa E3 separada** con el mismo esquema, conservando la DB E2 para consulta. Definir propietario y credenciales existentes; no ejecutar `DROP DATABASE` ni `TRUNCATE` como automatismo.
4. Confirmar el reset externo y la época de los datos. No interpretar un timeout, 429 o listado fallido como stock cero. Revisar también notificaciones/archivos antiguos antes de habilitar consumidores.
5. En la ventana controlada, detener los procesos que escriben y la sincronización antes de cambiar conexiones; evitar que unos sigan escribiendo en E2 mientras otros usan E3. Configurar API, timer y workers en la DB E3 acordada y en URLs productivas coherentes; comprobar la precedencia de `.env.local` y del entorno de systemd.
6. Aplicar el head del esquema en la nueva DB, verificarlo y activar todos los servicios con la configuración acordada. No migrar órdenes, reservas, ofertas, facturas o tareas mutantes E2 como trabajo pendiente de E3. Catálogo, recetas y stock se obtienen del ambiente vigente.
7. Arrancar sin stock: suministro directo sólo permitido, mercado para los cuatro restringidos y producción según demanda. Los futuros workers de facturas y mercado deben estar implementados y activos para ese ensayo completo; este paso no los crea aún.
8. Ejecutar el diagnóstico de sólo lectura y pruebas de disponibilidad. Conservar una marca temporal de inicio/fin y estado de los servicios. Una vuelta atrás debe usar una combinación coherente de aplicación, esquema, DB y período; no reactivar consumidores E2 contra el externo E3.

El equipo puede elegir mantener una única DB con separación explícita por período en el paso 2. Si lo hace, probar que **todas** las colas, unicidades y consultas filtran el período; no basta cambiar una fecha en el dashboard.

## 5. Cómo repetir las verificaciones

Desde la raíz del backend. El diagnóstico no arranca servicios ni aplica migraciones.

```bash
# Observación local: configuración sanitizada, conexión y revisión de migraciones.
.venv/bin/python -m scripts.verify_e3_baseline \
  --api-url http://127.0.0.1:8000 \
  --output /tmp/e3-baseline-local.json

# Añadir lectura del inventario oficial del ambiente configurado.
.venv/bin/python -m scripts.verify_e3_baseline \
  --inventory \
  --api-url http://127.0.0.1:8000 \
  --output /tmp/e3-baseline-inventario.json

# Sólo donde existan las unidades systemd del proyecto.
.venv/bin/python -m scripts.verify_e3_baseline \
  --check-services \
  --api-url http://127.0.0.1:8000 \
  --output /tmp/e3-baseline-servidor.json
```

`--inventory` usa credenciales para autenticarse y únicamente lee datos de Farma Central; no llama al gateway. El JSON describe el proceso que ejecuta el comando, no las variables internas de otros procesos. Un código de salida 1 indica un check fallido, por ejemplo PostgreSQL apagado. Las omisiones se listan en `not_verified`; un check de conteos correcto no certifica E3 completa.

Para guardar una nueva versión de contratos públicos, usar otra carpeta con la fecha real. Cada captura exitosa conserva un archivo con timestamp y hash; no sustituye versiones de otro día ni guarda respuestas de error como especificaciones.

```bash
.venv/bin/python -m scripts.capture_e3_contracts \
  --output-dir docs/e3/contracts/AAAA-MM-DD
```

`AAAA-MM-DD` representa la fecha que se elija, no es una carpeta exigida por el programa. `pending` se usa para mercado HTTP 400/404/503 antes de la disponibilidad anunciada; no se interpreta como contrato válido. El script sólo consulta las URLs públicas predefinidas, sin autenticación, POST, cookies de sesión ni redirecciones.

Con PostgreSQL de pruebas disponible, ejecutar la suite ya existente:

```bash
.venv/bin/python -m scripts.test_e2
.venv/bin/ruff check app scripts tests
.venv/bin/ruff format --check app scripts tests
```

El ejecutor utiliza esquemas aislados y bloquea conexiones externas. Se recomienda `TEST_DATABASE_URL` apuntando a una DB local dedicada a pruebas; no confundirla con datos operativos. En esta auditoría se utilizó una instancia temporal independiente y se ensayaron migraciones allí.

## 6. Cierre del paso

El criterio documental y de auditoría del paso 1 queda cubierto: matriz, responsables, contratos versionados, procedimiento de reset, reproducciones y suite ejecutada. **No declara resueltas las brechas registradas.**

Quedan por verificar la DB/API operativas encendidas, los servicios remotos y flujos reales, las respuestas C1/C2, y los contratos que se habilitan el 11/12. Se puede iniciar el modelo del paso 2 y el cliente SOAP con estas limitaciones visibles; no cerrar la recuperación financiera ni desplegar E3 autónoma asumiendo respuestas que aún faltan.
