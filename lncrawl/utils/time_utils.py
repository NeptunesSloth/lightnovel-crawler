from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc)


def current_timestamp():
    """Current UNIX timestamp in milliseconds"""
    return round(1000 * utc_now().timestamp())
