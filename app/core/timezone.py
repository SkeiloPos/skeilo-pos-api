"""Mecanismo central de zona horaria (spec 030, FR-002).

Todas las entidades con un instante absoluto (Venta, Orden, Pago, Caja,
Inventario, Mesas, Facturas, Compras, Auditoría) pasan por aquí: una sola
resolución de zona horaria por tenant, un solo tipo de serialización con
offset UTC explícito, un solo cálculo de límites de día de negocio, y un solo
envoltorio de "ahora". Ningún módulo de entidad implementa su propia
conversión (ver spec.md FR-002).
"""
from datetime import date, datetime, time, timezone
from functools import lru_cache
from typing import Annotated, NamedTuple
from zoneinfo import ZoneInfo, available_timezones

from pydantic import PlainSerializer

from app.core.config import settings
from app.core.models import Tenant


def resolve_timezone(tenant: "Tenant | None") -> ZoneInfo:
    """Zona horaria del tenant, con `America/Bogota`/`TENANT_TIMEZONE` como
    respaldo si el tenant no llega o su columna aún es `None` (no debería
    serlo tras la migración, pero el respaldo evita un None-crash)."""
    if tenant is not None and tenant.timezone:
        return ZoneInfo(tenant.timezone)
    return ZoneInfo(settings.TENANT_TIMEZONE)


def _serialize_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


UtcDatetime = Annotated[datetime, PlainSerializer(_serialize_utc, return_type=str)]


def local_day_bounds_utc(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Medianoche local de `day` y del día siguiente, ambas en UTC naive —
    mismo tipo que las columnas `DateTime` que compara (FR-004)."""
    next_day = date.fromordinal(day.toordinal() + 1)
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(next_day, time.min, tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def utc_now() -> datetime:
    """Envoltorio único de `datetime.now(timezone.utc)` (FR-008) — reemplaza
    los sitios que hoy construyen su propio "ahora" para asignarlo a una
    columna `DateTime` naive persistida."""
    return datetime.now(timezone.utc)


def utc_now_naive() -> datetime:
    """"Ahora" en UTC **sin** `tzinfo`: la única forma de escribir un instante
    en una columna `DateTime` sin zona (spec 138, FR-005).

    Regla: columna sin zona => `utc_now_naive()`; columna `DateTime(timezone=True)`
    (hoy solo `promotion_evaluated_at`) => `utc_now()`. Nadie arma a mano
    `utc_now().replace(tzinfo=None)`, `datetime.utcnow()` ni `datetime.now()` sin zona
    (`test_instantes_forma_unica` lo vigila). Se apoya en `utc_now()` para que un reloj
    fijado en este módulo afecte a ambas."""
    return utc_now().replace(tzinfo=None)


def business_today(tenant: "Tenant | None") -> date:
    """"Hoy" en la zona horaria del tenant, no en la del servidor ni en la UTC
    (spec 138, FR-007). Vivía en `cash/reports.py` (spec 030); se movió aquí
    para que lo usen también el rechazo de pagos y el nombre del export de inventario."""
    return utc_now().astimezone(resolve_timezone(tenant)).date()


@lru_cache(maxsize=1)
def _zonas_iana_disponibles() -> frozenset[str]:
    # `available_timezones()` recorre el disco en cada llamada.
    return frozenset(available_timezones())


def validate_iana_timezone(value: object) -> str:
    """Devuelve `value` si es una zona IANA válida o lanza `ValueError` (spec 138, FR-010).

    Es válida si **(a)** la base IANA del sistema la reconoce y **(b)** tiene forma de
    región (`Área/Lugar`, puede llevar más de una barra) o es exactamente `UTC`. La regla
    de forma deja fuera los archivos de la máquina (`localtime`, `Factory`, `posixrules`)
    que `ZoneInfo` acepta. El disparador de `shared.tenants` aplica la misma regla en la
    base de datos (data-model.md §1)."""
    if not isinstance(value, str):
        raise ValueError(f"Zona horaria inválida: {value!r}")
    if value not in _zonas_iana_disponibles() or (value != "UTC" and "/" not in value):
        raise ValueError(f"Zona horaria inválida: {value!r}")
    return value


class ZonaCurada(NamedTuple):
    iana: str
    nombre: str    # texto del selector de alta de tenant
    etiqueta: str  # «(hora de {etiqueta})» en el correo


# Las siete zonas del selector (data-model.md §3). Duplicada en
# `pos-heladeria/src/app/shared/tenant-timezones.ts`; una prueba por lado compara con el mismo literal.
ZONAS_CURADAS: tuple[ZonaCurada, ...] = (
    ZonaCurada("America/Bogota", "Bogotá (Colombia)", "Colombia"),
    ZonaCurada("America/Mexico_City", "Ciudad de México", "Ciudad de México"),
    ZonaCurada("America/Lima", "Lima (Perú)", "Lima"),
    ZonaCurada("America/Santiago", "Santiago (Chile)", "Santiago"),
    ZonaCurada("America/Argentina/Buenos_Aires", "Buenos Aires (Argentina)", "Buenos Aires"),
    ZonaCurada("Europe/Madrid", "Madrid (España)", "Madrid"),
    ZonaCurada("Asia/Tokyo", "Tokio (Japón)", "Tokio"),
)


def etiqueta_zona_correo(zona: str, instante: datetime) -> str:
    """Nombre corto de la zona para el correo (spec 138, FR-011): la etiqueta de la lista
    curada («Colombia», «Ciudad de México»…) o, si la zona es válida pero no está en la
    lista, el desfase **vigente en `instante`** (`UTC−3`, `UTC+5:30`, `UTC`). Quien la usa
    decide el texto envolvente («hora de …» solo para nombres, no para desfases).

    `instante` naive se interpreta como UTC (así se guardan); jamás se llama a
    `astimezone()` sobre un naive, que lo leería como hora local de la máquina."""
    for curada in ZONAS_CURADAS:
        if curada.iana == zona:
            return curada.etiqueta
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    desfase = instante.astimezone(ZoneInfo(zona)).utcoffset()
    minutos = int(desfase.total_seconds() // 60)
    if minutos == 0:
        return "UTC"
    signo = "+" if minutos > 0 else "\u2212"  # menos tipográfico (U+2212)
    horas, resto = divmod(abs(minutos), 60)
    return f"UTC{signo}{horas}" + (f":{resto:02d}" if resto else "")
