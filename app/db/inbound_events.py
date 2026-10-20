import base64
import hashlib

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clients.sftp_orders import SFTPOrderFile
from app.models import InboundEvent, InboundEventSource


def record_sftp_files(session: Session, files: list[SFTPOrderFile]) -> int:
    recorded = 0
    for file in files:
        statement = (
            insert(InboundEvent)
            .values(
                source=InboundEventSource.SFTP,
                external_event_id=file.path,
                payload={
                    "path": file.path,
                    "xml_base64": base64.b64encode(file.content).decode("ascii"),
                    "sha256": hashlib.sha256(file.content).hexdigest(),
                },
            )
            .on_conflict_do_nothing(constraint="uq_inbound_events_source_external_id")
            .returning(InboundEvent.id)
        )
        if session.scalar(statement) is not None:
            recorded += 1
    return recorded


def known_sftp_paths(session: Session) -> set[str]:
    return set(
        session.scalars(
            select(InboundEvent.external_event_id).where(
                InboundEvent.source == InboundEventSource.SFTP
            )
        )
    )
