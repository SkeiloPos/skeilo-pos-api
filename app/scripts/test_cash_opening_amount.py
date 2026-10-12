"""Test de la validación de saldo inicial mayor a cero al abrir caja (spec 113).

    python -m app.scripts.test_cash_opening_amount

Cubre los 7 casos de prueba de contracts/post-shifts-open.md: el router rechaza
`opening_amount <= 0` con 422 y mensaje explícito, sin importar si es cero,
negativo o se omite del cuerpo (casos 1-3); acepta cualquier valor
estrictamente mayor a cero, incluido el mínimo 0.01 (casos 4-5); el
`CheckConstraint` de base de datos rechaza un `INSERT` directo con
`opening_amount <= 0` fuera del endpoint, como defensa en profundidad
(caso 6); y la migración `NOT VALID` de esta spec no afecta ningún turno
histórico ya existente con `opening_amount = 0` (caso 7, FR-007).

A diferencia de `test_cash_net_sales.py` (spec 108), sí necesita un tenant
real en Postgres -- mismo patrón que `test_facturacion.py`/
`test_split_blindaje.py` (specs 107/089): SQLite en memoria (el harness de
`orders_fixtures.py`) no valida `CheckConstraint` de la misma forma que
PostgreSQL, y los casos 6-7 dependen exactamente de eso.

Cada caso crea su propia `CashRegister` desechable; se borra al terminar
junto con cualquier `CashShift` que haya quedado.
"""
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.db import with_db
from app.core.models import User
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift
from app.api.v1.cash.router import open_shift
from app.api.v1.cash.schemas import ShiftOpen

CONSTRAINT = "ck__cash_shifts__ck_cash_shift_opening_positive"


def _schema() -> str:
    with with_db(None) as db:
        rows = db.execute(text("SELECT schema FROM shared.tenants ORDER BY id")).scalars().all()
    for schema in rows:
        with with_db(schema) as db:
            exists = db.execute(
                text("SELECT to_regclass(:q)"), {"q": f"{schema}.cash_shifts"}
            ).scalar() is not None
        if exists:
            return schema
    raise SystemExit("Ningún tenant tiene cash_shifts creada todavía.")


def _check(label, actual, esperado) -> None:
    if actual != esperado:
        raise AssertionError(f"{label}: esperado {esperado}, obtenido {actual}")
    print(f"  ok  · {label}")


def _expect_422(fn, label: str) -> None:
    try:
        fn()
    except HTTPException as e:
        _check(label, e.status_code, 422)
        return
    raise AssertionError(f"{label}: no lanzó HTTPException")


def _nueva_caja(db, suf: str) -> CashRegister:
    reg = CashRegister(name=f"caja-113-{suf}-{uuid4().hex[:6]}")
    db.add(reg)
    db.flush()
    db.commit()
    return reg


def _cleanup(schema: str, register_ids: list) -> None:
    if not register_ids:
        return
    with with_db(schema) as db:
        ids = ",".join(f"'{r}'" for r in register_ids)
        db.execute(text(f'DELETE FROM "{schema}".cash_shifts WHERE cash_register_id IN ({ids})'))
        db.execute(text(f'DELETE FROM "{schema}".cash_registers WHERE id IN ({ids})'))
        db.commit()


def test_opening_amount_cero_rechaza_422(schema: str, user_id, registers: list) -> None:
    """Caso 1: opening_amount=0, caja sin turno abierto -> 422, mensaje
    explícito, ningún CashShift creado."""
    with with_db(schema) as db:
        reg = _nueva_caja(db, "cero")
        registers.append(reg.id)
        user = db.get(User, user_id)

        body = ShiftOpen(cash_register_id=reg.id, opening_amount=Decimal("0"))
        _expect_422(lambda: open_shift(body=body, db=db, user=user), "opening_amount=0 -> 422")
        db.rollback()

        creados = db.execute(
            select(CashShift).where(CashShift.cash_register_id == reg.id)
        ).scalars().all()
        _check("ningún CashShift creado (caso 1)", len(creados), 0)
    print("OK: test_opening_amount_cero_rechaza_422")


def test_opening_amount_negativo_rechaza_422(schema: str, user_id, registers: list) -> None:
    """Caso 2: opening_amount=-50000 -> 422, mismo criterio, ningún CashShift creado."""
    with with_db(schema) as db:
        reg = _nueva_caja(db, "negativo")
        registers.append(reg.id)
        user = db.get(User, user_id)

        body = ShiftOpen(cash_register_id=reg.id, opening_amount=Decimal("-50000"))
        _expect_422(lambda: open_shift(body=body, db=db, user=user), "opening_amount=-50000 -> 422")
        db.rollback()

        creados = db.execute(
            select(CashShift).where(CashShift.cash_register_id == reg.id)
        ).scalars().all()
        _check("ningún CashShift creado (caso 2)", len(creados), 0)
    print("OK: test_opening_amount_negativo_rechaza_422")


def test_opening_amount_omitido_rechaza_422(schema: str, user_id, registers: list) -> None:
    """Caso 3: se omite opening_amount del cuerpo -> cae en el default 0,
    mismo tratamiento que el caso 1 (spec.md, Edge Cases)."""
    with with_db(schema) as db:
        reg = _nueva_caja(db, "omitido")
        registers.append(reg.id)
        user = db.get(User, user_id)

        body = ShiftOpen(cash_register_id=reg.id)
        _check("el default de opening_amount sigue siendo 0", body.opening_amount, Decimal("0"))
        _expect_422(lambda: open_shift(body=body, db=db, user=user), "opening_amount omitido -> 422")
        db.rollback()
    print("OK: test_opening_amount_omitido_rechaza_422")


def test_opening_amount_valido_crea_turno(schema: str, user_id, registers: list) -> None:
    """Caso 4: opening_amount=50000 -> se crea el CashShift con ese valor,
    sin cambios respecto al comportamiento actual."""
    with with_db(schema) as db:
        reg = _nueva_caja(db, "valido")
        registers.append(reg.id)
        user = db.get(User, user_id)

        body = ShiftOpen(cash_register_id=reg.id, opening_amount=Decimal("50000"))
        shift = open_shift(body=body, db=db, user=user)
        db.commit()
        _check("CashShift creado con opening_amount=50000", shift.opening_amount, Decimal("50000"))
    print("OK: test_opening_amount_valido_crea_turno")


def test_opening_amount_minimo_valido_crea_turno(schema: str, user_id, registers: list) -> None:
    """Caso 5: opening_amount=0.01 (válido pero mínimo) -> se crea el turno;
    confirma que el límite es estrictamente > 0, sin piso de negocio adicional."""
    with with_db(schema) as db:
        reg = _nueva_caja(db, "minimo")
        registers.append(reg.id)
        user = db.get(User, user_id)

        body = ShiftOpen(cash_register_id=reg.id, opening_amount=Decimal("0.01"))
        shift = open_shift(body=body, db=db, user=user)
        db.commit()
        _check("CashShift creado con opening_amount=0.01", shift.opening_amount, Decimal("0.01"))
    print("OK: test_opening_amount_minimo_valido_crea_turno")


def test_check_constraint_rechaza_insert_directo(schema: str, user_id, registers: list) -> None:
    """Caso 6: INSERT directo (fuera del endpoint) con opening_amount<=0 ->
    falla por el CheckConstraint de base de datos, defensa en profundidad."""
    with with_db(schema) as db:
        reg = _nueva_caja(db, "constraint")
        registers.append(reg.id)
        user = db.get(User, user_id)

        shift = CashShift(
            cash_register_id=reg.id, user_id=user.id, user_name=user.name,
            opening_amount=Decimal("0"), status="open",
        )
        db.add(shift)
        try:
            db.commit()
            raise AssertionError("el INSERT directo con opening_amount=0 no falló")
        except IntegrityError:
            db.rollback()
            print(f"  ok  · INSERT directo con opening_amount=0 falla por {CONSTRAINT}")
    print("OK: test_check_constraint_rechaza_insert_directo")


def test_turno_historico_no_afectado_por_migracion(schema: str, user_id, registers: list) -> None:
    """Caso 7: un CashShift histórico con opening_amount=0 (simulado quitando
    momentáneamente el constraint, insertando la fila, y re-aplicando
    exactamente el mismo `CHECK ... NOT VALID` que la migración de esta spec)
    sigue existiendo sin modificarse, y re-crear la restricción no falla
    (FR-007)."""
    with with_db(schema) as db:
        reg = _nueva_caja(db, "historico")
        registers.append(reg.id)
        user = db.get(User, user_id)

        # Simula el estado anterior a esta spec (constraint laxo, >= 0).
        db.execute(text(f'ALTER TABLE "{schema}".cash_shifts DROP CONSTRAINT {CONSTRAINT}'))
        db.execute(text(
            f'ALTER TABLE "{schema}".cash_shifts ADD CONSTRAINT {CONSTRAINT} '
            f'CHECK (opening_amount >= 0)'
        ))
        db.commit()

        historico = CashShift(
            cash_register_id=reg.id, user_id=user.id, user_name=user.name,
            opening_amount=Decimal("0"), status="open",
        )
        db.add(historico)
        db.commit()
        historico_id = historico.id

        # Re-aplica exactamente lo que hace la migración de esta spec (T009).
        db.execute(text(f'ALTER TABLE "{schema}".cash_shifts DROP CONSTRAINT {CONSTRAINT}'))
        db.execute(text(
            f'ALTER TABLE "{schema}".cash_shifts ADD CONSTRAINT {CONSTRAINT} '
            f'CHECK (opening_amount > 0) NOT VALID'
        ))
        db.commit()

        sigue = db.get(CashShift, historico_id)
        _check("el turno histórico sigue existiendo", sigue is not None, True)
        _check("su opening_amount no cambió", sigue.opening_amount, Decimal("0"))
    print("OK: test_turno_historico_no_afectado_por_migracion")


def main() -> None:
    schema = _schema()
    print(f"Validación de saldo inicial > 0 al abrir caja (tenant: {schema})")

    with with_db(schema) as db:
        user = db.execute(select(User).limit(1)).scalar_one_or_none()
        if user is None:
            raise SystemExit("No hay usuarios en shared.users.")
        user_id = user.id

    registers: list = []
    tests = [
        test_opening_amount_cero_rechaza_422,
        test_opening_amount_negativo_rechaza_422,
        test_opening_amount_omitido_rechaza_422,
        test_opening_amount_valido_crea_turno,
        test_opening_amount_minimo_valido_crea_turno,
        test_check_constraint_rechaza_insert_directo,
        test_turno_historico_no_afectado_por_migracion,
    ]
    fallos = 0
    try:
        for test in tests:
            try:
                test(schema, user_id, registers)
            except AssertionError as exc:
                fallos += 1
                print(f"FALLÓ: {test.__name__}: {exc}")
    finally:
        _cleanup(schema, registers)

    if fallos:
        raise SystemExit(f"{fallos}/{len(tests)} pruebas fallaron")
    print(f"Todas las {len(tests)} pruebas pasaron.")


if __name__ == "__main__":
    main()
