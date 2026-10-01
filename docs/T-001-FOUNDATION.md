# T-001 — Foundation Backend (documentación técnica)

Base: ADR-001 (monolito modular), ADR-002 (multi-tenancy), ADR-003 (PostgreSQL only), ADR-006 (tiempo), ADR-007 (convergencia).
Ramas: `main` → `refactor/backend-v2` → `feat/t001-foundation` (EXECUTION-PREP §5; T-001 sugería `refactor/foundation-v2`, se siguió la convención más específica).

## Estructura

```text
app/core/config/    Settings tipados (pydantic-settings), validación por entorno
app/core/db/        Base (naming convention), engine, SessionLocal, get_session, session_scope, check_connection
app/core/context/   RequestContext (contextvars) + RequestContextMiddleware (correlation id)
app/core/errors/    AppError + subclases, envelope uniforme, handlers
app/core/logging/   JSON estructurado con redacción de secretos
app/core/time/      now_utc, get_zone, to_zone, business_date, parse_aware, ensure_aware
app/api/system.py   /health (liveness) y /ready (PostgreSQL)
app/modules/*       Fronteras vacías del monolito modular (sin implementación)
app/shared/         Kernel compartido (vacío)
app/core/database.py  Alias de compatibilidad para imports legacy (`from app.core.database import Base, ...`)
```

`app/core/security.py` se mantiene como módulo (no paquete) para no romper imports legacy; su reestructura corresponde a T-002.

## Configuración

`Settings` (`app/core/config/settings.py`). Entornos: `development | test | staging | production`.
`DATABASE_URL` es **obligatoria** y debe ser PostgreSQL (`postgres://`, `postgresql://` y `postgresql+psycopg2://` se normalizan a `postgresql+psycopg://`; cualquier otro esquema falla al arrancar).
En `staging`/`production` se exige `SECRET_KEY` ≥ 32 caracteres no por defecto, `DEBUG=false` y `TRUSTED_HOSTS` explícito (sin `*`).
`DEFAULT_TIMEZONE` debe ser IANA (`America/Santo_Domingo`). Los secretos están excluidos de `repr()`.
El código de negocio no ramifica por entorno.

## Sesión y transacciones

Una `Session` por request (`get_session`, expuesta como `get_db` legacy) o por unidad de trabajo (`session_scope`).
Commit explícito del caso de uso; rollback ante error y al salir; cierre siempre. Conexiones con `pool_pre_ping`, `connect_timeout` y `timezone=UTC` de sesión.

## Alembic

Baseline limpia `0001` generada contra PostgreSQL (esquema actual de los modelos). Las 22 migraciones históricas (escritas para SQLite/esquema legacy y no ejecutables sobre una BD vacía) y `scripts/prepare_database.py` se eliminaron; siguen en el historial git.
`Base.metadata` usa naming convention. `downgrade` elimina también los tipos ENUM de PostgreSQL.
**La API ya no crea tablas al arrancar**: `alembic upgrade head` es el único camino.

## Contexto y correlation id

`X-Correlation-ID`: se acepta si coincide con `^[A-Za-z0-9._-]{8,128}$`; si no, se genera (uuid4 hex). Se devuelve en la respuesta, aparece en logs y en el cuerpo de error. `tenant_id`, `branch_id`, `actor_id` y `timezone` del contexto **nunca** se leen de cabeceras del cliente; los llenará el resolver autenticado (T-002/T-003) con `bind_context`.

## Errores

`{"error": {"code", "message", "correlation_id", "details"?}, "detail": ...}`.
Códigos: `validation_error`, `authentication_failed`, `authorization_denied`, `not_found`, `conflict`, `idempotency_conflict`, `business_rule_violation`, `service_unavailable`, `internal_error` (+ `bad_request`, `method_not_allowed`, `rate_limited`, `request_error` para `HTTPException` genéricas).
`detail` se conserva solo como alias transitorio del formato legacy para web/móvil; en 5xx es siempre genérico. Los errores de validación no devuelven `input`. Una excepción no controlada responde 500 genérico con el correlation id y se re-lanza (como Starlette) para que servidor/tests la vean.

## Logging

JSON por línea: `timestamp, level, message, module, environment, correlation_id, tenant_id, user_id` + extras. Se redactan claves sensibles (password, token, secret, authorization, api_key, cookie, credential, dsn, database_url) y credenciales en URLs / `Bearer`. Se registra una línea `request_completed` (método, ruta sin query, estado, duración); no se registran cuerpos.

## Health / readiness

`GET /health`: proceso vivo, sin dependencias. `GET /ready`: `SELECT 1` en PostgreSQL; 503 `service_unavailable` sin detalles de conexión. Ambos son de solo lectura. (`/api/v1/health` legacy se conserva.)

## Pruebas

`TEST_DATABASE_URL` obligatoria (PostgreSQL, nombre terminado en `_test`; `tests/pg_env.py` lo exige porque las suites legacy hacen `drop_all`).
`tests/test_t001_foundation.py` cubre T001-01…T001-12 (las migraciones se prueban en bases desechables creadas con `CREATE DATABASE`, requiere un rol con `CREATEDB`).
Suites legacy migradas a PostgreSQL sin cambiar lógica de negocio. Cambios en tests: (1) cabecera de `test_api.py` usa `pg_env`; (2) se eliminó `test_customer_profile_migration_preserves_legacy_records` (ejecutaba una migración histórica sobre SQLite en memoria; la migración ya no existe); (3) `test_cash*.py` obtenían el admin con `/users[0]`, que dependía del orden binario de SQLite: ahora se busca por rol; (4) se eliminó `tests/test_database_config.py` (probaba SQLite y `prepare_database.py`).

Comandos:

```bash
docker run -d --name moneyfast-pg -e POSTGRES_USER=moneyfast -e POSTGRES_PASSWORD=moneyfast -e POSTGRES_DB=moneyfast_dev -p 127.0.0.1:55440:5432 postgres:16
docker exec moneyfast-pg psql -U moneyfast -d moneyfast_dev -c "create database moneyfast_test"
export DATABASE_URL=postgresql+psycopg://moneyfast:moneyfast@127.0.0.1:55440/moneyfast_dev
export TEST_DATABASE_URL=postgresql+psycopg://moneyfast:moneyfast@127.0.0.1:55440/moneyfast_test
uv run alembic upgrade head
uv run python -m pytest -q
uv run ruff check && uv run ruff format --check
```

## CI

`.github/workflows/ci.yml`: PostgreSQL 16, ruff check/format, `alembic upgrade head`, `alembic check`, downgrade/re-upgrade y pytest (incluye la guarda de motor único). Ya no se hace `create_all + stamp`.

## Dependencias

| DEPENDENCY | VERSION | PURPOSE | WHY_REQUIRED | ALTERNATIVES | NOTES |
|---|---|---|---|---|---|
| ruff (dev) | 0.16.9 (`>=0.16.9`) | lint + format | CI de lint/format exigido por T-001 §16; no existía ninguna herramienta | flake8+black+isort (3 herramientas) | Mantenida activamente; solo dev, no entra en runtime. Alcance limitado al código T-001 (`[tool.ruff] include`) |

Sin dependencias de runtime nuevas (stdlib: `logging`, `contextvars`, `zoneinfo`).

## Decisiones tomadas dentro de la libertad de T-001

- SQLAlchemy 2.0 (ya instalado) en lugar de migrar a SQLModel: TECH-MASTER permite "SQLModel/SQLAlchemy o equivalente compatible con el código existente".
- IDs de contexto como `str`: la estrategia de IDs sigue abierta (T-001 §19).
- Alias `app.core.database` para no tocar 25 modelos legacy.
- `typecheck`: no hay herramienta configurada en el repositorio; no se introdujo una (sin spec que la pida).

## Limitaciones conocidas / acciones del propietario

- **Migraciones / bases existentes (decisión confirmada):** `RESET_AND_RESEED` para los entornos actuales de prueba (Fast Money aún no está en producción). No se mantiene compatibilidad con la cadena Alembic legacy; la baseline `0001` es la base de la convergencia v2. Esto NO se extrapola a futuras bases de producción. `railpack.json` ejecuta `alembic upgrade head`; en `staging`/`production` deben definirse `SECRET_KEY` y `TRUSTED_HOSTS` explícitos (la app se niega a arrancar si no).
- El `.env` local con `DATABASE_URL=sqlite:///...` deja de funcionar; apuntarlo a PostgreSQL.
- La lógica legacy no cambió: mora/estados siguen usando fecha UTC (`loan_service.py`), el reverso de Caja no corrige Crédito, etc. Pertenece a paquetes posteriores.
- `app/services/email_service.py` imprime el código de recuperación en logs de desarrollo (legacy, fuera de T-001).
- Los clientes web/móvil leen `detail`; se conserva el alias hasta su convergencia.

## Deuda explícita para T-002 (seguridad)

- **Código de recuperación en logs**: `app/services/email_service.py` imprime/registra el código de recuperación cuando SMTP no está configurado. No lo introdujo T-001 y no bloquea la fundación, pero **MUST** eliminarse/corregirse en Identity/Auth: ningún recovery code, token o secreto puede terminar en logs en la arquitectura nueva. (El `debug_code` devuelto por `/auth/request-password-reset` fuera de producción también debe revisarse.)
- Otros hallazgos de la auditoría que corresponden a T-002: límite de intentos de login/recuperación, enumeración de usuarios en `verify-reset-code`, tokens en `localStorage`/`AsyncStorage`.
- `app/services/cash_service.py` tiene un diff local sin confirmar, ajeno a T-001, que se revisará en el paquete de Caja.

