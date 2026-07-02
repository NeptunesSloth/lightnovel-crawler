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
    """Best-effort LAN IP of this machine — works with NO internet at all.

    The default-route trick alone fails when there is no internet (e.g. reading
    on the go via a laptop/phone hotspot), so also enumerate the hostname's
    addresses and prefer hotspot/private ranges. 192.168.137.x is the Windows
    Mobile-hotspot subnet — exactly the no-Wi-Fi-at-the-gym case.
    """
    ips: list = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet is actually sent
        ips.append(s.getsockname()[0])
    except Exception:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except Exception:
        pass

    def rank(ip: str) -> int:
        if ip.startswith("192.168.137."):
            return 0  # Windows Mobile hotspot
        if ip.startswith("192.168."):
            return 1
        if ip.startswith("10."):
            return 2
        if ip.startswith("172."):
            return 3
        return 4

    seen = []
    for ip in ips:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip not in seen:
            seen.append(ip)
    seen.sort(key=rank)
    return seen[0] if seen else "127.0.0.1"


@router.get("/lan", summary="LAN address of this server (read on your phone)")
def lan_info(request: Request) -> Dict[str, str]:
    """LAN reader URL, with the caller's own session token embedded.

    Scanning the QR then signs the phone in automatically — no password typing.
    The reader page strips the token from the address bar immediately on load.
    Only ever returned to an already-authenticated caller, over the local network.
    """
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    url = f"http://{_lan_ip()}:{port}/reader"
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            url += f"?authToken={token}"
    return {"url": url}


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
