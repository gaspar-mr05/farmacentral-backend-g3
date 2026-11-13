# Paso 24: validación real en desarrollo

Estado al 13/11/2026: **pendiente**. Una suite con mocks o una respuesta HTTP
correcta no certifica los recorridos completos ni el puntaje del curso.

## Evidencia obtenida en esta sesión

- Readiness público del backend: HTTP 200.
- SOAP `getBankStatement`: lectura exitosa. No se emitió ni pagó una factura.
- Gateway: una llamada autorizada a `gemini-flash-lite`, salida máxima 128 tokens;
  consumo informado de 368 tokens de entrada y 97 de salida (465 total).
  Clasificó una consulta sintética como `inquiry`. No verifica transporte de chat,
  elección de OC ni una respuesta real al cliente. No se conoce el costo exacto
  de esa llamada; no se estima una tarifa ni un presupuesto renovado.
- El OpenAPI público de mensajería sigue mostrando sólo autenticación y respuesta
  de clasificación; no incluye `inquiry` ni `POST /messaging/messages`.
- La DB local no fue accesible (`OperationalError`). SSH desde esta sesión fue
  rechazado por autenticación; no se reiniciaron servicios.

Capturas públicas sin claves, saldo ni datos del cliente:
`evidence/2026-11-13/public-probes.json` y `gateway-probe.json`. Los detalles
financieros de la consulta SOAP se conservaron sólo en un archivo privado local.
El usuario autorizó minimizar el costo bajo su límite informado de USD 1; no se
utilizó el antecedente de USD 2 para asumir capacidad adicional.

## Observador de E3

`scripts.validate_e3_flow` exige `--environment dev`. Rechaza cualquier endpoint
remoto configurado fuera del host oficial de desarrollo, incluyendo SOAP y WSDL.
Abre PostgreSQL con `SET TRANSACTION READ ONLY`, sin migraciones, cambios de
estado, sincronizaciones ni llamadas al LLM. La autenticación técnica de los
clientes puede usar POST; las únicas operaciones SOAP son consultas de facturas.
El limitador local habitual de SOAP sigue aplicándose.

Con este código instalado en el servidor donde está la DB, desde su terminal:

```bash
cd /home/integracion/farmacentral-backend
.venv/bin/python -m scripts.validate_e3_flow \
  --environment dev --list-ids --output /tmp/e3-periods.json
```

Elegir explícitamente el período correcto, sin crear uno ni escogerlo por fecha:

```bash
.venv/bin/python -m scripts.validate_e3_flow \
  --environment dev --period-id UUID_DEL_PERIODO --list-ids \
  --output /tmp/e3-targets.json
```

Observar IDs reales ya procesados por los workers. Repetir cada opción para
incluir varios canales, compra/venta y consultas/recalls:

```bash
.venv/bin/python -m scripts.validate_e3_flow \
  --environment dev --period-id UUID_DEL_PERIODO \
  --order-id OC_SFTP --order-id OC_PUBSUB \
  --trade-id UUID_COMPRA --trade-id UUID_VENTA \
  --message-id MENSAJE_CON_ID --message-id MENSAJE_NATURAL \
  --recall-id RECALL_MERCADO --recall-id RECALL_AJENO \
  --output /tmp/e3-observations.json
```

Los UUID de mercado son los IDs locales que imprime el descubrimiento. Las OCs
son IDs oficiales. El observador comprueba cumplimiento y trazabilidad de ventas,
cierres y evidencia de unidades en mercado, facturas oficiales por ID en sus
ventanas de emisión y fases locales de mensajería. El estado de cobro de una
factura de venta se observa como `pending/paid`; no se supone que el comprador
pague de inmediato. Una factura de compra pendiente no acredita el pago.
La asociación factura/OC parte de la evidencia local: el contrato SOAP no expone
esa asociación dentro de `BillingInvoice`; debe contrastarse con recibos/portal.
Los reportes de recall y los envíos se presentan como auditoría local, sin
atribuirle al portal ni al chat una confirmación que no se consultó.

Los reportes se crean con permisos 0600, no se sobrescriben y pueden contener
información financiera privada: conservarlos fuera del repositorio. El código
1 indica un chequeo fallido; el código 2 indica trabajo de validación pendiente,
incluso si las comprobaciones seleccionadas pasan. `automated_status` resume lo
observado; `step24_status` permanece pendiente hasta la revisión manual de toda
la evidencia. La falta de acceso no se convierte en un resultado aprobado.

## Recorridos y evidencia aún requerida

| Recorrido | Evidencia mínima a conservar | Estado |
| --- | --- | --- |
| sFTP y Pub/Sub | ID/canal, recepción, linaje o compra, unidades y despacho en plazo, OC cumplida, factura y cobro observado | Pendiente |
| Compra de mercado | Oferta y OC oficiales, contraparte, IDs y lotes recibidos/pulmón, factura y pago oficial | Pendiente |
| Venta de mercado | Oferta y OC oficiales, unidades vendidas, factura emitida y estado real de cobro | Pendiente |
| Consultas explícita y natural | Mensajes recibidos de dos tipos, OC correcta según historial, ambas confirmaciones antes de 30 minutos, teléfono correcto y recepción en chat | Pendiente |
| Estado parcial, rechazado y vencido | Snapshot oficial y texto que refleje ese estado sin inventar cumplimiento | Pendiente |
| Recall comprado y lote ajeno | IDs, reporte en plazo, cuarentena/notificaciones correctas, reporte vacío sin movimientos en caso ajeno, verdad/diff del portal cuando esté disponible | Pendiente |
| Recuperación real | IDs y timestamps antes/después de un reinicio coordinado durante conciliación/emisión/envío, sin duplicaciones | Pendiente |

Para generar escenarios, usar las opciones oficiales del portal y coordinar la
contraparte de mercado con otro grupo. No se inventan endpoints de prueba. El chat
oficial prueba transporte y defensa oral; en producción no acredita una OC real
ni puntaje. Una prueba nueva del gateway exige respetar el presupuesto compartido;
esta sesión ya realizó la única llamada prevista.

Los reinicios requieren coordinar el flujo y registrar su fase antes de actuar.
No repetir una emisión, pago, aceptación o envío para averiguar si el anterior
ocurrió. Ante incertidumbre, aplicar los procedimientos del paso 22 y conservar
la evidencia. El observador no inicia operaciones ni reinicia procesos.
