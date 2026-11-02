from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from confluent_kafka import Consumer, KafkaError, TopicPartition
from sqlalchemy.orm import Session

from app.access import bump_auth_version
from app.database import SessionLocal
from app.models import AccessConsumerState, AccessEventInbox, User, UserLoginMethod
from app.settings import settings

logger = logging.getLogger(settings.service_name)

TOPIC = "people.access.v1"
RETRY_DELAY_SECONDS = 5.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def handle_identity_changed(db: Session, payload: dict[str, Any], aggregate_id: int) -> None:
    display_name = payload.get("display_name")
    name_version = payload.get("name_version")
    if display_name is None:
        return
    user = (
        db.query(User)
        .filter(User.person_id == aggregate_id, User.merged_into_user_id.is_(None))
        .one_or_none()
    )
    if user is None:
        return
    incoming_version = int(name_version or 0)
    if user.name_version and incoming_version and incoming_version < int(user.name_version):
        return
    user.display_name = str(display_name)[:255]
    if name_version is not None:
        user.name_version = incoming_version


def handle_merged(db: Session, payload: dict[str, Any], aggregate_id: int) -> None:
    duplicate_person_id = payload.get("duplicate_person_id") or aggregate_id
    canonical_person_id = payload.get("canonical_person_id")
    if duplicate_person_id is None or canonical_person_id is None:
        return
    duplicate_person_id = int(duplicate_person_id)
    canonical_person_id = int(canonical_person_id)
    if duplicate_person_id == canonical_person_id:
        return

    duplicate = (
        db.query(User)
        .filter(User.person_id == duplicate_person_id, User.merged_into_user_id.is_(None))
        .one_or_none()
    )
    if duplicate is None:
        return
    canonical = (
        db.query(User)
        .filter(User.person_id == canonical_person_id, User.merged_into_user_id.is_(None))
        .one_or_none()
    )
    if canonical is None:
        duplicate.person_id = canonical_person_id
        bump_auth_version(duplicate)
        return
    if canonical.id == duplicate.id:
        return

    existing_identifiers = {
        method.identifier
        for method in db.query(UserLoginMethod)
        .filter(UserLoginMethod.user_id == canonical.id, UserLoginMethod.verified_at.isnot(None))
        .all()
    }
    for method in db.query(UserLoginMethod).filter(UserLoginMethod.user_id == duplicate.id).all():
        if method.verified_at is None or method.identifier in existing_identifiers:
            continue
        method.user_id = canonical.id
        existing_identifiers.add(method.identifier)

    duplicate.is_active = False
    duplicate.merged_into_user_id = canonical.id
    bump_auth_version(duplicate)
    bump_auth_version(canonical)


HANDLERS = {
    "people.identity.changed": handle_identity_changed,
    "people.merged": handle_merged,
}


def apply_access_event(event: dict[str, Any], session_factory=None) -> str:
    factory = session_factory or SessionLocal
    event_id = event.get("event_id")
    aggregate_id = event.get("aggregate_id")
    aggregate_version = event.get("aggregate_version")
    if not event_id or aggregate_id is None or aggregate_version is None:
        logger.warning("Rejected malformed access event", extra={"event_id": event_id})
        return "invalid"

    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = event.get("event_type") or ""
    aggregate_id = int(aggregate_id)
    aggregate_version = int(aggregate_version)

    with factory() as db:
        if db.query(AccessEventInbox).filter(AccessEventInbox.event_id == event_id).one_or_none() is not None:
            return "duplicate"

        state = (
            db.query(AccessConsumerState)
            .filter(AccessConsumerState.aggregate_id == aggregate_id)
            .with_for_update()
            .one_or_none()
        )
        last_version = int(state.last_version) if state is not None else 0
        if aggregate_version <= last_version:
            return "stale"
        if aggregate_version != last_version + 1:
            return "gap"

        handler = HANDLERS.get(event_type)
        if handler is None:
            logger.info("Ignoring unsupported access event type", extra={"event_type": event_type})
        else:
            handler(db, payload, aggregate_id)

        db.add(
            AccessEventInbox(
                event_id=event_id,
                event_type=event_type,
                aggregate_id=aggregate_id,
                aggregate_version=aggregate_version,
                payload=payload,
            )
        )
        if state is None:
            db.add(
                AccessConsumerState(
                    aggregate_id=aggregate_id,
                    last_version=aggregate_version,
                    last_event_id=event_id,
                )
            )
        else:
            state.last_version = aggregate_version
            state.last_event_id = event_id
            state.updated_at = utcnow()
        db.commit()
        return "applied"


def run_access_consumer() -> None:
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": "oms1.people.access",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([TOPIC])
    logger.info("Started OMS1 Kafka consumer for %s", TOPIC)
    try:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                if message.error().code() != KafkaError._PARTITION_EOF:
                    logger.error("Kafka consumer error: %s", message.error())
                continue
            try:
                event = json.loads(message.value().decode("utf-8"))
            except json.JSONDecodeError:
                logger.exception("Rejected OMS1 access event: invalid JSON")
                consumer.commit(message=message, asynchronous=False)
                continue

            try:
                result = apply_access_event(event)
            except Exception:
                logger.exception(
                    "Failed to apply OMS1 access event; retrying without commit",
                    extra={"event_id": event.get("event_id")},
                )
                consumer.seek(TopicPartition(message.topic(), message.partition(), message.offset()))
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            if result == "gap":
                logger.error(
                    "Access event version gap; offset is not committed until replay",
                    extra={
                        "event_id": event.get("event_id"),
                        "aggregate_id": event.get("aggregate_id"),
                        "aggregate_version": event.get("aggregate_version"),
                        "error_code": "access_event_version_gap",
                    },
                )
                consumer.seek(TopicPartition(message.topic(), message.partition(), message.offset()))
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            consumer.commit(message=message, asynchronous=False)
    finally:
        consumer.close()
