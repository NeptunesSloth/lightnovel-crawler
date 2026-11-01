import json
import socket
from typing import Any, Dict

from fastapi import APIRouter, Body, Request, Security
from fastapi.responses import Response

from ...context import ctx
from ...dao import NotificationItem, User
from ...exceptions import ServerErrors
from ...server.models import PutNotificationRequest, UpdateRequest
from ...services import reader_state
from ..security import ensure_user

# The root router
router = APIRouter()


def _lan_ip() -> str:
    """Best-effort LAN IP of this machine (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@router.get("/lan", summary="LAN address of this server (read on your phone)")
def lan_info(request: Request) -> Dict[str, str]:
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    return {"url": f"http://{_lan_ip()}:{port}/reader"}


@router.get("/lan-qr", summary="QR code (SVG) of the LAN reader address")
def lan_qr(request: Request) -> Response:
    import io

    import qrcode
    import qrcode.image.svg

    url = lan_info(request)["url"]
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=16)
    buf = io.BytesIO()
    img.save(buf)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


@router.put(
    "/notifications",
    summary="Save user notification settings",
)
def put_notification_settings(
    user: User = Security(ensure_user), body: PutNotificationRequest = Body()
) -> bool:
    request = UpdateRequest(
        extra=dict(
            email_alerts={
                NotificationItem(int(k)): 1 if v else 0
                for k, v in body.email_alerts.items()
                if v and int(k) in list(NotificationItem)
            }
        )
    )
    ctx.users.update(user.id, request)
    return True


@router.get("/reader-state", summary="The Reader's saved state for this user, across devices")
def get_reader_state(user: User = Security(ensure_user)) -> Dict[str, Any]:
    with ctx.db.session() as sess:
        current = sess.get(User, user.id)
        return dict((current.extra or {}).get("reader_state") or {}) if current else {}


@router.put(
    "/reader-state",
    summary="Merge this device's Reader state into the saved one and return the result",
)
def put_reader_state(
    user: User = Security(ensure_user),
    body: Dict[str, Any] = Body(default={}),
) -> Dict[str, Any]:
    if len(json.dumps(body)) > reader_state.MAX_STATE_BYTES:
        raise ServerErrors.invalid_input.with_extra("Reader state is too large")
    with ctx.db.session() as sess:
        current = sess.get(User, user.id)
        if not current:
            raise ServerErrors.unauthorized
        extra = dict(current.extra or {})
        merged = reader_state.merge(extra.get("reader_state") or {}, body)
        extra["reader_state"] = merged
        current.extra = extra
        sess.add(current)
        sess.commit()
        return merged
