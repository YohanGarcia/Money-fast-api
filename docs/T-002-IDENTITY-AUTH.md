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
* El identificador de login es **tenant-aware** (migración `0003`): unicidad `(company_id, email)` para cuentas de agencia y unicidad separada para cuentas de plataforma (`company_id NULL`); el email se guarda normalizado (`lower(btrim)`, check `email_normalized`). Ya no hay unicidad global.
* Tenant = `companies.id` hasta T-003. Usuario de plataforma = `company_id NULL`; sin endpoints de plataforma en T-002.
* No se implementó MFA (hooks: `SecretNotifier`, estado `pending`, servicio de auth); sin impersonación ni break-glass ni cuentas de servicio.
* Sin dependencias nuevas (Argon2/JWT ya existían).

## 11. Limitaciones conocidas

* `security_version` no se implementó: la revocación se hace por filas de sesión, que ya se evalúan en cada request.
* Los contadores de `auth_throttle` no se purgan automáticamente (no hay jobs aún).
* Roles: crear/archivar sí; **editar la composición** de un rol (y su historia) queda para un paquete posterior.
* Los endpoints legacy (v1) de otros módulos siguen usando `require_roles`; su reemplazo por `require_permission` ocurre al reconstruir cada módulo.
* Web/Mobile conservan tokens en `localStorage`/`AsyncStorage`; v2 los entrega en el cuerpo JSON y su almacenamiento seguro es parte de la convergencia de esas superficies.

## 12. Resolución de tenant explícita (contrato aprobado) y Google OIDC

### 12.1 Contrato
| Flujo | Entrada |
|---|---|
| Login con contraseña | `tenant_slug` + `email` + `password` (`POST /api/v2/auth/login`) |
| Recuperación | `tenant_slug` + `email` (`POST /api/v2/auth/recovery/request`); completar usa solo el token (ligado a la cuenta) |
| Google | `tenant_slug` + id_token de Google + nonce emitido por el backend |

* `companies.slug` (migración `0004`): único, formato `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`, palabras reservadas rechazadas, sin distinguir mayúsculas. Los tenants existentes reciben `nombre-id`; el registro v1 genera uno único o acepta `tenant_slug` opcional (409 si está tomado, 422 si es inválido).
* El slug se resuelve **antes** de buscar la cuenta (`resolve_account`): unicidad `(company_id, email)`, así que el mismo correo existe en varios tenants y se autentica de forma determinista. Un slug inexistente o inactivo es indistinguible de un correo desconocido (misma respuesta, mismo throttling, mismo tiempo gracias al hash ficticio). Sin slug = espacio de cuentas de **plataforma** (`company_id NULL`), nunca una cuenta de agencia.
* Los contadores de throttle usan el espacio `slug|email`, de modo que los fallos en una agencia no bloquean el mismo correo en otra.
* La API legacy `/api/v1/auth/login` no tiene contexto de tenant (no se modifican Web/Mobile): resuelve solo si el correo es único y **falla cerrado** si está duplicado entre tenants (`find_unambiguous_account`). Desaparecerá al converger los clientes. **TENANT-LOGIN-05 queda cerrado para v2** con el slug.
* `POST /api/v1/auth/register` y `POST /api/v1/companies` conservan el 409 global por correo (crean un tenant sin contexto previo); señal de existencia heredada a cerrar con verificación de correo en el onboarding.

### 12.2 Google (OpenID Connect) — `app/modules/identity/{oidc,google}.py`
* **El backend solo verifica** el ID token: firma RS256 contra el JWKS del proveedor (allow-list de algoritmos: rechaza `none` y HS*), `iss` (`https://accounts.google.com` / `accounts.google.com`), `aud` = `GOOGLE_CLIENT_ID`, `exp` (con tolerancia configurable) y `sub`. No intercambia códigos ni guarda secretos de cliente ni tokens de Google (`external_identities` no tiene columnas de token). Los secretos nunca viajan a Web/Mobile.
* **Nonce/anti-replay**: `POST /auth/google/challenge` emite un nonce de un solo uso (solo se guarda su SHA-256; 5 min), ligado a tenant y propósito (`login` o `link`; `link` además al usuario). El claim `nonce` del token debe coincidir y el reto se consume atómicamente. Para un slug desconocido se devuelve un nonce sin guardarlo (misma respuesta). PKCE/`state` corresponden al cliente en el flujo de redirección; el backend, al no canjear códigos, no los necesita.
* **Identidad**: `external_identities(tenant_id, user_id, provider, issuer, subject)` con unicidad parcial de vínculos activos: una identidad Google ↔ una cuenta por tenant y una identidad Google por cuenta. La misma identidad puede vincularse en tenants distintos (cuentas distintas). El correo de Google es solo metadato informativo: **nunca** se usa para encontrar ni vincular cuentas.
* **Vinculación explícita** (`POST /auth/google/link/challenge` + `POST /auth/google/link`): requiere sesión autenticada de la cuenta destino y un nonce emitido para ese usuario; repetir es idempotente; otra cuenta → 409 `external_identity_conflict`. `DELETE /auth/google/link` revoca (la fila se conserva).
* **Login** (`POST /auth/google/login`): tenant por slug → vínculo activo → cuenta. Estado `disabled`/`locked` ⇒ 403 aunque Google autentique; `pending` o sin vínculo ⇒ 401 `external_identity_not_linked` (también para slugs inexistentes). Crea la misma sesión server-side que la contraseña (revocable, refresh con detección de reutilización) y audita (`auth.login.succeeded` con `method=google`, `identity.google.linked|unlinked`); los eventos no contienen `sub` ni correo. Los fallos suman al mismo backoff por IP.
* La contraseña sigue soportada; MFA fuera de alcance (DR-009 abierto).
* **BLOCKED_BY_EVIDENCE**: la conexión real con Google requiere `GOOGLE_CLIENT_ID` y salida a Internet (JWKS), que no están disponibles aquí. Sin configurar, los endpoints responden 503 `service_unavailable`. Toda la lógica de decisión se probó con un proveedor falso (clave RSA, JWKS, issuer y audience propios).
* No implementado (fuera del alcance de este paquete): "crear agencia con Google" (onboarding de tenant: crea tenant + admin inicial; requiere decidir verificación de correo/abuso) y alta de usuarios invitados mediante Google. Un usuario nuevo sin vínculo previo no puede entrar con Google.
