# Paso 25: preparación de producción y nueva época

Estado: preparación local implementada; activación y ensayo remoto pendientes.
La información de producción se verifica desde el lunes 16 de noviembre. La
evaluación comienza el 18 a las 10:00 y termina el 20 a las 22:00, hora de Chile.
Un inventario vacío **no confirma un reset**. Obtener la confirmación del curso y
guardar su referencia antes de inicializar la época de evaluación.

## Configuración sin mezclar ambientes

Las unidades systemd y `deploy/release.sh` fijan
`APP_ENV_FILE=/home/integracion/farmacentral-backend/.env`. Así Pydantic lee sólo
ese archivo, sin agregar `.env.local`. Las variables ya exportadas por el proceso
siguen teniendo prioridad. No ejecutar `source .env`: usar el selector explícito.
Conservar copias privadas separadas para desarrollo, producción e historial;
nunca versionarlas ni imprimir secretos. En el servidor, `.env` debe contener
exclusivamente la configuración del ambiente activo.
Configurar también `APP_ENV=prod` (o `dev` para el ensayo), explícitamente.

Desde el 16, cotejar con el portal del grupo:

- URLs de Farma Central, OC, mercado, mensajería, checkout, WSDL y dirección SOAP.
- Grupo, secretos, host/casilla/contraseña y known_hosts de sFTP.
- Proyecto, tópico, suscripción y archivo de cuenta de servicio Pub/Sub.
- URL pública, audiencias y cuentas de servicio OIDC de mensajería y recalls.
- Host y nombre de la DB; período seleccionado y claves privadas de operaciones.

Guardar una copia del WSDL oficial fuera del repositorio. Con `.env` ya preparado,
ejecutar (sustituir `HOST_OFICIAL` por el hostname publicado, sin `https://`):

```bash
export APP_ENV_FILE=/home/integracion/farmacentral-backend/.env
.venv/bin/python -m scripts.check_e3_readiness \
  --environment prod --expected-api-host HOST_OFICIAL \
  --wsdl-file /ruta/privada/billing.wsdl
```

El comando no hace llamadas remotas ni modifica datos. Comprueba las URLs,
configuración explícita, formato de la cuenta de servicio y dirección anunciada
en el WSDL; muestra booleanos y controles sin secretos. Un resultado válido no
verifica autenticación, permisos remotos, saldo, existencia de la suscripción,
salud del worker ni confirmación del reset. Verificar esos puntos por separado
con lecturas oficiales. Si el curso publica servicios en hosts diferentes,
revisar esa configuración y el comprobador antes de aprobarla.

Fijar explícitamente `REPLENISHMENT_SPENDING_LIMIT` según el saldo y presupuesto
del grupo; no usar el monto ilustrativo de `.env.example` sin evaluarlo. Revisar
`REPLENISHMENT_MINIMUM_STOCK`, `REPLENISHMENT_TARGET_STOCK` y vida útil mínima.
La facturación usa revisión de 60 s, sincronización completa de 600 s y límite
compartido de 45 requests/min por defecto. Revisar estos valores contra los
límites publicados. `BILLING_EMIT_SALES_ENABLED` y
`BILLING_PAY_INVOICES_ENABLED` deben habilitarse intencionalmente para operar;
la revisión estática falla si permanecen deshabilitados.

## Respaldo, migración y cambio de época

Ejecutar este procedimiento manualmente en mantenimiento, después de confirmar
el reset. Pausar los despliegues automáticos durante el cambio. Detener primero
API, target, timer e inventario; detener también cualquier worker o script
iniciado manualmente. El nuevo período no debe crearse mientras otro proceso
pueda escribir en su DB.

```bash
sudo systemctl stop farmacentral-backend.service farmacentral-workers.target
sudo systemctl stop farmacentral-inventory-sync.timer farmacentral-inventory-sync.service
```

Registrar el SHA anterior, configuración privada, versión de Alembic y período.
Respaldar la DB anterior con `pg_dump --format=custom`. Usar los parámetros libpq
`PGHOST`, `PGPORT`, `PGUSER` y un `.pgpass` privado; no poner contraseñas en la URL
de la línea de comandos. Los nombres siguientes son marcadores a reemplazar por
bases distintas; no ejecutar una restauración sobre la DB operativa.

```bash
umask 077
pg_dump --format=custom --dbname=DB_ANTERIOR --file=/ruta/privada/antes-e3.dump
pg_restore --list /ruta/privada/antes-e3.dump
createdb DB_ENSAYO_RESTAURACION
pg_restore --exit-on-error --no-owner --dbname=DB_ENSAYO_RESTAURACION /ruta/privada/antes-e3.dump
```

Verificar en la copia restaurada Alembic, período, cantidades e IDs conocidos de
órdenes, facturas y tareas. Conservar la DB original y su respaldo. El test
`test_e3_backup_restore.py` ensaya realmente dump/restore sobre un clúster
temporal; no sustituye comprobar el respaldo del servidor.

Crear una **DB nueva**, con el propietario/permisos de la aplicación, mediante
el administrador PostgreSQL. Seleccionarla en `DATABASE_URL` del `.env` activo y
comprobar que no haya una variable exportada que la sustituya. No copiar eventos,
unidades, reservas ni tareas del período anterior a esta DB. Varias tablas no
tienen separación por período: cambiar sólo `BILLING_PERIOD_ID` dejaría trabajo
antiguo activo. Mantener las credenciales remotas del ambiente elegido.

```bash
export APP_ENV_FILE=/home/integracion/farmacentral-backend/.env
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.prepare_e3_epoch \
  --environment prod --code e3-evaluation \
  --starts-at 2026-11-18T10:00:00-03:00 --ends-at 2026-11-20T22:00:00-03:00
```

Este último comando sólo verifica. Rechaza una DB con filas en cualquier tabla
de la aplicación, migraciones pendientes, ambiente distinto o fechas inválidas.
No borra ni archiva automáticamente nada. Para crear el período, repetirlo con
`--create --reset-reference 'REFERENCIA_REAL_DEL_AVISO_DEL_CURSO'`, únicamente
después de confirmar el reset; la referencia es una declaración del operador,
no una comprobación automática con el curso. Guardar el JSON privado y copiar
su `period_id` a `BILLING_PERIOD_ID` antes de iniciar servicios. No repetir la
creación: una DB con período ya creado deja de considerarse vacía.

El período de ensayo en dev debe usar otro código, fechas correspondientes al
ensayo y su propia DB; no reutilizar el período de evaluación.

## Workers y ensayo de arranque

Instalar las unidades actualizadas una vez, con privilegios administrativos:

```bash
bash deploy/install-systemd-units.sh
sudo systemctl start farmacentral-backend.service farmacentral-workers.target
sudo systemctl enable --now farmacentral-inventory-sync.timer
systemctl list-units 'farmacentral-worker@*' --no-pager
curl --fail http://127.0.0.1:8000/api/health/ready
```

El instalador incluye API, inventario, timer, plantilla y target, y detiene los
nombres financieros antiguos para evitar procesos duplicados. El deploy habitual
requiere las reglas sudoers documentadas para reiniciar API y target sin clave.
Revisar `/api/operations/workers?period_id=UUID_NUEVO` con la clave de operaciones
por un cliente privado. Exigir heartbeats recientes y ciclos completados para
el período nuevo; `systemctl is-active` por sí solo no demuestra progreso.

El ensayo remoto de dev sigue pendiente de ejecución por un integrante con SSH:

1. Coordinar un reset del sandbox; una DB local vacía no vacía el inventario remoto.
2. Con ambiente dev y DB nueva, ejecutar el mismo procedimiento de época.
3. Iniciar el reabastecedor y confirmar compras directas sólo de insumos permitidos,
   ofertas buy para faltantes, recepción de los cuatro restringidos y producción.
4. Revisar excedentes, buffer, lotes y margen con
   `.venv/bin/python -m scripts.sell_surplus --once` (plan sin ejecutar).
   Publicar sólo un plan revisado con `--execute --once`, o mantener el proceso
   con `--execute`. Ajustar `--lot-size`, `--kit-buffer`, `--raw-buffer-batches`
   y `--minimum-margin` al ensayo. Este script no se activa automáticamente por
   el target; acordar y supervisar su ejecución para sostener ventas.
5. Registrar IDs de ofertas, OC, unidades, producción, despachos y facturas;
   comprobar las primeras compras/ventas y pedidos cumplidos oficialmente.
6. Revisar `/`, `/trazabilidad`, recepción de recalls y chat en el sitio público.
   Completar la evidencia de `STEP24.md`, incluyendo reinicio y duplicados.

Las pruebas locales no consumen gateway ni hacen operaciones de negocio remotas.
No se ha activado producción ni ejecutado este ensayo remoto desde esta sesión.

## Vuelta atrás y observación

Ensayar previamente en la DB restaurada el SHA anterior con su configuración y
comprobar readiness y lectura del historial. Para revertir aplicación, detener
los servicios, seleccionar un checkout limpio del SHA probado, instalar sus
dependencias y reiniciar. No ejecutar `alembic downgrade`, truncar tablas ni
restaurar sobre la DB activa como parte de una reversión rutinaria. Comprobar
compatibilidad del esquema con ese SHA; este paso no agrega migraciones.

Si el reset remoto ya ocurrió, **no reactivar la DB/período anterior**: conservar
la nueva época y revertir sólo a una versión compatible. Preservar operaciones
inciertas para conciliarlas antes de volver a ejecutarlas. Para versiones que
no conocen `APP_ENV_FILE`, retirar del directorio activo cualquier `.env.local`
de desarrollo, guardándolo de forma privada.

Tras desplegar, observar logs privados, heartbeats, saldo/deuda, vencimientos,
reservas y errores de canal. Publicar el código revisado en GitHub antes del
inicio. El paso 25 sólo se considera cerrado cuando el ensayo remoto produce
compras/ventas y pedidos cumplidos, todos los servicios usan el ambiente correcto
y el despliegue público está comprobado. Esas verificaciones siguen pendientes.
