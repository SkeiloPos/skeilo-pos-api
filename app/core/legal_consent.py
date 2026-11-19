"""Huella no reversible de la IP del cliente final (spec 162, FR-018,
research.md Decisión 5): `sha256(ip + CONSENT_IP_HASH_SALT)`, determinista
(comparable entre dos aceptaciones) pero no reversible sin el salt, que solo
vive en configuración del servidor (`CONSENT_IP_HASH_SALT`), nunca persistido
junto al hash."""
import hashlib


def ip_hash(ip: str, salt: str) -> str:
    return hashlib.sha256(f"{ip}{salt}".encode("utf-8")).hexdigest()
