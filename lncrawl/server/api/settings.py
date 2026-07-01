import socket
from typing import Dict

from fastapi import APIRouter, Body, Request, Security
from fastapi.responses import Response

from ...context import ctx
from ...dao import NotificationItem, User
from ...server.models import PutNotificationRequest, UpdateRequest
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
