# Caja por sucursal

Implementación en API, web (`/cash`) y Expo (`Mi jornada`). Moneda RD$; zona horaria America/Santo_Domingo.

## Activación

1. Ejecutar `alembic upgrade head` con respaldo previo.
2. Crear las sucursales en Configuración y asignar sucursal a todos los cobradores y cajeros activos.
3. En Caja, configurar el saldo inicial de cada sucursal activa. Es dinero físico verificado, no la suma de pagos históricos.
4. Habilitar Caja. Desde ese momento los cobros necesitan medio, origen, sucursal y clave de idempotencia. Las aplicaciones antiguas reciben un mensaje de actualización.
5. Abrir la jornada con el efectivo contado. Una diferencia requiere resolución del administrador.

Los pagos anteriores permanecen históricos y no generan automáticamente deuda del cobrador. El Cajero cuenta como usuario del plan y trabaja desde la web. Gerencia consulta su sucursal; cobradores consultan y declaran sus propias entregas en Expo.

## Recorrido

- Efectivo en campo: abona al préstamo y queda a cargo de quien lo registró.
- Entrega declarada: reserva el importe para recepción, sin afectar caja ni el préstamo.
- Recepción: Caja confirma el importe contado y distribuye la entrega entre los cobros más antiguos, incluso parcialmente. El faltante sigue pendiente y exige motivo.
- Ventanilla: abona al préstamo e ingresa a la jornada abierta en una transacción.
- Transferencia: requiere referencia, destino y PDF/JPG/PNG de hasta 5 MB. Solo la confirmación de Caja aplica el abono; no modifica efectivo físico.
- Cuadre: apertura + entradas − salidas. Un cierre exacto es inmediato; con diferencia queda bloqueado para revisión del administrador. Los pendientes del cobrador se arrastran aparte.
- Desembolso: la solicitud debe estar firmada y vinculada. Préstamo, cuotas, transición y movimiento se guardan juntos. El desembolso bancario registra un movimiento de efectivo cero; su importe está en el préstamo vinculado y en la auditoría de la solicitud. Ambos medios se operan dentro de una jornada abierta.

## Correcciones y trazabilidad

No se borran movimientos ni pagos confirmados. Un reverso autorizado agrega un movimiento compensatorio en la jornada actual; los cierres previos conservan sus cifras. Revertir una entrega restaura el pendiente mediante aplicaciones negativas y conserva las aplicaciones originales. Revertir el movimiento de un cobro o desembolso corrige exclusivamente el efectivo: **no cancela el préstamo ni elimina su abono**. El administrador debe documentar el motivo de la corrección.

Los cobros conservan sucursal y cobrador originales al reasignar un cliente. Usuarios con saldo, entrega o transferencia pendiente no pueden cambiar de sucursal/rol ni desactivarse. Las sucursales con Caja conservan su historial y no se eliminan.

El historial admite fecha inclusiva local, día, semana desde lunes, mes, sucursal, cobrador, responsable, tipo y estado. Excel y la vista imprimible/PDF usan los mismos filtros. Los comprobantes se consultan autenticados.

## Transacciones

Las escrituras de Caja bloquean su fila de sucursal antes de consultar saldos. Versiones evitan sobrescritura de jornadas, entregas, transferencias y solicitudes. Las claves de idempotencia se conservan para reintentos; reutilizar una clave con otro contenido produce 409. Cualquier fallo revierte también cuotas y aplicaciones de entrega. Un importe mayor al saldo aplicable se rechaza sin cambios parciales.

## Validación

`python -m unittest tests.test_cash tests.test_api tests.test_calendar -q`

La suite cubre entregas parciales, arrastre, transferencias y confirmación repetida, sobregiros, versiones, reversos, diferencias y apertura siguiente, aislamiento, roles, límite de usuarios, reasignación, operaciones simultáneas, reportes, medianoche, históricos y desembolsos con fallos posteriores a crear el préstamo.

Web: `npm run build`. Expo: `npx tsc --noEmit` y exportación Android. Las pruebas en navegador de Expo verifican los componentes y llamadas de la app; no sustituyen una prueba física de cámara, selector nativo de archivos o impresión.

## Sobrantes de entregas

El cajero los reporta por separado. El administrador aprueba su incorporación como sobrante o rechaza con motivo documentado. No liquidan cobros ni generan abonos. Un sobrante por revisar bloquea el cuadre para evitar contarlo también como diferencia de cierre.

## Actualizaciones en tiempo real
La web conecta a `/api/v1/cash/live` mediante WebSocket y autentica con el token de acceso en el primer mensaje (nunca en la URL). Solo administradores, cajeros y gerentes autorizados a la sucursal pueden suscribirse. Los avisos se publican después del commit; los errores y reintentos idempotentes no generan cobros adicionales. La web vuelve a consultar la información autorizada al recibir un aviso o reconectar, sin sondeo periódico. El heartbeat comprueba la conexión y la vigencia de la sesión; no refresca los saldos.

El transporte actual de eventos es en memoria y requiere **un único proceso/worker de API**, como el entorno local. Antes de escalar a varios procesos o instancias, sustituir el transporte por pub/sub compartido (por ejemplo Redis). El proxy de producción debe permitir WebSocket Upgrade en `/api/v1/cash/live` y usar WSS con HTTPS. Vite ya tiene `ws: true`.

La sección Entregas permite seleccionar un cobrador, revisar sus cobros pendientes y recibir efectivo directamente con receive_collector. Si hay entregas declaradas pendientes, se exige recibirlas primero. La recepción directa valida sucursal, jornada, versión e idempotencia, distribuye el efectivo a los cobros más antiguos y no vuelve a abonar al préstamo. Una recepción parcial exige motivo y conserva el resto pendiente.

