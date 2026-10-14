# T-004 — Customer Foundation (documentación técnica)

Base: DF-02 (clientes y expedientes), DR-023 (identidad de persona), DR-024, DR-008, DR-029, DR-030, DECISION-REGISTER-v2.md, T-002 (persons), T-003 (tenant/branch).
Rama: `feat/t004-customer-foundation` desde `refactor/backend-v2 @ d4204bf` (tags `T003_APPROVED`, `T003_MERGED_TO_BACKEND_CONVERGENCE`).

## 1. Baseline previo (cliente legacy)

| Pieza | Estado encontrado | Clasificación |
|---|---|---|
| `customers` (tabla plana) | Mezcla identidad (full_name, document_id/document_key, birth_date String(10), nationality, marital_status), contacto (phone, home_phone, email), dirección (address libre + sector, calle, barrio, province, city, house_number, building, apartment, reference_note, lat/lon), referencias en `JSON` (máx. 3), notas, y **asignación operativa** (assigned_collector_id, route_id → Cobranza; cash_branch_id → Caja). Tenant = `company_id`; OCC con `version` | **REBUILD** como modelo nuevo; la tabla **sigue viva** (KEEP transitorio) porque `loans.customer_id` y `loan_applications.customer_id` la referencian |
| Identidad duplicada | `full_name`/`document_id` viven en la fila de cliente (segunda identidad); unicidad `(company_id, document_key)` pero con duplicados históricos con `document_key` NULL | TRANSFORM → `persons`; duplicados → MANUAL_REVIEW |
| Contactos | 2 teléfonos + 1 correo en columnas | TRANSFORM → `customer_contacts` |
| Direcciones | 1 dirección en columnas; sector y barrio ya separados; `city` = municipio | TRANSFORM → `customer_addresses` (sector/barrio separados) |
| Referencias | JSON `{nombre, telefono, cedula, direccion}` ≤3 | TRANSFORM → `customer_references` |
| Sucursal | Solo indirecta: `cash_branch_id` (Caja) y la sucursal de la ruta | DEFER/DO_NOT_MIGRATE: origen/gestión quedan vacíos (decisión humana) |
| Cobrador / ruta | En la ficha de cliente | **DEFER** a Cobranza (no pertenece al expediente, DF-02 §23) |
| Búsqueda v1 | En memoria: carga todos los clientes de la empresa y filtra en Python (`routes/customers.py`) | REMOVE de la ruta nueva: búsqueda server-side |
| Endpoints v1 `/api/v1/customers` (GET, POST, GET/{id}, PUT) | Usados por solicitudes, préstamos, caja, reportes, rutas; 114 referencias en `test_api` | **KEEP** congelados hasta reconstruir Crédito |
| Documentos identificativos | Solo el número (`document_id`); sin tipo, país ni vigencias | ADAPT (ver §3) |
| Migraciones | Hasta `0005` | `0006` añade el modelo nuevo; no toca `customers` |
| Datos | De prueba (ADR-007 `RESET_AND_RESEED`) | RESET_AND_RESEED por defecto; herramienta de transformación idempotente disponible |

## 2. Modelo

```text
Person (identidad común, T-002)  1 ── 0..1 ── CustomerProfile (rol cliente en un tenant)
CustomerProfile 1 ── N CustomerContact | CustomerAddress | CustomerReference | CustomerDuplicateFlag
```

* **`Person` ampliada** (identidad, no rol): `alias`, `birth_date`, `nationality`, documento (`document_type` ficha técnica libre normalizada, `document_number` tal como se ingresó, `document_number_normalized` derivado, `document_country`, `document_issue_date`, `document_expiry_date`) y `search_name` (derivado: nombre sin acentos ni mayúsculas, solo para comparar/buscar; el nombre original nunca se parte ni se reescribe). Los derivados los calcula un único listener (`before_insert/update`). Checks: documento consistente, tipo obligatorio si hay número, vigencia ≥ emisión. **Una persona por documento normalizado y tipo dentro de un tenant** (`uq_persons_tenant_document`, índice parcial) y `UNIQUE(tenant_id, id)` como destino de FKs tenant-safe.
* **`CustomerProfile`** (`customer_profiles`): `tenant_id`, `person_id`, `customer_code`, `status` (`pending|active|inactive`), `origin_branch_id`, `management_branch_id`, `marital_status`, `internal_note`, `version` (control de concurrencia optimista), `legacy_customer_id` (mapeo opcional a la fila plana), auditoría de autoría. **Sin** credenciales, permisos, deuda, préstamos ni datos laborales (test estructural). `UNIQUE(tenant_id, customer_code)` y `UNIQUE(tenant_id, person_id)`; FKs compuestas tenant-safe a `persons` y `branches` (origen y gestión). Una `Person` puede ser a la vez `UserAccount` (T-002) y cliente sin duplicarse.
* **Estados**: `pending` (alta; sin expediente validado) → `active` ↔ `inactive`. Activar/desactivar son operaciones explícitas con permiso propio; desactivar **no borra nada** y un cliente moroso no cambia de estado (el estado es del expediente, DF-02 §13). **`restricted/blocked` queda BLOCKED_BY_SPEC**: DF-02 §13 pide no inventar una máquina de estados material y no hay definición de qué bloquea.
* **Código** (`CLI-000001`): secuencia monótona por tenant (`tenant_sequences`, upsert atómico), nunca reutilizada; opcionalmente se puede indicar un código explícito (patrón técnico, único por tenant). El formato es configuración futura; no es control de seguridad ni clave primaria.
* **Contactos** (`phone|mobile|email|other`): valor original + `normalized_value`; **a lo sumo un contacto primario activo por tipo** (índice parcial único); el primero de cada tipo es primario; cambiar = desactivar + agregar (la fila vieja se conserva con quién y cuándo). Sin `UPDATE` de valor.
* **Direcciones** (`residence|work|business|mailing|other`): `sector` y `barrio` son columnas separadas; país, provincia, municipio, calle, número, edificio, apartamento, referencia, código postal; **latitud/longitud opcionales, juntas y solo si hay dirección textual** (checks de BD); una primaria activa. Sin dependencia de mapas.
* **Referencias** (`personal|family|commercial|employer|other`): nombre, relación, teléfono, notas. **No son Person ni fiador** y no crean responsabilidad financiera.
* **Sin borrado**: no existen rutas `DELETE`; las FKs son restrictivas; todo se desactiva.

## 3. Normalización (`app/shared/normalization.py`, sin reglas legales)

Se guarda siempre el valor original y el normalizado. Documento: alfanuméricos en mayúsculas (`001-0000001-1` ≡ `00100000011` ≡ `001 0000001 1`); tipo de documento: token libre en mayúsculas (**no hay lista cerrada de documentos nacionales**; se acepta cualquiera y solo se exige coherencia técnica). Teléfono: solo dígitos, y un número de 11 dígitos que empieza por 1 se reduce a 10 (canonicalización técnica NANP; documentada, no es regla de validez). Correo: minúsculas y recorte, formato técnico. Nombre: comparación sin acentos ni mayúsculas.

## 4. Deduplicación (`dedup.py`)

* **EXACT_MATCH**: mismo `(document_type, document normalizado)` en el tenant → se **rechaza** la creación o corrección (409 `duplicate_identity`); nada se crea ni se fusiona.
* **POSSIBLE_MATCH**: teléfono activo igual, correo activo igual, o **nombre normalizado + fecha de nacimiento** iguales → se detiene con 409 `possible_duplicate` y la lista de candidatos; solo si el llamante lo reconoce (`acknowledge_possible_duplicates`) se crea el cliente y se **registran banderas** `customer_duplicate_flags` en `pending_review`. Un contacto agregado después también puede levantar bandera.
* **NO_MATCH**: el resto. Un nombre solo, una dirección compartida o un apellido **no** son señal.
* La revisión (`/duplicates/{id}/review`: `dismissed` | `confirmed_duplicate` + nota) solo cambia el estado de la bandera: **no hay fusión** (T-004 §14; la fusión formal queda para un paquete posterior con permiso elevado). Un candidato que el llamante no puede leer (otra sucursal) se informa como `restricted` sin id ni código.
* `POST /customers/duplicate-check` clasifica sin escribir. Condición de carrera: el índice único de BD vuelve a rechazar el duplicado exacto.

## 5. API v2 (`/api/v2/customers`)

`GET ?q&status&branch_id&code&document&phone&email&limit&offset`, `POST`, `POST /duplicate-check`, `GET|PATCH /{id}`, `PATCH /{id}/identity`, `POST /{id}/activate|deactivate`, `PUT /{id}/management-branch`, `GET /{id}/duplicates`, `POST /duplicates/{flag}/review`, y por hijo (`contacts`, `addresses`, `references`): `GET`, `POST`, `POST …/{id}/deactivate` (+ `…/primary` en contactos y direcciones).
Entradas con `extra="forbid"` (un `tenant_id` externo es 422). Otros tenants → 404. `PATCH` exige `version` (409 `version_conflict` si cambió). **Los GET no escriben** (probado con un listener SQL). Los POST de solo lectura (`duplicate-check`) tampoco.

**Búsqueda** (siempre del lado del servidor y dentro del tenant): `q` = código o nombre (subcadena sin acentos/mayúsculas, comodines escapados); `document`, `phone` y `email` = coincidencia **exacta** normalizada (no hay escaneo parcial de identificadores); filtros por estado, sucursal y código; paginación con tope 100. Se devuelven DTOs mínimos.

## 6. Privacidad y auditoría

* La lista devuelve solo `id, customer_code, display_name, status, sucursales, documento enmascarado` (sin teléfonos ni correo). El detalle enmascara el documento y omite fecha de nacimiento, país y vigencias salvo `customers.read_sensitive`.
* **Auditoría** (`security_events`, append-only): `customer.created|updated|identity_changed|activated|deactivated|management_branch_changed|contact_added|contact_primary_changed|contact_deactivated|address_*|reference_*|duplicate_flagged|duplicate_reviewed`, con actor, tenant, `correlation_id` y before/after de campos no sensibles. **No se registran documentos completos, teléfonos, correos, nombres ni notas**: solo ids, nombres de campos cambiados y valores enmascarados (`********011`). Los valores reales antes/después de una corrección de identidad viven en `person_identity_revisions` (con `kind` = `correction` o `change` y motivo obligatorio, DF-02 §16), un almacén con control de acceso.
* Los logs de la aplicación no contienen documentos, teléfonos ni correos (probado).

## 7. Autorización e integración (T-002/T-003)

Permisos nuevos (migración `0006`, concedidos a los administradores de agencia existentes): `customers.read`, `customers.read_sensitive`, `customers.create`, `customers.update`, `customers.identity.correct`, `customers.activate`, `customers.assign_branch`, `customers.duplicates.review`. Ningún chequeo mira nombres de rol (test estructural); un rol sin estos permisos (p. ej. "cajero") no recibe acceso al expediente.
**Alcance por sucursal = sucursal de gestión del cliente**: un grant de sucursal alcanza solo clientes gestionados por esa sucursal; un cliente sin sucursal de gestión requiere autoridad tenant-wide; crear con grant de sucursal obliga a indicar esa sucursal; cambiar la sucursal de gestión (`assign_branch`) y la búsqueda `duplicate-check` son tenant-wide. El origen es inmutable. Ambas sucursales deben ser del tenant y estar activas (FKs compuestas + 404). La sucursal de gestión **no** limita dónde puede pagar el cliente (DR-024). Google/contraseña y los alcances `branch`/`cash_point` de T-002/T-003 no se modifican.

## 8. Transformación de datos legacy

`import_legacy_customers()` / `scripts/import_legacy_customers.py` (idempotente, no hace commit por sí misma): por cada fila plana sin perfil crea `Person` (nombre completo **sin partir**), perfil `active` con código nuevo, contactos, dirección (calle ← `calle` o `address`; el texto libre extra va a la referencia), referencias y el mapeo `legacy_customer_id`. Un documento ya tomado **no se fusiona**: la persona nueva entra sin documento, la nota interna lo registra y se levanta una bandera. Fechas de nacimiento no ISO → NULL. No copia cobrador/ruta/caja. La tabla `customers` y sus FKs no se tocan.

## 9. Migración `0006`

`persons` (columnas nuevas, `search_name` rellenado con traducción de acentos, checks, índice único parcial, `UNIQUE(tenant_id,id)`), `tenant_sequences`, `customer_profiles`, `customer_contacts`, `customer_addresses`, `customer_references`, `customer_duplicate_flags`, `person_identity_revisions` (+ FKs compuestas e índices) y los 8 permisos. Probada con una persona existente en BD desechable (upgrade, `alembic check`, downgrade, re-upgrade).

## 10. Limitaciones conocidas

* `restricted/blocked`, completitud por producto, verificación de datos (declarado/verificado), actividad económica/laboral, gastos declarados, relaciones (cónyuge, representante), tipo de cliente persona jurídica y fusión formal **no** se implementan (BLOCKED_BY_SPEC o paquetes posteriores).
* Documentos: no hay almacenamiento ni referencias lógicas todavía.
* Duplicados por dirección compartida (S02) no generan advertencia; solo teléfono, correo, documento y nombre+fecha.
* La búsqueda por nombre usa `LIKE` sobre una columna normalizada (sin índice trigram; aceptable para el tamaño actual).
* Doble fuente transitoria: `/api/v1/customers` sigue siendo la autoridad para Crédito/Caja hasta su reconstrucción; no hay sincronización automática (solo la herramienta de importación bajo demanda).
* La normalización de teléfonos NANP es una decisión técnica, no una regla de validez.
