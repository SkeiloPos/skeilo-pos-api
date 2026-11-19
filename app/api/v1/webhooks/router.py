"""Webhooks de pasarelas externas (spec 161, Historia de Usuario 4; contrato `wompi-webhook.md § 2`).

`POST /api/v1/webhooks/wompi` es **público**: Wompi lo llama sin `x-tenant-host` y sin token, así que no
depende de `get_tenant`, nunca pasa por el guard de suscripción y funciona aunque el tenant esté bloqueado
—justo cuando más se necesita, porque es lo que lo desbloquea al aprobar el pago—. Su única autenticación es
la firma del evento (`wompi.verify_event`).

Solo Wompi cuenta como éxito con **HTTP 200**: ante cualquier otra respuesta reintenta 3 veces en 24 h. Por eso
todo evento con firma válida responde 200 —procesado, duplicado o sin efecto—, y solo la firma o la estructura
inválidas responden 4xx (esos reintentos no hacen daño: un evento forjado no debe tener efecto).
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.v1.plan import subscription_service
from app.core import wompi
from app.core.config import settings
from app.core.dependencies import get_shared_db

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: Único tipo de evento de Wompi que actúa sobre un pago. Los demás (`nequi_token.updated`, ...) se confirman
#: con 200 y no hacen nada.
TRANSACTION_UPDATED = "transaction.updated"


@router.post(
    "/wompi",
    summary="Recibir un evento de Wompi (confirmación de pago de suscripción)",
    description=(
        "Endpoint público para Wompi. Verifica la firma del evento (checksum SHA-256 con el secreto de eventos), "
        "es idempotente por referencia y, al aprobarse el pago, recalcula la vigencia del tenant y lo desbloquea "
        "de inmediato. `200` si el evento es válido (procesado, duplicado o sin efecto); `400` si la estructura es "
        "inválida; `401` si la firma no coincide o no hay secreto configurado."
    ),
    responses={
        200: {"description": "Evento válido. `{\"status\": \"processed\" | \"duplicate\" | \"ignored\"}`."},
        400: {"description": "Cuerpo ilegible, estructura incompleta o `properties` firmados insuficientes."},
        401: {"description": "Firma inválida, o `WOMPI_EVENTS_SECRET` sin configurar."},
    },
)
async def receive_wompi_event(request: Request, db: Session = Depends(get_shared_db)):
    try:
        event = json.loads(await request.body())
    except ValueError:  # JSON inválido, vacío o no UTF-8
        raise HTTPException(status_code=400, detail="Cuerpo inválido")

    try:
        wompi.verify_event(event, request.headers.get("x-event-checksum"), settings.WOMPI_EVENTS_SECRET)
    except wompi.WompiEventError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    if event.get("event") != TRANSACTION_UPDATED:
        return {"status": "ignored"}

    # La base de datos es síncrona: se despacha al pool de hilos para no bloquear el event loop.
    result = await run_in_threadpool(subscription_service.apply_wompi_event, db, event)
    return {"status": result}
