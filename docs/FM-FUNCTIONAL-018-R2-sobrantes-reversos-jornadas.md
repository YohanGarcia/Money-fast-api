# FM-FUNCTIONAL-018-R2 — Sobrantes, reversos y jornadas heredadas (DISEÑO, sin implementar)

> Estado: **propuesta para aprobación**. Nada de este documento cambia el flujo financiero actual.
> Principio rector: el **administrador supervisa y autoriza**; el **cajero opera y ejecuta**; el original nunca se edita.

---

## 1. Sobrantes — cómo funciona HOY (identificado)

Flujo legado (`/api/v1/cash/commands`, `app/services/cash_service.py`):

1. **Reportar** (`report_surplus`, solo cajero): crea una fila `CashDelivery` en estado `surplus_review` ligada a **su** jornada
   (`session_id`) y al cobrador. No mueve dinero.
2. **Confirmar** (`confirm_surplus`, solo administrador): pasa el estado a `surplus_confirmed` **y crea un movimiento
   `surplus` de `+declared` en la jornada que lo reportó**; ese movimiento **sube el saldo de la Caja** (`add_movement`).
3. **Rechazar** (`reject_surplus`, administrador): solo cambia el estado; no mueve dinero.
4. Mientras exista un sobrante en `surplus_review` en la caja, el **cierre** de cualquier jornada de esa caja se rechaza
   (política A, decisión D1 pendiente).

**Hallazgo.** En el paso 2 es el *administrador* quien dispara el movimiento que modifica el saldo de una jornada ajena. Con R1/R2
la jornada ya no la elige el administrador (cae solo en la que reportó el sobrante) y por eso se mantuvo como única excepción
(`reviewed_report`), pero sigue siendo una entrada de efectivo en la jornada de un cajero creada por un administrador.

### Propuesta (coherente con "administrador supervisor, cajero operador")
Separar **reconocimiento** de **asiento**, igual que Nómina:

| Paso | Quién | Efecto |
|---|---|---|
| Reportar sobrante | cajero | `surplus_review` (sin cambio de saldo) |
| **Autorizar** sobrante | administrador | `surplus_authorized`: decisión + motivo + auditoría. **Sin movimiento.** |
| **Registrar ingreso** | **cajero dueño de la jornada** | crea el movimiento `surplus` en **su** jornada, idempotente, con auditoría de quién autorizó |
| Rechazar | administrador | `surplus_rejected` (sin movimiento) |

Reglas: un sobrante solo se registra una vez (restricción única por entrega); si la jornada ya cerró, el sobrante **no** se
inserta en el día cerrado: pasa a la **diferencia** de ese cierre (ver 2) o se registra como ingreso de la siguiente jornada
del mismo cajero con referencia al original. El cierre de la jornada espera solo a los sobrantes aún en `surplus_review`
o `surplus_authorized`.

Alternativa más simple (sin cambio de estados): registrar el sobrante **solo como diferencia** en el cierre del cajero
(`counted − expected` ya queda registrado sin asiento). Pierde la trazabilidad por cobrador.

**Decisión requerida:** elegir entre la propuesta de 2 pasos o la alternativa por diferencia de cierre.

---

## 2. Reversos — diseño "administrador autoriza, cajero ejecuta"

### Hoy
`reverse` (legado) exige rol administrador y escribe un movimiento `reversal` en una jornada elegida por él. **Bloqueado** desde R1
(el administrador no opera jornadas). Nadie puede revertir actualmente.

### Modelo propuesto
Tabla nueva `cash_reversal_authorizations` (migración posterior, aditiva):

| Campo | Notas |
|---|---|
| `id`, `company_id`, `box_id` | aislamiento por empresa/sucursal |
| `original_movement_id` | FK; **única entre estados activos** (`authorized`, `executed`) → no hay reversos duplicados |
| `authorized_by`, `authorized_at`, `reason` | administrador; motivo obligatorio |
| `status` | `authorized` → `executed` \| `cancelled` \| `expired` |
| `executed_by`, `executed_at`, `reversal_movement_id` | cajero y asiento compensatorio |
| `idempotency_key`, `request_digest` | reintentos seguros |

### Flujo
1. **Autorizar** (administrador): valida que el movimiento original exista, sea de un tipo reversible (`expense`, `withdrawal`, `bank_deposit`,
   `cash_addition`, `delivery`, `counter_payment`, `disbursement`, `surplus` — la lista actual), no tenga ya un reverso activo y pertenezca a
   su empresa. **No** crea movimientos.
2. **Ejecutar** (cajero): en **su** jornada abierta de la **misma caja**, crea **un** movimiento compensatorio (`reversal`, `reverses_id=original`,
   monto de signo contrario). El original **no se toca** (los triggers de inmutabilidad ya lo garantizan).
3. **Cancelar** (administrador) mientras esté `authorized`; **expira** si no se ejecuta en N días (parámetro).

### Controles
- **Duplicados:** índice único parcial sobre `original_movement_id` para estados activos + clave de idempotencia al ejecutar.
- **Saldo negativo:** si el reverso **saca** efectivo (revertir un aporte/cobro), exige `saldo ≥ monto` en la jornada del cajero; si no alcanza → 409 y la autorización queda `authorized`.
- **Deuda del cliente:** el reverso de **Caja** corrige solo efectivo. **No** modifica préstamos, cuotas ni aplicaciones. Un movimiento que proviene
  de un **pago de préstamo** se revierte únicamente por el flujo de reverso de pagos (`payments.reverse`, que ya exige jornada propia
  y recompone la deuda); la autorización de Caja lo rechaza con un mensaje que apunta a ese flujo.
- **Entregas de cobrador:** igual que hoy, el reverso de una entrega recompone la custodia del cobrador con una entrega de corrección; no reabre nada.

### Jornada original ya cerrada
- El asiento compensatorio **nunca** se inserta en la jornada cerrada (el cuadre y su `snapshot` son inmutables).
- Se ejecuta en la **jornada abierta actual del cajero de esa caja**, con `reverses_id` apuntando al original y referencia al día original.
- Si no hay jornada abierta, la autorización espera (no ejecuta) y se le indica al cajero que abra su caja.
- Quien ejecuta puede ser el cajero original o cualquier cajero autorizado de la caja; ambos quedan en la auditoría.
- El reporte del día cerrado no cambia; el efecto contable cae en la fecha de **ejecución** (`executed_at`), igual que Nómina.

**Decisión requerida:** aprobar el modelo, el plazo de expiración y si cualquier cajero de la caja puede ejecutar.

---

## 3. Jornadas heredadas cuyo dueño no es cajero

### Situación
Tras R1/R2, una jornada **abierta** cuyo dueño tiene rol operativo distinto de cajero (típicamente un administrador del sistema anterior)
no puede cerrarse ni moverse: queda bloqueada y ocupa su punto de caja.

### Procedimiento de cierre supervisado (propuesto)
Antes de migrar **cualquier** dato de la base antigua (hoy no se migra nada):

1. **Inventario** (solo lectura, ver `scripts/inventory_legacy_noncashier_sessions.sql`): jornadas abiertas con dueño no cajero, saldo esperado
   (`apertura + Σ movimientos`), movimientos, entregas pendientes, sobrantes en revisión.
2. **Congelar**: ninguna operación nueva en esas cajas (suspender el punto de caja).
3. **Conciliar**: un supervisor cuenta el efectivo físico y lo contrasta con el saldo esperado; se firma un acta (importe contado, diferencia, motivo).
4. **Cerrar**: opción A (recomendada) — cierre en el **sistema anterior** antes de migrar. Opción B — acción administrativa auditada nueva
   `supervised_close`: asigna la jornada a un **cajero designado** (registro `session_ownership_transfer` con motivo, quién autoriza y acta) y ese cajero la
   cierra con su conteo; la diferencia queda registrada como en un cierre normal.
5. **Reabrir** el punto de caja y verificar que no quedan jornadas bloqueadas (la misma consulta del paso 1 devuelve 0 filas).

### Conciliación
`saldo_esperado = apertura + Σ(entradas) − Σ(salidas)` por jornada; diferencia = `contado − esperado` (sin asiento, como el cierre simple).
Los totales por empresa deben coincidir con el reporte financiero antes y después.

**No se ejecuta nada sobre datos reales** ni se migra la base antigua sin autorización expresa.
