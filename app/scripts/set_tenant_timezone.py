"""Script interno: fija la zona horaria IANA de un tenant (spec 030, reapertura
de A-46). Sin pantalla de autoservicio — camino único de escritura junto con
el `server_default='America/Bogota'` de la migración (Clarifications).

Uso:
    python -m app.scripts.set_tenant_timezone --host central.skeilopos.com --timezone America/Mexico_City

Un valor inválido se rechaza antes de escribirse: `Tenant.timezone` valida con
`validate_iana_timezone` (`app/core/timezone.py`: zona IANA reconocida Y con forma
`Área/Lugar` o `UTC`) en el propio modelo (`@validates`, `app/core/models.py`), así que
la excepción se lanza al asignar el atributo, antes de cualquier `commit()`. Además un
disparador de `shared.tenants` aplica la misma regla en la base de datos (spec 138, FR-010):
si la base la rechaza, el script informa el mensaje del motor y no persiste nada.

Este script es el ÚNICO camino para cambiar la zona de un tenant ya creado (no hay pantalla de
edición); antes de escribir imprime una advertencia (FR-016): cambiar la zona reinterpreta el historial.
"""
import argparse
import logging

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.core.db import with_db
from app.core.models import Tenant

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


ADVERTENCIA = (
    "ADVERTENCIA: el tenant {host!r} pasa de la zona {anterior!r} a {nueva!r}.\n"
    "Los instantes guardados no cambian, pero el historial se reinterpretará en la nueva zona: "
    "límites de día, reportes, promociones y fechas mostradas cambiarán de hora local."
)


def set_tenant_timezone(host: str, timezone: str) -> Tenant:
    with with_db(None) as db:
        tenant = db.execute(
            select(Tenant).where(Tenant.host == host)
        ).scalar_one_or_none()
        if tenant is None:
            raise RuntimeError(f"No existe ningún tenant con host {host!r}.")

        anterior = tenant.timezone
        try:
            tenant.timezone = timezone
        except ValueError as exc:
            raise RuntimeError(
                f"{exc} No se persistió ningún cambio."
            ) from exc

        # spec 138 (FR-016): antes de escribir, avisar de que el historial se reinterpreta.
        print(ADVERTENCIA.format(host=host, anterior=anterior, nueva=timezone))

        try:
            db.commit()
        except DBAPIError as exc:
            # El disparador de `shared.tenants` rechaza lo que PostgreSQL no reconoce (p. ej. un alias heredado).
            db.rollback()
            raise RuntimeError(
                f"La base de datos rechazó la zona horaria {timezone!r}: {getattr(exc, 'orig', exc)}. "
                "No se persistió ningún cambio."
            ) from exc
        db.refresh(tenant)
        logger.info("Tenant %r (%s) ahora usa la zona horaria %r.", host, tenant.name, timezone)
        return tenant


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fija la zona horaria IANA de un tenant existente."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--timezone", required=True)
    args = parser.parse_args()

    set_tenant_timezone(host=args.host, timezone=args.timezone)


if __name__ == "__main__":
    main()
