from uuid import UUID

from celery import Celery

from app.core.db import with_db
from app.core.mail import send_email, create_message
from app.core.timezone import utc_now_naive
from app.api.v1.inventory.import_ import procesar_archivo_importacion
from app.models.inventory_import_job import InventoryImportJob

c_app = Celery("pos")

c_app.config_from_object("app.core.config")


@c_app.task
def send_email_task(recipients: list[str], subject: str, body: str, text_body: str | None = None):
    # text_body opcional (research.md Decisión 5): una tarea encolada con la
    # firma antigua antes del despliegue sigue ejecutándose sin error.
    message = create_message(recipients=recipients, subject=subject, body=body, text_body=text_body)
    send_email(message)
    print("Email sent")


@c_app.task
def import_inventory_task(tenant_schema: str, job_id: str, filas: list[list]):
    """spec 109: procesa en segundo plano un archivo de importación de
    inventario ya validado en estructura (FR-005, síncrono, en el router). Las
    filas viajan ya parseadas (no el archivo binario) para que el argumento de
    la tarea sea JSON-serializable, igual que el resto de tareas de este
    proyecto -- `filas` son valores planos (str/int/float/None) tal cual los
    devuelve `openpyxl`, sin ningún tipo que el serializador JSON no entienda."""
    with with_db(tenant_schema) as db:
        job = db.get(InventoryImportJob, UUID(job_id))
        try:
            procesar_archivo_importacion(db, job, filas)
        except Exception:
            db.rollback()
            job = db.get(InventoryImportJob, UUID(job_id))
            job.status = "interrumpido"
            job.finished_at = utc_now_naive()
            db.commit()
            raise
    print(f"Import {job_id} finalizado")
