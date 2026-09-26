# Ejemplos de Farma Central

Respuestas reales capturadas desde el ambiente `dev` el 26 de septiembre de
2026. Estos archivos sirven para acordar contratos y modelos con Backend B; la
aplicación no debe usarlos como datos de producción.

- `products-available.dev.json`: respuesta de `GET /products/available`.
- `spaces.dev.json`: respuesta de `GET /spaces`.
- `inventory-by-space.dev.json`: respuestas de
  `GET /spaces/{storeId}/inventory` para cada espacio. Cada respuesta está
  acompañada por el `storeId` usado en la consulta.

Antes de actualizar estas muestras, se deben volver a revisar por posibles
credenciales o datos sensibles.
