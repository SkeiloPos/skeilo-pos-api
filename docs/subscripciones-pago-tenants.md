# Gestión de pagos de suscripción de tenants

## Contexto

Hoy cada `Tenant` (`shared.tenants`) ya tiene un `plan_id` obligatorio y, opcionalmente,
`ciclo_facturacion` / `plan_iniciado_en` / `plan_vence_en` (`app/core/models.py:59-70`),
calculados al crear o reasignar el plan vía `tenant_create()` / `update_tenant_plan()`
(`app/core/db.py`, `app/api/v1/super_admin/router.py:380-421`). Existe un chequeo reactivo de
vencimiento, `ensure_plan_not_expired` (`app/core/plan_limits.py:87-99`), que bloquea con 403 la
creación de recursos limitados y el acceso a los módulos `inventario`/`promociones`/`cocina` en
cuanto `plan_vence_en` queda en el pasado — pero **nunca bloquea el login**.

No existe hoy:
- Ningún registro histórico de pagos de suscripción (solo se sobrescriben `plan_iniciado_en`/
  `plan_vence_en` en cada renovación manual).
- Ninguna pasarela de pago integrada (se confirmó, por grep exhaustivo, que no hay Stripe, Wompi,
  PayU, ePayco ni MercadoPago en el backend).
- Ningún período de gracia ni alerta de vencimiento próximo.
- Ninguna forma de que el propio tenant pague: hoy la renovación la hace manualmente el Super
  Admin vía `PATCH /super-admin/tenants/{id}`, sin ningún comprobante real detrás.

El objetivo de esta iniciativa es que **el propio tenant pague su suscripción** mediante un link
de pago (pasarela **Wompi**, Colombia), sin gestionar tarjetas ni métodos de pago guardados en su
cuenta, y que el sistema aplique automáticamente las reglas de vencimiento, gracia y bloqueo, y
recalcule las fechas de la suscripción según cuándo se realice el pago.

## Reglas de negocio acordadas con el cliente

1. **La suscripción nace con el tenant**: al crear un tenant y asignarle un plan, la suscripción
   empieza a correr desde ese momento (ya ocurre hoy vía `plan_iniciado_en`/`plan_vence_en`).
2. **Solo planes mensuales por ahora** (`ciclo_facturacion = "mensual"`).
3. **Alerta de vencimiento próximo**: 2 días antes de `plan_vence_en`, el sistema debe avisar
   (banner in-app) a los usuarios con rol `ADMIN` del tenant. No bloquea nada, es solo informativo.
4. **Período de gracia**: al llegar `plan_vence_en` (día 0), el tenant entra en un período de
   gracia de **5 días** durante el cual el sistema sigue funcionando con normalidad — ningún
   módulo se bloquea — y solo se muestra un banner de aviso con el botón para pagar.
5. **Bloqueo total de módulos**: pasados los 5 días de gracia sin que se registre un pago
   aprobado, el sistema bloquea el acceso a **todos los módulos**. El login **nunca** se bloquea:
   el usuario siempre puede iniciar sesión, pero solo ve el mensaje de renovación y el botón de
   pago, sin poder operar ningún módulo.
6. **El link de pago se genera a demanda**: el sistema no envía ni genera proactivamente el link;
   es el propio tenant (usuario con rol `ADMIN`) quien lo solicita con un botón "pagar ahora",
   disponible en cualquiera de los estados de alerta/gracia/bloqueo.
7. **Recalculo de la fecha de vencimiento al aprobarse un pago**:
   - Si el pago se aprueba **antes de vencer o durante el período de gracia** → la nueva fecha de
     vencimiento se cuenta desde el `plan_vence_en` anterior (`plan_vence_en_anterior + 1 mes`),
     de forma que el tenant no pierde el tiempo que ya tenía pagado.
   - Si el pago se aprueba **después de agotado el período de gracia** (tenant ya bloqueado) → la
     nueva fecha de vencimiento se cuenta desde la fecha real del pago (`fecha_pago + 1 mes`).
8. **Sin gestión de tarjetas**: el tenant nunca administra medios de pago guardados; cada cobro es
   un link de pago de un solo cargo.

## Alcance de esta iniciativa

Backend únicamente. Un cambio de contrato de API no se considera terminado hasta que se ajuste el
cliente Angular (`../pos-heladeria`) — cada historia que toque un endpoint público indica el punto
de integración que el frontend deberá consumir, pero su implementación queda fuera de esta lista.

## Historias de usuario

Las historias están ordenadas para implementarse **secuencialmente**: cada una depende de que la
anterior esté terminada (modelo de datos antes que lógica de negocio, lógica de negocio antes que
automatización, automatización antes que reportes).

---

### HU1 — Ajustar el cómputo de vencimiento para incluir el período de gracia

**Como** sistema, **quiero** considerar el período de gracia de 5 días al evaluar si un plan está
vencido, **para que** el tenant no pierda acceso a los módulos exactamente el día de vencimiento,
sino solo al agotarse la gracia.

**Criterios de aceptación**
- Dado un tenant con `plan_vence_en` en el pasado pero dentro de los 5 días siguientes, al acceder
  a un módulo o crear un recurso limitado, el sistema permite la operación con normalidad.
- Dado un tenant con `plan_vence_en + 5 días` ya en el pasado, al acceder a un módulo o crear un
  recurso limitado, el sistema responde 403 con el mensaje de renovación (igual que hoy).
- El login nunca se bloquea por este motivo, en ningún estado (ya es el comportamiento actual:
  no se toca el flujo de login).
- `GET /plan` expone un estado de suscripción con (al menos) estas 4 variantes calculadas en
  tiempo real contra `plan_vence_en`:
  - `activa`: faltan más de 2 días para vencer (o el plan no vence, `plan_vence_en IS NULL`).
  - `por_vencer`: faltan 2 días o menos para vencer.
  - `en_gracia`: ya venció, dentro de los 5 días de gracia.
  - `bloqueada`: venció y ya pasaron los 5 días de gracia.
- `GET /plan` también expone los días restantes/transcurridos y la fecha límite de pago antes del
  bloqueo total, para que el frontend pueda redactar el mensaje exacto.
- Un tenant con `plan_vence_en IS NULL` (plan sin vencimiento) siempre está en estado `activa` y
  nunca entra en gracia ni bloqueo.

**Notas técnicas**
- Nuevos settings en `app/core/config.py`: `SUBSCRIPTION_GRACE_DAYS` (default `5`) y
  `SUBSCRIPTION_ALERT_DAYS_BEFORE` (default `2`), siguiendo el patrón de
  `PROMOTION_EXPIRE_INTERVAL_MINUTES` (línea 61 del mismo archivo).
- `app/core/plan_limits.py`:
  - Nueva función pura `get_subscription_state(tenant)` que devuelve el estado (`activa` /
    `por_vencer` / `en_gracia` / `bloqueada`) y los metadatos de fecha, usando
    `relativedelta(days=settings.SUBSCRIPTION_GRACE_DAYS)` sobre `tenant.plan_vence_en` (mismo
    import `dateutil.relativedelta` ya usado en `calculate_plan_vencimiento`, línea 197).
  - `ensure_plan_not_expired` (línea 87) pasa a comparar contra `plan_vence_en + gracia` en vez de
    `plan_vence_en` directo. Es el único punto de entrada que hay que tocar: `enforce_plan_limit`
    y `require_module_access`/`ensure_module_access` ya la invocan como primer paso, así que
    ningún router necesita cambios.
- `app/api/v1/plan/service.py` (`build_plan_summary`): sustituir el booleano `vencido` por el
  nuevo estado completo de `get_subscription_state`.
- `app/api/v1/plan/schemas.py` (`PlanSummaryResponse`): agregar `subscription_state`,
  `dias_restantes` y `fecha_limite_pago` (o nombres equivalentes).

**Verificación**
- Tests unitarios de `get_subscription_state` cubriendo las 4 franjas de fecha.
- Test de integración de `GET /plan` reflejando cada estado.
- Test de que un recurso/módulo limitado sigue accesible durante la gracia y se bloquea después.

---

### HU2 — Modelo de pagos de suscripción y generación de link de pago (Wompi)

**Como** administrador del tenant, **quiero** solicitar un link de pago para mi suscripción,
**para que** pueda pagarla yo mismo sin depender del Super Admin.

**Criterios de aceptación**
- Un usuario con rol `ADMIN` del tenant puede solicitar la generación de un link de pago para su
  suscripción vigente, en cualquier estado (`por_vencer`, `en_gracia` o `bloqueada`; también se
  permite en `activa` para quien quiera pagar por adelantado).
- Un usuario que no sea `ADMIN` recibe 403 al intentar solicitarlo.
- El monto del link corresponde al precio mensual vigente del plan del tenant
  (`plan.precio_mensual`).
- Si ya existe un link de pago `pending` vigente (no expirado), se reutiliza en vez de crear uno
  nuevo — evita que el tenant genere links duplicados a cada clic.
- Cada solicitud queda registrada con su estado, de forma trazable (quién la generó y cuándo).

**Notas técnicas**
- Nueva tabla `shared.subscription_payments`, modelo nuevo en
  `app/models/subscription_payment.py` (seguir el patrón de `app/models/plan.py`):
  `id`, `tenant_id` (FK `shared.tenants.id`), `plan_id` (FK `shared.plans.id`, snapshot al
  momento del pago), `ciclo_facturacion`, `amount` (`Numeric(12, 2)`, snapshot de
  `plan.precio_mensual`), `status` (`pending` / `approved` / `rejected` / `expired`), `gateway`
  (`"wompi"`), `gateway_reference` (string, única, para idempotencia del webhook de HU3),
  `payment_link_url`, `payment_link_expires_at`, `requested_by_user_id` (FK
  `shared.users.id`), `period_start`, `period_end` (se completan al aprobarse, ver HU3),
  `paid_at`, `created_at`/`updated_at`.
- Migración Alembic nueva, siguiendo la convención de nombres de
  `alembic/versions/5a77a91b482d_tenant_plan_assignment.py`.
- Cliente Wompi: wrapper mínimo (p. ej. `app/core/wompi_client.py`) para crear un "Payment Link"
  de un solo cargo vía la API de Wompi, usando nuevos settings `WOMPI_PUBLIC_KEY`,
  `WOMPI_PRIVATE_KEY` y `WOMPI_INTEGRITY_SECRET` (`app/core/config.py` + `.env.example`).
- Endpoint nuevo `POST /plan/subscription/payment-link` en `app/api/v1/plan/router.py`, protegido
  con `Depends(require_tenant_admin)` (dependencia ya existente en `app/core/dependencies.py`,
  mismo patrón usado en otros routers "solo admin del tenant").

**Verificación**
- Test de integración con el cliente Wompi mockeado: se crea el registro `pending` y se devuelve
  la URL.
- Test de reutilización: una segunda solicitud con un link `pending` vigente no crea un segundo
  registro.
- Test de autorización: usuario no-`ADMIN` → 403.

---

### HU3 — Webhook de confirmación de pago y recalculo de fechas de vencimiento

**Como** sistema, **quiero** recibir la confirmación de Wompi cuando un pago se aprueba, **para
que** la suscripción del tenant se renueve automáticamente con la fecha correcta según cuándo se
pagó.

**Criterios de aceptación**
- Al recibir una notificación de pago aprobado desde Wompi para una referencia existente:
  - Si el pago se confirmó **antes del vencimiento o durante el período de gracia**, la nueva
    fecha de vencimiento es `plan_vence_en_anterior + 1 mes`.
  - Si el pago se confirmó **después de agotada la gracia** (tenant ya bloqueado), la nueva fecha
    de vencimiento es `fecha_de_pago + 1 mes`.
  - El registro de `subscription_payments` correspondiente queda `approved`, con `paid_at`,
    `period_start` y `period_end` completos.
  - El tenant recupera acceso inmediato a todos los módulos (el estado vuelve a `activa`).
- Al recibir una notificación de pago rechazado, el registro queda `rejected` y no se modifica
  ninguna fecha del tenant.
- Una notificación con firma inválida se rechaza (401/400) sin modificar nada.
- Reenviar la misma notificación (mismo `gateway_reference`) dos veces no aplica el cambio de
  fecha dos veces (idempotencia).

**Notas técnicas**
- Endpoint público `POST /webhooks/wompi` (nuevo router, p. ej. `app/api/v1/webhooks/`), valida
  la firma del evento con `WOMPI_EVENTS_SECRET` según el esquema de checksum que define Wompi.
- Servicio de negocio (p. ej. `app/services/subscription_renewal.py`):
  - Determina la fecha base reutilizando el mismo corte que `get_subscription_state` (HU1):
    `base = plan_vence_en` si `paid_at` cae dentro de vencimiento+gracia, si no `base = paid_at`.
  - `nuevo_vence = calculate_plan_vencimiento(base, "mensual")`, reutilizando la función ya
    existente y probada en `app/core/plan_limits.py:197-207`.
  - Actualiza `tenant.plan_iniciado_en = base` y `tenant.plan_vence_en = nuevo_vence`.
- Si el proyecto ya tiene un patrón de auditoría para acciones administrativas sensibles
  (`app/core/admin_audit_log.py`), evaluar si corresponde registrar también aquí la renovación.

**Verificación**
- Tests que simulan los 3 escenarios de fecha de pago (antes de vencer, durante gracia, después de
  gracia) y confirman la fecha de vencimiento resultante exacta en cada caso.
- Test de firma inválida y de reenvío duplicado del mismo evento.

---

### HU4 — Alertas automáticas de vencimiento próximo, inicio de gracia y bloqueo

**Como** administrador del tenant, **quiero** recibir un aviso dentro del sistema cuando mi
suscripción esté por vencer, haya entrado en gracia o se haya bloqueado, **para** poder renovarla
a tiempo sin enterarme solo cuando ya no puedo operar.

**Criterios de aceptación**
- 2 días antes del vencimiento, cada usuario `ADMIN` del tenant recibe un aviso in-app indicando
  cuántos días faltan.
- El día en que la suscripción vence (inicio de la gracia), cada `ADMIN` recibe un aviso de que
  entró en período de gracia y hasta qué fecha puede pagar sin bloqueo.
- Al agotarse la gracia, cada `ADMIN` recibe un aviso de que el sistema quedó bloqueado.
- Ninguno de estos tres avisos se duplica entre corridas sucesivas del proceso que los genera
  (un aviso por ciclo de vencimiento, no uno por cada ejecución del job).
- Los usuarios sin rol `ADMIN` no reciben estos avisos.

**Notas técnicas**
- Nuevas funciones de notificación in-app en `app/core/notifications/dispatch.py`, junto a
  `notify_order_created`/`notify_payment_completed` ya existentes:
  `notify_subscription_expiring(tenant)`, `notify_subscription_grace_started(tenant)` y
  `notify_subscription_blocked(tenant)`, con nuevos `event_type`
  (`"subscription.expiring"`, `"subscription.grace_started"`, `"subscription.blocked"`) y
  `related_entity_type="tenant"`.
- Filtrar la entrega a solo usuarios con rol `ADMIN` (hoy `_dispatch` no distingue rol; definir si
  se agrega un parámetro de "solo roles X" o se filtra en el canal in-app al consultarlas).
- Nuevo job periódico en `app/core/scheduler.py`, replicando el patrón exacto de
  `expire_promotions` / `_run_expire_promotions_with_lock` (líneas 223-290 y 348-361): recorre
  `shared.tenants` con su `timezone`, evalúa `plan_vence_en` en hora local de cada tenant (no UTC
  del servidor — el mismo motivo documentado en ese código para el bug histórico A-128), y
  despacha cada notificación una sola vez por ciclo de vencimiento, verificando antes si ya existe
  un `NotificationEvent` de ese tipo para ese tenant con el mismo `plan_vence_en` en el payload.
- Registro del job en `start_scheduler()` con `IntervalTrigger`, mismo criterio que
  `expire_promotions`.

**Verificación**
- Test del job con tenants en cada franja de fecha (2 días antes, día de vencimiento, día de fin
  de gracia), comprobando que se crea exactamente un `NotificationEvent` por franja.
- Test de que correr el job dos veces no duplica los avisos.
- Test de que un usuario no-`ADMIN` no recibe estas notificaciones.

---

### HU5 — Historial de pagos de suscripción

**Como** administrador del tenant (o Super Admin), **quiero** ver el historial de pagos de
suscripción de un tenant, **para** llevar control de lo pagado y resolver dudas de facturación.

**Criterios de aceptación**
- Un `ADMIN` del tenant puede consultar el historial de pagos de su propia suscripción (fecha,
  monto, estado, período cubierto).
- El Super Admin puede consultar el historial de pagos de cualquier tenant, para soporte y
  reconciliación.
- Un tenant no puede ver el historial de otro tenant.
- El historial está paginado y ordenado del más reciente al más antiguo.

**Notas técnicas**
- `GET /plan/subscription/payments` en `app/api/v1/plan/router.py`, protegido con
  `Depends(require_tenant_admin)`.
- `GET /super-admin/tenants/{id}/subscription-payments`, en `app/api/v1/super_admin/router.py` o
  `app/api/v1/super_admin/plans_router.py` (el que corresponda según convención actual del
  módulo Super Admin).
- Ambos reutilizan el modelo `shared.subscription_payments` creado en HU2 — ninguna tabla nueva.

**Verificación**
- Test de paginación/orden del historial.
- Test de aislamiento: un tenant no puede leer el historial de otro vía el endpoint de `/plan`.

---

## Nota de integración con el frontend

Este documento cubre solo el backend. Siguiendo la convención del proyecto (un cambio de
contrato de API no se considera terminado hasta tocar el cliente Angular en `../pos-heladeria`),
cuando cada historia quede lista habrá que coordinar con el frontend para:
- HU1: el guard de navegación y el banner de suscripción deben leer `subscription_state` de
  `GET /plan` en vez de (o además de) `vencido`.
- HU2: el botón "pagar ahora" debe llamar a `POST /plan/subscription/payment-link` y redirigir al
  tenant a la URL de Wompi devuelta.
- HU3/HU4: el banner debe reflejar los 4 estados y los avisos nuevos deben aparecer en el centro
  de notificaciones existente.
- HU5: pantalla de historial de pagos dentro de la sección de plan/suscripción del tenant.

## Verificación end-to-end sugerida (una vez implementadas las 5 historias)

1. Crear un tenant de prueba con plan mensual, forzar `plan_vence_en` a distintas fechas (hoy-3,
   hoy, hoy+2, hoy+6) directamente en BD y confirmar vía `GET /plan` el `subscription_state`
   correcto en cada caso.
2. Llamar `POST /plan/subscription/payment-link` como `ADMIN` y como no-`ADMIN`, confirmar 200/403.
3. Simular el webhook de Wompi aprobado para los 3 escenarios de fecha de pago y verificar el
   `plan_vence_en` resultante en cada uno.
4. Correr el job del scheduler manualmente (como ya se hace hoy con `expire_promotions`) y revisar
   que se crean las notificaciones esperadas, sin duplicarse en una segunda corrida.
5. Confirmar que un tenant bloqueado (gracia agotada) puede seguir haciendo login pero recibe 403
   al intentar usar cualquier módulo o crear cualquier recurso limitado.
