from celery import Celery

from app.core.mail import send_email, create_message

c_app = Celery("pos")

c_app.config_from_object("app.core.config")


@c_app.task
def send_email_task(recipients: list[str], subject: str, body: str, text_body: str | None = None):
    # text_body opcional (research.md Decisión 5): una tarea encolada con la
    # firma antigua antes del despliegue sigue ejecutándose sin error.
    message = create_message(recipients=recipients, subject=subject, body=body, text_body=text_body)
    send_email(message)
    print("Email sent")
