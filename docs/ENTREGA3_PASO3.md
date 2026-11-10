# Entrega 3 — Paso 3: cliente SOAP de facturación

Implementado `app.clients.billing.BillingClient`, asíncrono, con las cuatro
operaciones oficiales: `emit_invoice`, `pay_invoice`, `get_invoices` y
`get_bank_statement`. No escribe en la base de datos ni se activa desde los
workers existentes. La conciliación periódica y la ejecución de tareas durables
del paso 2 se incorporan en los siguientes pasos.

## Contrato y autenticación

Se utiliza HTTPX y XML SOAP 1.1 con WS-Security UsernameToken/PasswordText.
Las credenciales son `FARMA_CENTRAL_GROUP` y `FARMA_CENTRAL_API_SECRET`.
Se eligió este cliente pequeño sin dependencias nuevas para conservar importes
como `Decimal` desde el XML y evitar conversiones intermedias a `float`.

El WSDL se carga una vez por cliente, valida las cuatro operaciones y proporciona
sus SOAPAction. El destino se obtiene de la configuración, no de la dirección
de desarrollo anunciada dentro del WSDL. Se rechazan HTTP remoto, credenciales
en URL, XML inseguro y respuestas inesperadas. Los errores y logs no incluyen
contraseñas, sobres SOAP ni textos arbitrarios de fallos remotos.

`BillingInvoice` y `BankStatement` validan identidades, estados, fechas con zona
horaria e importes finitos. `get_invoices` exige un rango de hasta tres días y
los filtros de lado y estado. Los importes no pasan por `float`.

## Fallos y límites

No hay reintentos automáticos. Si una emisión o pago pudo ejecutarse antes de
un timeout, desconexión, fallo del servidor o respuesta inválida, el error
incluye `outcome_uncertain=True`: el consumidor debe conciliar antes de repetir.
Un error de conexión anterior al envío no se marca como resultado incierto.

El límite local predeterminado es 45 solicitudes por minuto, por debajo de las
60 por IP publicadas por el servicio. Un archivo con bloqueo entre procesos
comparte el ritmo y el cooldown de HTTP 429/Retry-After; el bloqueo no se mantiene
durante esperas ni llamadas de red. No controla clientes ajenos que compartan IP.

Variables opcionales en `.env.example`:

| Variable | Valor predeterminado |
| --- | --- |
| `BILLING_URL` | `/soap/billing` en el host de Farma Central |
| `BILLING_WSDL_URL` | URL anterior con `?wsdl` |
| `BILLING_TIMEOUT_SECONDS` | `10` |
| `BILLING_REQUESTS_PER_MINUTE` | `45` |
| `BILLING_RATE_LIMIT_FILE` | `/var/lib/farmacentral/billing.rate` si existe el directorio; si no, archivo temporal por usuario |

La plantilla systemd incorpora `StateDirectory=farmacentral` para compartir el
archivo pese a `PrivateTmp=true`. Al instalar la plantilla actualizada se debe
ejecutar `sudo systemctl daemon-reload`; todos los procesos de facturación deben
usar el mismo archivo accesible por su usuario. Este paso no instala ni reinicia
servicios en el servidor.

## Verificación

```bash
.venv/bin/python -m pytest tests/clients/test_billing.py tests/clients/test_billing_rate_limit.py tests/scripts/test_verify_billing_connection.py tests/core/test_config.py
.venv/bin/python -m scripts.verify_billing_connection --include-invoices --output /tmp/billing-readonly.json
```

Las pruebas usan HTTP simulado y verifican las cuatro operaciones, autenticación,
precisión decimal, errores SOAP/HTTP, resultados inciertos sin reenvío y cuota
compartida entre procesos. La suite completa se ejecutó con PostgreSQL temporal
y conexiones externas bloqueadas; véase la evidencia adjunta.

La lectura real en desarrollo validó saldo del grupo 3 y las cuatro combinaciones
de facturas (cliente/proveedor, pendiente/pagada), sin facturas en las últimas
24 horas. No se emitieron ni pagaron facturas, y no se llamó al LLM. La respuesta
vacía comprueba conectividad y contrato de lectura; no acredita el ciclo contable.

Se conserva pendiente confirmar la vinculación oficial factura–OC y el alcance
del pago automático descrito por fuentes externas. El cliente no deduce esa
relación por monto o fecha ni implementa todavía decisiones de negocio.
