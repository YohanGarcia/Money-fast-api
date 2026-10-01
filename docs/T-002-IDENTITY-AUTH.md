# T-002 — Identity & Authorization Foundation (documentación técnica)

Base: ADR-004, DR-008 (autorización), DR-010 (abuso de autenticación), DR-023 (identidad de persona), DF-09, ADR-002, ADR-003, ADR-006.
Rama: `feat/t002-identity-auth` desde `refactor/backend-v2 @ fa0c147` (tags `T001_APPROVED`, `T001_MERGED_TO_BACKEND_CONVERGENCE`).

## 1. Baseline previo (estado legacy observado antes de T-002)

| Área | Estado encontrado |
|---|---|
| Modelos | `User` (tabla `users`: `email` único global, `role` enum fijo de 5 valores, `is_active` bool, `company_id` = tenant, sin persona), `UserSession`, `PasswordResetCode` |
| Roles | `superadmin/admin/manager/collector/cashier` en columna; `require_roles()` compara nombres; el cajero se limita con una lista de prefijos de URL en `get_current_user` |
| Permisos | No existían (autorización = nombre de rol) |
| Login | `POST /api/v1/auth/login`: verifica Argon2 (`pwdlib.recommended`), sin límite de intentos, mismo mensaje para usuario inexistente/clave errónea pero sin igualar tiempos |
| Sesiones | JWT HS256 access (30 min) + refresh (7 d) con `sid`; sesión server-side con `is_active`; refresh rota hash pero sin detectar reutilización ni expiración propia; logout por refresh token |
| Recuperación | Código de 6 dígitos (15 min): `request-password-reset` devolvía `debug_code` fuera de producción sin SMTP y **imprimía el código en logs**; `verify-reset-code` respondía 404 si el correo no existía (enumeración); sin límite de intentos; token JWT de reset reutilizable hasta expirar |
| Auditoría | Ninguna para identidad |
| Tests | ~8 tests de auth en `test_api.py` (sesión/registro/debug_code) |
| Clientes | Web guarda `access_token`/`refresh_token` en `localStorage` (`AuthContext.tsx`), móvil en `AsyncStorage` (`apiClient.ts`). Se documenta como contrato legacy; **no se modificó Web/Mobile** |

Vulnerabilidades observadas (de la auditoría): fuerza bruta de login/código, enumeración, secreto de recuperación en logs, `debug_code`, ausencia de permisos/alcance/techo de delegación, `role` editable sin auditoría, sin revocación por reutilización de refresh.

## 2. Modelo (`app/modules/identity/models.py`)

`Person != UserAccount != Role != Permission`

* **Person** (`persons`): identidad común (nombres, documento opcional, tenant). Sin credenciales ni permisos. Sin reglas de deduplicación (T-004).
* **UserAccount** (tabla `users`, conservada por compatibilidad de FKs; alias `app.models.user.User`): credenciales + ciclo de vida. `person_id` opcional (los usuarios legacy se enlazan a una persona al crearse), `status` ∈ `pending | active | locked | disabled` (autoritativo), `activated_at`, `last_login_at`, `password_changed_at`, `locked_at/locked_until`, `disabled_at`. `is_active` es ahora una vista híbrida de `status` solo para código legacy. `role` (enum legacy) pasa a **nullable**: los usuarios v2 no tienen privilegios legacy.
* **Permission** (`permissions`): código estable `recurso.acción`, `scope_kind` `tenant|platform`, `is_sensitive`. Catálogo definido en código (`catalog.py`) y sembrado por la migración `0002`; solo permisos de seguridad (12). No se inventaron permisos de Crédito/Caja.
* **Role** (`roles`): `tenant_id` (NULL = rol de plataforma), nombre, `system_defined`. Unicidad tenant-aware por índices parciales (`uq_roles_tenant_name`, `uq_roles_platform_name`).
* **RolePermission**, **UserRoleAssignment** (`user_role_assignments`): alcance `tenant | branch | own`, `branch_id`, `assigned_by`, `revoked_at/revoked_by`. La revocación conserva la fila (historia). Unicidad parcial de asignaciones activas.
* **RecoveryToken**, **AuthThrottle**, **SecurityEvent** (ver abajo). El tenant sigue siendo la fila de `companies` hasta T-003.

## 3. Autenticación

* **Hashing**: Argon2id (pwdlib), parámetros configurables (`PASSWORD_HASH_TIME_COST`, `_MEMORY_KIB`, `_PARALLELISM`; defaults de argon2-cffi), rehash transparente en login. Política de contraseña: longitud 10–128 configurable, sin reglas de composición ni rotación forzada (DF-09 §9).
* **Sesiones**: access JWT (30 min) + refresh JWT (7 d) con `sid`; autorización siempre contra el estado actual en BD (usuario `active`, sesión activa, no expirada, no revocada). El JWT solo lleva ids; tenant/permisos nunca se leen del token. Refresh rota el token; **reutilizar un refresh superado revoca la sesión** (`session.refresh_reuse_detected`). Sesiones tienen `expires_at`, `revoked_at`, `revoked_reason`.
* **Login** (`POST /api/v2/auth/login`, y el legacy `/api/v1/auth/login` comparte el mismo servicio): (1) puertas de throttle uniformes, (2) verificación con hash ficticio si la cuenta no existe, (3) errores específicos (`account_disabled`/`account_locked`) solo **después** de que la contraseña sea correcta, (4) evento de auditoría.
* **Logout / revocación**: logout de la sesión actual; revocar todas las sesiones de un usuario (`security.sessions.revoke`); revocación automática al desactivar, bloquear, cambiar contraseña (resto de sesiones) y completar recuperación (todas).

## 4. Anti-abuso (DR-010)

Backoff exponencial respaldado en PostgreSQL (`auth_throttle`, sin dependencias externas, seguro con varios workers; claves = HMAC del identificador, nunca email/IP en claro). Los intentos mientras está bloqueado **no se cuentan** (no se puede extender el bloqueo) y el retraso está **acotado** (`AUTH_THROTTLE_MAX_SECONDS`, 900 s): nunca hay bloqueo permanente.

| Contador | Umbral (defecto) | Efecto |
|---|---|---|
| login por (cuenta, IP) | 5 fallos | 429 + `Retry-After`, 30 s × 2^n |
| login por IP | 50 fallos | idem |
| login por cuenta | 10 fallos | `status=locked` temporal (900 s), sesiones revocadas, auto-desbloqueo |
| recuperación: solicitudes por IP | 20 | 429 |
| recuperación: solicitudes por cuenta | 3 | **silencioso**: misma respuesta 202 pero no se emite token |
| recuperación: finalizaciones fallidas por IP | 10 | 429 |

La IP es la del socket (`request.client.host`); las cabeceras `X-Forwarded-For` **no** se confían. Detrás de un proxy hay que usar el soporte de proxy del servidor restringido a la dirección del proxy.

## 5. Recuperación y activación

* Token opaco de 256 bits; en BD solo su SHA-256; un solo uso (consumo atómico `UPDATE … WHERE used_at IS NULL … RETURNING`, probado con concurrencia); expiración (15 min recuperación, 72 h activación); una solicitud nueva revoca los tokens anteriores sin usar.
* `POST /auth/recovery/request` responde siempre `202` con el mismo cuerpo exista o no la cuenta (o esté desactivada). La entrega ocurre **después** de responder (`BackgroundTasks`), así que el tiempo de respuesta no distingue.
* `POST /auth/recovery/complete`: valida política antes de consumir, fija contraseña, activa cuentas `pending`/`locked`, revoca todas las sesiones, audita. Errores: `invalid_recovery_token`, `recovery_token_reused`, `password_policy_violation`.
* **El secreto solo viaja por el `SecretNotifier`** (SMTP por defecto). Nunca va a logs, stdout, respuestas, `debug_code` ni auditoría. Si SMTP no está configurado simplemente no se entrega y se audita `recovery.delivery_failed` (sin el secreto). `PendingDelivery.repr` oculta el secreto.
* Alta de usuario (`POST /users`): crea Person + cuenta `pending` con contraseña desconocida e invitación de activación; **no concede privilegios** (la asignación de roles es un paso aparte). El administrador nunca conoce ni lee la contraseña.

## 6. Autorización (deny-by-default)

* `Principal` se reconstruye de la BD en cada request (`effective_grants`): asignaciones activas → rol activo del **mismo tenant** (o rol de plataforma para usuarios de plataforma) → permisos del mismo `scope_kind`. Una fila corrupta que apunte a un rol de otro tenant no concede nada.
* `Principal.allows(permiso, tenant_id, branch_id, owner_id)`: DENY salvo concesión existente de un permiso del catálogo con alcance compatible (`tenant` = todo el tenant, `branch` = solo esa sucursal, `own` = solo recursos propios). Permiso desconocido = DENY.
* Capacidades de **plataforma y de agencia separadas**: un permiso tiene un solo `scope_kind`; un rol de agencia no puede contener permisos de plataforma (404) y los principals de plataforma no ejercen permisos de agencia.
* `require_permission("x.y")` es la dependencia reutilizable (FastAPI); los casos de uso vuelven a comprobar el objetivo (tenant/rol/usuario). Los ids de otro tenant responden **404**, igual que los inexistentes.
* El contexto (`actor_id`, `tenant_id`, `branch_id`) se enlaza con `bind_context` solo desde la sesión autenticada; cabeceras como `X-Tenant-ID`/`X-User-ID` se ignoran, y los payloads con `tenant_id` se rechazan (422, `extra="forbid"`).
* **Techo de delegación** (`assert_within_ceiling`): quien asigna/crea un rol debe poseer todos sus permisos con al menos el mismo alcance (tenant-wide para asignaciones tenant/own; tenant-wide o la misma sucursal para asignaciones de sucursal). También aplica a quitar un rol y a desactivar a un usuario con más privilegios. **Autoescalamiento**: nadie (administradores incluidos) puede asignarse roles ni desactivarse a sí mismo.

## 7. Auditoría de seguridad

`security_events`: append-only (trigger `BEFORE UPDATE OR DELETE` en PostgreSQL), `correlation_id` de T-001, `actor_id`/`subject_id` sin FK (evidencia histórica), `details` JSONB **filtrado** (se descartan claves tipo password/token/hash/code/key y se limpian valores). Eventos: `auth.login.succeeded|failed|denied`, `auth.logout`, `account.locked|unlocked`, `recovery.requested|completed|failed|delivery_failed`, `password.changed|change_failed`, `user.created|invited|disabled|enabled`, `role.created|assigned|removed`, `session.revoked`, `session.refresh_reuse_detected`, más `legacy.role_changed`. Los intentos bloqueados por throttle no se auditan (log) para que una inundación no haga crecer la tabla.

## 8. API v2 (`/api/v2`)

`POST /auth/login|refresh|logout|recovery/request|recovery/complete|password/change`, `GET /auth/me`; `GET|POST /users`, `GET /users/{id}`, `POST /users/{id}/disable|enable|roles|sessions/revoke`, `DELETE /users/{id}/roles/{role_id}`; `GET|POST /roles`, `GET /permissions`, `GET /security/events`. Errores con el envelope de T-001 (`detail` sigue como alias legacy).

## 9. Legacy retirado / adaptado

| Elemento legacy | Disposición |
|---|---|
| `/api/v1/auth/request-password-reset`, `verify-reset-code`, `reset-password`, `debug_code`, tabla `password_reset_codes`, `create_password_reset_token`, `send_reset_code_email` | **RETIRADO** (`email_service` ya no imprime códigos ni destinatarios) |
| `/api/v1/auth/login|refresh|logout|me` | **ADAPTADO** a los servicios nuevos (throttle, revocación, auditoría); mismos contratos |
| `/api/v1/auth/register` | **ADAPTADO**: crea además Person, rol de sistema "Administrador de agencia" y asignación |
| `get_current_user` | **ADAPTADO**: una sola ruta de autenticación; enlaza el contexto |
| `app/core/security.py` | **REBUILD** → paquete `app/core/security/` (`passwords`, `tokens`; mismos nombres exportados) |
| `User.is_active` | Vista híbrida de `status`; columna eliminada |
| `/api/v1/users` (alta/edición legacy con `role`) | **TRANSITORIO**: crea Person, audita cambios de rol/estado/contraseña, revoca sesiones al desactivar o cambiar contraseña; no puede editar usuarios v2 (`role` NULL). Se retirará con la convergencia de Web/Mobile |
| Roles legacy como autorización | Siguen solo para rutas legacy; **no** se migran como permisos |
| Datos legacy | `RESET_AND_RESEED` (aprobado); la migración `0002` rellena `status` desde `is_active` |

## 10. Decisiones tomadas dentro de la libertad de T-002

* `person_id` opcional en `users` (compatibilidad con filas/flujo legacy); obligatorio en el alta v2.
* `email` sigue siendo identificador de login único global (el login no recibe tenant).
* Tenant = `companies.id` hasta T-003. Usuario de plataforma = `company_id NULL`; sin endpoints de plataforma en T-002.
* No se implementó MFA (hooks: `SecretNotifier`, estado `pending`, servicio de auth); sin impersonación ni break-glass ni cuentas de servicio.
* Sin dependencias nuevas (Argon2/JWT ya existían).

## 11. Limitaciones conocidas

* `security_version` no se implementó: la revocación se hace por filas de sesión, que ya se evalúan en cada request.
* Los contadores de `auth_throttle` no se purgan automáticamente (no hay jobs aún).
* El alta de usuario con correo ya existente responde 409 también para correos de otro tenant (señal de existencia limitada a quien tiene `users.create`).
* Roles: crear/archivar sí; **editar la composición** de un rol (y su historia) queda para un paquete posterior.
* Los endpoints legacy (v1) de otros módulos siguen usando `require_roles`; su reemplazo por `require_permission` ocurre al reconstruir cada módulo.
* Web/Mobile conservan tokens en `localStorage`/`AsyncStorage`; v2 los entrega en el cuerpo JSON y su almacenamiento seguro es parte de la convergencia de esas superficies.
