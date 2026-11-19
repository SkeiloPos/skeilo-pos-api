"""Script interno de UNA SOLA VEZ: da a los tenants ya vencidos la gracia completa de 5 días contada desde el
despliegue (spec 161, Historia de Usuario 7, FR-038, D8; `research.md` Decisión 13; `data-model.md § 5`).

Por qué existe: antes de la spec 161 un tenant con `plan_vence_en` pasado solo perdía algunos módulos. Con el guard
global y la gracia de 120 h, uno vencido hace más de 5 días quedaría **bloqueado por completo en el instante del
despliegue**. Este script mueve `plan_vence_en` de esos tenants al `--cutoff` para que arranquen `en_gracia` con 5
días completos por delante.

Uso:

    # Simulación (no escribe nada): lista lo que haría.
    python -m app.scripts.grant_subscription_grace --cutoff 2026-12-01T15:00:00

    # Aplicar de verdad:
    python -m app.scripts.grant_subscription_grace --cutoff 2026-12-01T15:00:00 --apply

Orden recomendado en el despliegue: migraciones de esquema → este script con `--cutoff` = el instante actual en UTC →
reiniciar la API con el código nuevo. Mientras corre, la API vieja sigue sirviendo.

Qué hace, con `cutoff` UTC:

    UPDATE shared.tenants SET plan_vence_en = :cutoff WHERE plan_vence_en IS NOT NULL AND plan_vence_en < :cutoff

- No toca `plan_iniciado_en`, `ciclo_facturacion`, el plan, a los tenants vigentes (`plan_vence_en >= cutoff`) ni
  a los que nunca vencen (`NULL`).
- **Idempotente por construcción**: repetirlo con el MISMO `--cutoff` no encuentra ningún tenant con
  `plan_vence_en < cutoff` (ya valen exactamente `cutoff`). Por eso el corte es obligatorio y explícito: no hay un
  «ahora» implícito que lo vuelva repetible por accidente.
- Su próxima mensualidad se calcula desde el corte (RN-7: si paga en la gracia, el período parte de ese vencimiento).
- **Se pierde el valor histórico del vencimiento anterior**: es operativo, no contable, y queda impreso por tenant
  (id, nombre, anterior, nuevo) y en el log. Guarda esa salida: es la única copia y permite restaurar a mano.
- `--apply` rechaza un `--cutoff` en el futuro (más de 1 hora): movería a tenants vencidos a un vencimiento futuro y
  los dejaría `activa`, extendiéndoles la suscripción sin que nadie lo decida.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import with_db
from app.core.models import Tenant
from app.core.timezone import utc_now_naive

logger = logging.getLogger(__name__)

#: Holgura con la que se tolera un `--cutoff` apenas por delante del reloj (diferencias de segundos entre quien
#: ejecuta el script y la base). Más allá de esto, `--apply` lo rechaza.
FUTURE_TOLERANCE = timedelta(hours=1)


@dataclass(frozen=True)
class GraceChange:
    """Un tenant afectado: su `plan_vence_en` antes y después."""

    tenant_id: int
    name: str
    host: str
    previous: datetime
    new: datetime


def parse_cutoff(value: str) -> datetime:
    """`--cutoff` como instante UTC *naive* (como las columnas del esquema compartido). Acepta ISO 8601 con o sin
    zona (`Z`, `+00:00`, `-05:00`); un valor con zona se lleva a UTC. Lanza `ValueError` si no es una fecha."""
    text = (value or "").strip()
    if not text:
        raise ValueError("el corte está vacío")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def grant_grace(db: Session, cutoff: datetime, *, apply: bool) -> list[GraceChange]:
    """Tenants con `plan_vence_en < cutoff`, ordenados por id. Con `apply=True` los mueve al corte y confirma; sin
    él solo informa (no escribe nada)."""
    tenants = db.execute(
        select(Tenant)
        .where(Tenant.plan_vence_en.is_not(None), Tenant.plan_vence_en < cutoff)
        .order_by(Tenant.id)
    ).scalars().all()

    changes = [
        GraceChange(tenant_id=t.id, name=t.name, host=t.host, previous=t.plan_vence_en, new=cutoff) for t in tenants
    ]
    if apply:
        for tenant in tenants:
            tenant.plan_vence_en = cutoff
        db.commit()
    return changes


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Da a los tenants ya vencidos la gracia completa de 5 días desde el despliegue (spec 161, D8). "
        "Sin --apply solo simula."
    )
    parser.add_argument(
        "--cutoff", required=True,
        help="Instante de corte en ISO 8601 UTC (p. ej. 2026-12-01T15:00:00). Obligatorio y explícito.",
    )
    parser.add_argument("--apply", action="store_true", help="Aplicar los cambios. Sin esta bandera solo simula.")
    args = parser.parse_args(argv)

    try:
        cutoff = parse_cutoff(args.cutoff)
    except ValueError as exc:
        print(f"ERROR: --cutoff inválido ({args.cutoff!r}): {exc}")
        return 2

    if args.apply and cutoff > utc_now_naive() + FUTURE_TOLERANCE:
        print(
            f"ERROR: --cutoff {cutoff.isoformat()} está en el futuro: aplicar movería a tenants vencidos a un "
            "vencimiento futuro y los dejaría al día sin que nadie lo decida. Usa el instante actual en UTC."
        )
        return 2

    with with_db(None) as db:
        changes = grant_grace(db, cutoff, apply=args.apply)

    modo = "APLICADO" if args.apply else "SIMULACIÓN (no se escribió nada; agrega --apply para aplicar)"
    print(f"[{modo}] corte = {cutoff.isoformat()}")
    if not changes:
        print("Nada que hacer: ningún tenant tiene un vencimiento anterior al corte.")
        return 0

    print(f"{len(changes)} tenant(s) con plan_vence_en anterior al corte:")
    for change in changes:
        line = (
            f"  id={change.tenant_id} nombre={change.name!r} host={change.host} "
            f"anterior={change.previous.isoformat()} nuevo={change.new.isoformat()}"
        )
        print(line)
        if args.apply:
            logger.info("Gracia de suscripción otorgada: %s", line.strip())
    if args.apply:
        print("Guarda esta salida: es la única copia de los vencimientos anteriores.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
