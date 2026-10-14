# FM-FUNCTIONAL-018-R1 — Seguridad de Caja: separación Administrador / Cajero

## Regla aprobada
El administrador de empresa **configura y supervisa** Caja y **nunca opera** una jornada. El cajero opera **solo la suya**.

## Dónde se hace cumplir (servidor)
| Punto único | Qué impone |
|---|---|
| `identity.authorization.effective_grants` | `cash.sessions.open/close`, `cash.handovers.receive`, `cash.field_custody.accept/refund` **no son efectivos** para un usuario con rol legado `admin`, aunque existan grants heredados o asignaciones anteriores. |
| `identity.catalog.ensure_system_role` | El rol "Administrador de agencia" de empresas nuevas ya no los incluye. |
| `cash.port._record` (todo cambio de saldo v2) | La jornada debe pertenecer a quien opera y ese usuario no puede ser administrador (cubre jornadas heredadas cuyo dueño es un administrador). |
| `cash_service.add_movement` (todo cambio de saldo v1) | Igual regla; única excepción: la **confirmación** de un sobrante que reportó el cajero de esa misma jornada (la jornada la fija el reporte, no el administrador). |
| `cash_port.deposit` / desembolso `loans.disburse` | Exigen ser el dueño de la jornada (`payments.create` y `loans.disburse` por sí solos no operan Caja). |

## Techo de delegación
`assert_within_ceiling` solo exime los permisos operativos cuando **un administrador de la empresa** (rol de sistema de la empresa, activo)
los entrega **a un cajero activo de la misma empresa** con alcance **sucursal o punto de caja** (nunca a toda la empresa). Cualquier otro actor
queda sujeto al techo normal. Definir un rol con esos permisos es inerte hasta asignarlo.

## Nómina desde Caja (`/api/v1/payroll/payments`)
1. **Autorizar** — `POST /payroll/payments` (administrador, `source=cash`): crea un pago `authorized` (sucursal, importe, empleado, idempotencia). No elige jornada ni mueve dinero.
2. **Ejecutar** — `POST /payroll/payments/{id}/execute` (**solo cajero** de esa sucursal): valida su propia jornada abierta y su saldo, registra **una** salida en **su** jornada, deja auditoría `payroll_payment` con quién autorizó y quién ejecutó. Reintento idempotente (misma respuesta).
3. **Cancelar** — `POST /payroll/payments/{id}/cancel` (administrador) mientras siga `authorized`.
4. Pago desde **banco**: sin cambios, directo y solo del administrador.
5. Los reportes financieros cuentan solo pagos `paid`.

Migración `0025` (aditiva: estado y quién ejecutó/canceló; bajar de versión se rechaza si existen pagos autorizados/cancelados).

## Reversos y sobrantes (decisión de negocio pendiente)
- El administrador **ya no** puede registrar un reverso en la jornada de un cajero (el reverso es un asiento de caja). Hoy el reverso queda **bloqueado** para todos.
  Propuesta (misma forma que Nómina): el administrador **autoriza** el reverso y el cajero de la jornada lo **ejecuta**. Requiere aprobación.
- Confirmar sobrante sigue siendo del administrador, pero siempre cae en la jornada que lo reportó.

## Jornadas heredadas de un administrador
Una jornada abierta cuyo dueño es un administrador ya no es operable (ni cerrable). Antes de migrar datos de la base antigua hay que cerrarlas en el sistema anterior o definir un procedimiento de cierre supervisado.
