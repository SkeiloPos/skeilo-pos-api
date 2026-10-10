# POS Backend — Heladería (multi-tenant)

Backend de punto de venta (POS) **multi-tenant** construido con **FastAPI + PostgreSQL 16**, con aislamiento **schema-per-tenant**. Cubre catálogo, inventario (stock único), caja, ventas, facturación y el flujo completo de **gestión de mesas por QR** (carrito por comensal → consolidación → cobro → facturación).

> Las reglas de negocio del flujo de mesas están en [gestion-mesas.md](gestion-mesas.md).

---

## Stack

| Área                       | Tecnología                                  |
| -------------------------- | ------------------------------------------- |
| API                        | FastAPI                                     |
| ORM / migraciones          | SQLAlchemy 2 + Alembic                      |
| Base de datos              | PostgreSQL 16 (schema-per-tenant)           |
| Cache / colas              | Redis (blocklist de JWT + broker de Celery) |
| Tareas async               | Celery (worker)                             |
| Almacenamiento de imágenes | Cloudflare R2 (S3-compatible)               |
| Email                      | Resend                                      |
| Auth                       | JWT (PyJWT, HS256) + bcrypt                 |

Python **3.12** en la imagen Docker (3.11+ compatible).

---

## Requisitos previos

- **Opción A (recomendada):** Docker + Docker Compose.
- **Opción B (local):** Python 3.11+, PostgreSQL 16 y Redis 7 corriendo localmente.

---

## Configuración (`.env`)

Copia la plantilla y complétala:

```bash
cp .env.example .env
```

Variables principales:

| Variable                                                          | Descripción                                                                      |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`             | Credenciales que usa `docker-compose` para el contenedor de Postgres.            |
| `DATABASE_URL`                                                    | Cadena SQLAlchemy, p. ej. `postgresql+psycopg://admin:...@postgres:5432/pos_db`. |
| `REDIS_URL`                                                       | p. ej. `redis://redis:6379/0`.                                                   |
| `JWT_SECRET`                                                      | Secreto de firma de los JWT (**obligatorio**).                                   |
| `JWT_ALGORITHM`                                                   | Por defecto `HS256`.                                                             |
| `ACCESS_TOKEN_EXPIRY`                                             | Expiración del access token en minutos (por defecto `1440`).                     |
| `SESSION_TTL_MINUTES`                                             | Ventana deslizante de la sesión del comensal (QR). Por defecto `240` (4 h).      |
| `SESSION_ABS_MAX_MINUTES`                                         | Tope absoluto del token de sesión del comensal. Por defecto `1440` (24 h).       |
| `QR_TOKEN_SECRET`                                                 | Secreto dedicado para firmar tokens de QR/sesión. Si se omite, usa `JWT_SECRET`. |
| `RESEND_API_KEY` / `MAIL_FROM_NAME` / `MAIL_FROM`                 | Envío de correo (Resend).                                                        |
| `SUPER_ADMIN_NAME` / `SUPER_ADMIN_EMAIL` / `SUPER_ADMIN_PASSWORD` | Super admin global sembrado en el primer arranque.                               |
| `R2_*`                                                            | Credenciales y endpoints de Cloudflare R2 para imágenes de productos.            |

> **Hosts:** dentro de Docker, `DATABASE_URL`/`REDIS_URL` apuntan a los nombres de servicio (`postgres`, `redis`). Para correr la app fuera de Docker usa `localhost`.

---

## Arranque con Docker (recomendado)

```bash
docker compose up -d --build
```

Levanta:

- **api** → http://localhost:8000
- **postgres** → `:5432`
- **redis** → `:6379`
- **worker** → Celery

En el **primer arranque**, la app crea el schema `shared`, siembra los roles (`SUPER_ADMIN`, `ADMIN`, `CASHIER`, `MESERO`) y el super admin global (según las variables `SUPER_ADMIN_*`).

Ver logs:

```bash
docker compose logs -f api
```

---

## Arranque local (sin Docker de la app)

Levanta solo la infraestructura con Docker y corre la API en tu máquina:

```bash
# 1. Infraestructura (Postgres + Redis)
docker compose up -d postgres redis

# 2. Entorno de Python
python -m venv env
source env/bin/activate          # Windows: env\Scripts\activate
pip install -r requirements.txt

# 3. .env con hosts en localhost (DATABASE_URL/REDIS_URL -> localhost)

# 4. Migraciones
alembic upgrade head

# 5. API
uvicorn app.main:app --reload

# 6. Worker de Celery (en otra terminal, con el mismo entorno activado)
celery -A app.celery_task:c_app worker --loglevel=info
```

API en http://localhost:8000.

---

## Base de datos y migraciones

```bash
alembic upgrade head                            # aplica migraciones pendientes
alembic revision --autogenerate -m "mensaje"    # genera migración desde los modelos
alembic downgrade -1                            # revierte una migración
```

**Modelo multi-tenant:** existe un schema `shared` (registro de tenants + `users`/`roles`) y **un schema por tenant** creado dinámicamente. SQLAlchemy y Alembic usan `schema_translate_map` para enrutar los modelos declarados con `schema="tenant"` al schema correcto en tiempo de ejecución. Las migraciones se aplican a cada schema de tenant (helper `for_each_tenant_schema`).

Para un reset limpio en desarrollo (sin datos que preservar) existe el flujo de _reset baseline_ que regenera una única migración desde los modelos actuales.

---

## Multi-tenancy y tenants

- Cada request identifica su tenant con el header **`x-tenant-host`** (se resuelve contra `shared.tenants`).
- Crear/gestionar tenants desde la CLI:

  ```bash
  python -m app.scripts.tenant <comando>
  ```

- El super admin global (`tenant_id = NULL`) se siembra en el primer arranque desde `SUPER_ADMIN_*`.

---

## Autenticación

```
POST /api/v1/auth/login        # con header x-tenant-host -> access_token + refresh_token (JWT)
GET  /api/v1/auth/refresh-token
GET  /api/v1/auth/logout       # revoca el token (blocklist en Redis)
```

El flujo público del comensal (QR) usa **tokens firmados** de QR y de sesión (independientes del JWT de staff): el token de QR codifica tenant + mesa; el de sesión identifica al comensal con una ventana deslizante de actividad .

---

## Documentación de la API

- **Swagger UI:** http://localhost:8000/docs
- **OpenAPI JSON:** http://localhost:8000/openapi.json
- **Health check:** http://localhost:8000/api/v1/health

---

## Estructura del proyecto

```
app/
  main.py                 # app factory + montaje de routers
  core/                   # db multi-tenant, config, auth, tokens QR, storage, crud, pagination
  models/                 # modelos SQLAlchemy (tenant + shared)
  api/v1/                 # módulos: auth, users, categories, products, catalog,
                          #   inventory, menu, orders, cart, cash, sales, invoices, ...
  scripts/                # CLI de tenants, seeds, utilidades
alembic/                  # entorno y migraciones
docker-compose.yml
Dockerfile
```

---

## Flujo de gestión de mesas (QR)

Resumen del recorrido implementado (8 fases; ver [gestion-mesas.md](gestion-mesas.md)):

1. **QR firmado + menú público** — el comensal escanea y ve el menú sin autenticación.
2. **Carrito por comensal** — sesión con token deslizante; cada comensal arma su carrito.
3. **Consolidación (mesero)** — agrupa los carritos de la mesa en una orden y **descuenta inventario**.
4. **Adiciones post-consolidación** — más ítems a la orden abierta; crea _orden-hija_ si la única está en cobro.
5. **KDS (cocina)** — estados por ítem (`pendiente → en_preparacion → listo → entregado`), anulación/reemplazo.
6. **Bloqueo + cobro** — lock optimista, cuenta con _split_ por comensal, pago (genera venta), cancelación con reversa de inventario, liberación de mesa.
7. **Facturación** — factura interna con consecutivo (N por mesa, una por orden pagada).

---

## Tests de caracterización

`app/characterization_tests/` congela el comportamiento **actual** de los
módulos de criticidad ALTA de catálogo/inventario/facturación (motor de
precio de línea, plan de consumo de inventario, `record_movement`/
`apply_adjustment`, formato de número de factura, etc.), incluyendo un
golden master del núcleo de cálculo del flujo de pedido de mesa por QR. No
son tests de aceptación: si uno falla tras un cambio de código, la pregunta
es si el negocio autorizó ese cambio de comportamiento, no cómo "arreglar"
el test. Ver `app/characterization_tests/__init__.py` y
`golden_master/README.md` para el alcance completo y el procedimiento de
regeneración del golden master.

No añaden dependencias: usan `unittest` (biblioteca estándar) y SQLAlchemy
(ya en `requirements.txt`) sobre SQLite en memoria — no requieren Postgres
ni Redis levantados.

```bash
python -m unittest discover -s app/characterization_tests -p 'test_*.py' -v
```

(o un módulo suelto, p. ej. `python -m unittest
app.characterization_tests.test_golden_master_pricing_consumption -v`).

---

## Licencia

Proyecto privado. Todos los derechos reservados.
