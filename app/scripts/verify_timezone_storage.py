"""Compuerta 0 y verificación del almacenamiento en UTC (spec 138, FR-004, quickstart Q2/Q10).

    python -m app.scripts.verify_timezone_storage
    PGTZ=America/Bogota python -m app.scripts.verify_timezone_storage     # zona «hostil» del cliente

Comprueba, contra PostgreSQL de verdad, que lo que guarda la aplicación no depende de la
zona de la sesión del motor (que hereda de `PGTZ`, del rol o de la base):

  Informativo — una conexión SIN forzar (la que abriría cualquier cliente ajeno a la app):
    su zona, de dónde viene, y el defecto original (unas 17:00 UTC guardadas como 12:00).
  Verificaciones — con la conexión de la APLICACIÓN (`app.core.db.engine`):
    (1) la zona de la sesión es UTC;
    (2) un `datetime` aware de 2026-10-25 17:00 UTC se guarda como 17:00;
    (3) `now()` como valor por defecto de una columna sin zona coincide con la hora UTC real;
    (4) la zona de cada tenant es válida para Python (`validate_iana_timezone`) y existe en
        `pg_timezone_names`, incluidas las siete de la lista curada.
  Informativo — (5) si existe el disparador `trg_tenants_validate_timezone`.

Solo lectura salvo una tabla `TEMP` (desaparece al cerrar la conexión). Termina con código distinto
de cero si falla alguna de (1)–(4); la última línea es `OK` si todo pasó.

**Compuerta 0 (antes de desplegar):** correrlo en el servidor de producción
(`docker compose exec api python -m app.scripts.verify_timezone_storage`). Si falla (1)–(3), la
sesión histórica de producción no era UTC o los datos ya guardados no lo están: se pausa solo
FR-004/FR-005 en producción y la corrección de datos va en una spec aparte (Principios VII y VIII).
"""
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.core.config import settings
from app.core.db import engine
from app.core.timezone import ZONAS_CURADAS, utc_now_naive, validate_iana_timezone

INSTANTE_PRUEBA = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
TOLERANCIA_NOW_SEGUNDOS = 120
DISPARADOR = "trg_tenants_validate_timezone"

_fallos: list[str] = []


def _verificar(numero: str, descripcion: str, ok: bool, detalle: str = "") -> None:
    print(f"  [{'OK' if ok else 'FALLA'}] ({numero}) {descripcion}" + (f" — {detalle}" if detalle else ""))
    if not ok:
        _fallos.append(f"({numero}) {descripcion}")


def _guardar_y_leer(conexion) -> tuple[datetime, datetime]:
    """Guarda `INSTANTE_PRUEBA` en una columna sin zona y deja que otra tome `now()`.
    Devuelve (instante guardado, valor por defecto `now()`)."""
    conexion.execute(text("DROP TABLE IF EXISTS _verify_tz"))
    conexion.execute(text(
        "CREATE TEMP TABLE _verify_tz ("
        "id int PRIMARY KEY, guardado timestamp, por_defecto timestamp DEFAULT now())"
    ))
    conexion.execute(
        text("INSERT INTO _verify_tz (id, guardado) VALUES (1, :instante)"),
        {"instante": INSTANTE_PRUEBA},
    )
    return conexion.execute(text("SELECT guardado, por_defecto FROM _verify_tz WHERE id = 1")).one()


def _informe_sin_forzar() -> None:
    print("Conexión SIN forzar (informativo: lo que haría un cliente ajeno a la aplicación)")
    crudo = create_engine(settings.DATABASE_URL, future=True)  # sin el evento `connect` de la app
    try:
        with crudo.connect() as conexion:
            zona = conexion.execute(text("SHOW TimeZone")).scalar_one()
            origen = conexion.execute(
                text("SELECT source FROM pg_settings WHERE name = 'TimeZone'")
            ).scalar_one()
            print(f"  zona de la sesión: {zona} (origen: {origen})")
            try:
                for base, rol, config in conexion.execute(text(
                    "SELECT d.datname, r.rolname, s.setconfig FROM pg_db_role_setting s "
                    "LEFT JOIN pg_database d ON d.oid = s.setdatabase "
                    "LEFT JOIN pg_roles r ON r.oid = s.setrole"
                )):
                    print(f"  pg_db_role_setting: base={base} rol={rol} config={config}")
            except Exception as exc:  # solo informativo
                print(f"  pg_db_role_setting no disponible: {exc.__class__.__name__}")
            guardado, _ = _guardar_y_leer(conexion)
            efecto = "sin desfase" if guardado == INSTANTE_PRUEBA.replace(tzinfo=None) else "CON desfase"
            print(f"  17:00 UTC se guardaría como {guardado:%H:%M} ({efecto})")
    finally:
        crudo.dispose()


def main() -> int:
    print("Verificación del almacenamiento en UTC (spec 138)")
    print()
    _informe_sin_forzar()
    print()
    print("Conexión de la APLICACIÓN (app.core.db.engine)")
    with engine.connect() as conexion:
        zona = conexion.execute(text("SHOW TimeZone")).scalar_one()
        _verificar("1", "la zona de la sesión es UTC", zona == "UTC", f"zona: {zona}")

        guardado, por_defecto = _guardar_y_leer(conexion)
        _verificar(
            "2", "17:00 UTC se guarda como 17:00",
            guardado == INSTANTE_PRUEBA.replace(tzinfo=None), f"guardado: {guardado:%Y-%m-%d %H:%M}",
        )
        desfase = abs((por_defecto - utc_now_naive()).total_seconds())
        _verificar(
            "3", "now() (valor por defecto sin zona) coincide con la hora UTC real",
            desfase < TOLERANCIA_NOW_SEGUNDOS, f"diferencia: {desfase:.1f} s",
        )

        en_postgres = {r[0] for r in conexion.execute(text("SELECT name FROM pg_timezone_names"))}
        tenants = conexion.execute(text("SELECT host, timezone FROM shared.tenants ORDER BY id")).all()
        invalidos = []
        for host, zona_tenant in tenants:
            try:
                validate_iana_timezone(zona_tenant)
                valida_python = True
            except ValueError:
                valida_python = False
            if not valida_python or zona_tenant not in en_postgres:
                invalidos.append(f"{host}={zona_tenant!r}")
        _verificar(
            "4", f"la zona de cada uno de los {len(tenants)} tenants es válida (Python y PostgreSQL)",
            not invalidos, ", ".join(invalidos),
        )
        faltantes = [z.iana for z in ZONAS_CURADAS if z.iana not in en_postgres]
        _verificar(
            "4", f"las {len(ZONAS_CURADAS)} zonas de la lista curada existen en PostgreSQL",
            not faltantes, ", ".join(faltantes),
        )

        hay_disparador = conexion.execute(text(
            "SELECT EXISTS (SELECT 1 FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'shared' AND c.relname = 'tenants' AND t.tgname = :nombre "
            "AND NOT t.tgisinternal)"
        ), {"nombre": DISPARADOR}).scalar_one()
        print(f"  [info] (5) disparador {DISPARADOR}: {'existe' if hay_disparador else 'no existe'}")

    print()
    if _fallos:
        print("FALLÓ:", "; ".join(_fallos))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
