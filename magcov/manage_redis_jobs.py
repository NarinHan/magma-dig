#!/usr/bin/env python3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def queue_key(namespace):
    return "{0}:job_queue".format(namespace)


def processing_key(namespace):
    return "{0}:processing".format(namespace)


def job_key(namespace, job_id):
    return "{0}:job:{1}".format(namespace, job_id)


def done_channel(namespace):
    return "{0}:job_done".format(namespace)


def event_channel(namespace):
    return "{0}:job_events".format(namespace)


def read_json(path):
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object in {0}".format(path))
    return data


def decode_redis_hash(raw):
    decoded = {}
    for key, value in raw.items():
        if isinstance(key, (bytes, bytearray)):
            key_str = key.decode("utf-8")
        else:
            key_str = str(key)

        if isinstance(value, (bytes, bytearray)):
            value_str = value.decode("utf-8")
        else:
            value_str = str(value)

        decoded[key_str] = value_str
    return decoded


def set_job_fields(redis_client, namespace, job_id, fields):
    key = job_key(namespace, job_id)
    encoded = {}
    for key_name, value in fields.items():
        encoded[str(key_name)] = "" if value is None else str(value)
    if encoded:
        redis_client.hset(key, mapping=encoded)


def enqueue_job(redis_client, namespace, job_id):
    redis_client.lpush(queue_key(namespace), job_id)


def publish_event(redis_client, namespace, payload):
    redis_client.publish(
        event_channel(namespace),
        json.dumps(payload, ensure_ascii=False),
    )


def publish_done(redis_client, namespace, payload):
    redis_client.publish(
        done_channel(namespace),
        json.dumps(payload, ensure_ascii=False),
    )
