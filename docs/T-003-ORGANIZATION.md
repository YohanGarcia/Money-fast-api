# T-003 — Tenant, Branch, Currency & Cash Foundation (documentación técnica)

Base: DR-001 (multi-tenant), DR-005/ADR-006 (tiempo), DR-007 (diferencia de caja), DR-008 (autorización), DR-022 (moneda), DR-024 (organización), DR-029 (GET no muta), DR-030 (auditoría), DECISION-REGISTER-v2.md (vigente), T-001, T-002.
Rama: `feat/t003-tenant-branch-currency-cash` desde `refactor/backend-v2 @ e50662b` (tags `T002_APPROVED`, `T002_MERGED_TO_BACKEND_CONVERGENCE`).

## 1. Baseline previo (estado legacy antes de T-003)

| Área | Estado encontrado |
|---|---|
| Companies | `companies` (id, name, slug —añadido en T-002—, tax_id, address, phone, `is_active`, plan_id, suscripción PayPal). Ya era el tenant de facto: 22 FKs `company_id` en el resto del esquema |
| Sucursales | `branches` (name, address, manager_name, notary_name, phone, `is_active`, `company_id`). **Sin código visible**, sin timezone, sin estado formal; borrado protegido solo por reglas de la ruta (`routes/branches.py`) |
| Cajas | `cash_boxes`: **una por sucursal** (`branch_id UNIQUE`), `initial_balance`, y todo el runtime de caja (`cash_sessions`, movimientos, entregas, custodia…) en `cash_service.py` |
| Moneda | No existía el concepto: importes en "RD$" implícito; `currency_symbol` en `company_settings`; USD solo en planes/PayPal |
| Timezone | Constante global `America/Santo_Domingo` en `cash_service.TZ`, usada también por reportes; sin configuración por tenant/sucursal |
| Alcance por sucursal | `users.branch_id` (sucursal asignada, FK simple), reglas por rol en `cash_service.scope`; en T-002 `user_role_assignments.branch_id` (FK simple, validado solo en código) |
| Integridad cross-tenant | Solo por código (`WHERE company_id = ...`); sin FKs compuestas |
| Migraciones | Baseline `0001`…`0004` (T-001/T-002) |
| Datos | Solo de prueba (ADR-007 `RESET_AND_RESEED`) |
| Tests afectados | ~14 referencias a branches/companies en `test_api`, `test_cash`, `test_cash_refactor`; `make_tenant` de los tests T-002 |

## 2. Modelo

```text
Tenant (companies) 1 ── N Branch (branches) 1 ── N CashPoint (cash_points)
Tenant N ── N Currency  (tenant_currencies)         CashPoint N ── N Currency (cash_point_currencies, opcional)
```

* **Tenant = `companies`** (la tabla conserva su nombre para no romper 22 FKs; `Tenant` es el concepto). Se añadieron `status` (`active|inactive`, `is_active` queda como vista híbrida para el código legacy), `base_currency_code`, `default_timezone` (IANA, defecto `America/Santo_Domingo`) y `updated_at`. **`code` = `slug`** (único en la plataforma; ver T-002). Un tenant inactivo no acepta operaciones autenticadas nuevas (`403 tenant_inactive`; el login ya no resuelve su slug) y nunca se borra su historia.
* **Branch** (`branches`, ampliada): `code` visible **único por tenant** (`uq_branches_tenant_code`, mayúsculas, 1-20 caracteres), `status`, `timezone_override` (IANA, opcional), `updated_at`. `UNIQUE(company_id, id)` es el destino de las FKs compuestas que impiden referencias cross-tenant.
* **CashPoint** (`cash_points`, nueva): `tenant_id`, `branch_id`, `code` (único por tenant), `name`, `status` (`active|inactive|suspended`), `suspended_at/suspension_reason`. FK compuesta `(tenant_id, branch_id) → branches(company_id, id)`: una caja nunca puede apuntar a la sucursal de otro tenant. **Branch 1:N CashPoints** (sin unicidad por sucursal). `cash_point_currencies` limita opcionalmente las monedas de una caja (vacío = todas las del tenant) con FK compuesta a `tenant_currencies`.
* **Currency** (`currencies`): catálogo ISO 4217 sembrado por migración (DOP, USD, EUR), `exponent` (decimales, `0..4`, check) como regla de precisión. **Sin tasas de cambio ni conversión** (DR-022: paquete FX posterior). Sin `float` en el paquete (verificado por test).
* **Moneda base y habilitadas**: `tenant_currencies` (PK tenant+moneda, `enabled_at/by`, `disabled_at/by`; **deshabilitar conserva la fila**: la historia sigue consultable). La moneda base debe existir en `tenant_currencies` por una **FK compuesta diferida** (`companies(id, base_currency_code) → tenant_currencies`) y el servicio exige además que esté **habilitada**; no se puede deshabilitar la moneda base. Un listener `after_insert` de `Company` habilita la moneda base (y siembra el catálogo) para cualquier ruta que cree tenants (registro legacy, `/companies`, scripts, tests).

### Reconciliación con el modelo legacy (sin romper T-002)
* `company_id` se conserva (es el tenant). `persons.tenant_id`, `roles.tenant_id`, `user_role_assignments.tenant_id`, `users.company_id` siguen apuntando a `companies`.
* Nuevas FKs compuestas tenant-safe: `users(company_id, branch_id)`, `user_role_assignments(tenant_id, branch_id)` y `(tenant_id, cash_point_id)`. Un usuario/alcance no puede referenciar una sucursal o caja de otro tenant ni siquiera con SQL directo.
* Nuevo alcance **`cash_point`** en las asignaciones (`scope_kind` ∈ `tenant|branch|cash_point|own`, `cash_point_id`, check de consistencia y unicidad parcial) además del de sucursal; `Principal.allows(..., branch_id, cash_point_id)`, `holds_for_cash_point` y el techo de delegación lo respetan (un titular de sucursal no puede conceder más de su sucursal, uno de caja no más que su caja).
* Legacy `cash_boxes` **no se toca** (el runtime de Caja no cambia). La migración crea **una CashPoint moderna por cada `cash_box` existente** (`CAJA-<id box>`), sin FK entre ambos: la reconciliación runtime queda para el paquete de Caja.
* Rutas v1 de sucursales/empresas siguen funcionando: `is_active` es una vista de `status`; `code` toma un valor por defecto; el borrado de empresa vacía limpia además sus filas de identidad.

## 3. Timezone y configuración efectiva

`branch.timezone_override ?? tenant.default_timezone`, siempre IANA (los offsets `UTC-4`, `GMT+4`, `-04:00` y nombres inventados se rechazan con 422). Un único resolvedor (`organization/resolver.py`, solo lectura) calcula tenant, sucursal, zona efectiva y su origen, moneda base y monedas habilitadas; el contexto de request se enlaza con él.

**Request context** (T-001/T-002 extendido): tras autenticar, `bind_context` fija `tenant_id`, `branch_id` (sucursal asignada del usuario), `timezone` efectiva y `base_currency`, **siempre desde la sesión/BD**; cabeceras como `X-Tenant-ID`, `X-Branch-ID`, `X-Timezone`, `X-Base-Currency` se ignoran (probado). Una sucursal operativa distinta de la asignada (cobro cross-branch) deberá pasarse explícitamente y validarse contra el tenant y el alcance (el modelo ya lo permite: ver §6).

## 4. API v2 (`/api/v2`)

`GET /tenants/current`, `PUT /tenants/current/settings` (timezone/moneda base), `GET /tenants/current/effective[?branch_id]`;
`GET|POST /branches`, `GET|PUT /branches/{id}`, `POST /branches/{id}/disable|enable`;
`GET|POST /cash-points`, `GET /cash-points/{id}`, `PUT /cash-points/{id}/currencies`, `POST /cash-points/{id}/disable|enable|suspend|resume`;
`GET /currencies`, `GET|POST /tenant/currencies`, `DELETE /tenant/currencies/{code}` (deshabilita).
Entradas con `extra="forbid"` (un `tenant_id` en el payload es 422). Los ids de otro tenant devuelven 404. **Ningún GET escribe** (probado con un listener SQL: 0 INSERT/UPDATE/DELETE durante 13 lecturas).

## 5. Autorización (integración con T-002)

Nuevos permisos del catálogo (migración `0005`, concedidos a los roles "Administrador de agencia" existentes): `tenant.settings.read|manage`, `organization.branches.read|manage`, `cash.points.read|manage|suspend`, `currencies.read|manage`. Reglas:
* Crear una sucursal exige autoridad tenant-wide; administrar/leer una concreta basta con un grant de esa sucursal. Un grant de sucursal **no** abre otras sucursales ni las operaciones tenant-wide; un grant de caja solo alcanza esa caja. Tener el permiso funcional no implica acceso a todas las sucursales.
* Listados filtrados por alcance; sin ningún grant del permiso = 403 (deny-by-default), no una lista vacía.
* Capacidades de plataforma y de agencia siguen separadas: un principal de plataforma recibe 403 en todos los endpoints de agencia.

## 6. Preparación del cobro cross-branch (sin implementar pagos)

El modelo permite el futuro flujo de DR-024 sin decisiones que lo impidan: la deuda pertenece al tenant (no a una sucursal), `branches` y `cash_points` son tenant-safe y no hay propiedad exclusiva préstamo↔sucursal. Un pago futuro podrá registrar `loan_origin_branch_id`, `receiving_branch_id`, `receiving_cash_point_id` apuntando a `(tenant_id, branch_id)`/`(tenant_id, cash_point_id)` (claves compuestas ya disponibles). `ensure_cash_point_usable(tenant, cash_point, currency)` es la compuerta para nuevas sesiones/operaciones: tenant, sucursal y caja deben estar activos, la moneda habilitada para el tenant y admitida por la caja.

## 7. Suspensión de caja (DR-007)

Estado `suspended` solo mediante `POST /cash-points/{id}/suspend` (permiso propio `cash.points.suspend`, motivo obligatorio, auditado) y `resume`; **ninguna ruta de código lo establece automáticamente** y una diferencia de cierre pendiente no cambia el estado (probado con una sesión legacy en `closing_review` con diferencia y con un escaneo estático). Check de BD: `(status='suspended') = (suspended_at IS NOT NULL)`. No hay runtime de caja.

## 8. Auditoría

Mismo registro `security_events` (append-only) con `actor`, `tenant`, `correlation_id`, `occurred_at` y **before/after** (el limpiador de detalles ahora admite instantáneas anidadas y ya no descarta claves legítimas como `currency`): `org.tenant.settings_changed`, `org.branch.created|updated|timezone_changed|disabled|enabled`, `org.cash_point.created|disabled|enabled|suspended|resumed|currencies_changed`, `org.currency.enabled|disabled`.

## 9. Seeds

`app/modules/organization/seed.py` / `scripts/seed_organization.py` (idempotente, solo datos de prueba): 2 tenants (`dominicana`: `America/Santo_Domingo`, base DOP, DOP+USD; `nueva-york`: `America/New_York`, base USD, USD+DOP), 2 sucursales por tenant, ≥1 caja por sucursal (una con 2).

## 10. Migración `0005`

Crea `currencies` (+semilla), `tenant_currencies`, `cash_points`, `cash_point_currencies`; amplía `companies`, `branches`, `user_role_assignments`; retira `companies.is_active` y `branches.is_active` (rellenando `status`); `branches.code` = `SUC-<id>`; habilita DOP en los tenants existentes; crea una caja por cada `cash_box`; añade permisos y los concede a los administradores de agencia existentes. Probada con datos legacy en una BD desechable (upgrade, `alembic check`, downgrade a 0004 restaurando `is_active`, re-upgrade).

## 11. Decisiones dentro de la libertad de T-003

* La tabla `companies` se conserva como Tenant (renombrarla rompería 22 FKs sin beneficio).
* Código de caja único por tenant (no por sucursal) para que sea un identificador visible inequívoco.
* `suspended` se incluye (el paquete lo permite) con semántica documentada y control explícito.
* EUR en el catálogo además de DOP/USD (sin tasas).

## 12. Limitaciones conocidas

* No hay runtime de Caja (sesiones, aperturas, movimientos, arqueo, saldos por moneda), FX, pagos, ni conversión.
* La sucursal "operativa" de una petición (distinta de la asignada) se resolverá en los paquetes de Caja/Pagos; hoy el contexto usa la sucursal asignada.
* El cambio de moneda base/zona no recalcula nada (no hay datos monetarios que reinterpretar todavía); las reglas de transición de historia llegan con Caja/Crédito.
* Edición de `code` de sucursal/caja no se expone (cambiar identificadores visibles requiere decidir su política).
* Los esquemas legacy (`cash_boxes`, `company_settings.currency_symbol`) siguen existiendo hasta que se reconstruyan sus módulos.
