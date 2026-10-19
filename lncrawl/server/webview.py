from contextlib import suppress
import json
import logging
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread
import time
from typing import Optional
from urllib.request import urlopen

from ..assets.htmls import loading_path
from ..context import ctx
from ..enums import UserRole
from ..services.logger import log_to_file
from ..utils.browser_detect import pick_executable
from ..utils.platforms import Platform, Screen
from ..utils.sockets import free_port

logger = logging.getLogger(__name__)

# How long each half of the handshake may take. The browser answers on its
# debugging port moments after starting, so a short wait there is honest. The
# server behind it is not comparable: it runs database migrations and loads
# every source before it binds, and on a first Windows launch Defender scans the
# whole freshly-unpacked bundle while it does. Measured cold, that boot is tens
# of seconds; a timeout that cuts it short is indistinguishable from a crash to
# the person watching, because all they see is the loading window closing.
CDP_TIMEOUT = 30.0
SERVER_TIMEOUT = 600.0

# Bind on all interfaces so phones/tablets on the same Wi-Fi can open the
# Reader; the app window itself still connects over loopback. The whole API
# stays behind sign-in, so LAN exposure is auth-gated. The port must be chosen
# against this same address — see free_port.
SERVER_HOST = "0.0.0.0"

# Set when the server thread dies, so the waits below can fail immediately with
# the real cause instead of sitting out the full timeout on a server that is
# never going to bind.
_server_error: Optional[BaseException] = None


class FallbackException(Exception):
    """The app window is unavailable; serve the UI in the default browser."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)


def _listening_socket(host: str, port: int) -> socket.socket:
    """Open the listening socket here rather than leaving it to uvicorn.

    uvicorn answers a failed bind with `logger.error(exc)` and then `sys.exit(1)`
    (server.py and Config.bind_socket both). Its loggers are configured with
    propagate=False onto a stderr the desktop app has already pointed at the null
    device, so the reason is written nowhere and the thread dies with a bare
    SystemExit(1). Binding here keeps the OSError and its message, and handing
    the open socket to uvicorn also closes the window between choosing a port
    and using it.

    SO_REUSEADDR is deliberately not set: on Windows it lets a second process
    bind a port another one is already serving and silently take its traffic.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.listen(2048)
    except OSError as e:
        sock.close()
        raise RuntimeError(f"Could not listen on {host}:{port} — {e}") from e
    sock.set_inheritable(True)
    return sock


def _serve(host: str, port: int) -> None:
    import uvicorn

    from .app import app as server_app

    sock = _listening_socket(host, port)
    # log_config=None leaves logging alone. uvicorn's default config would
    # install its own handlers and stop its records propagating, which is what
    # kept bind failures out of app.log; left alone, uvicorn.error reaches the
    # root logger and is recorded with everything else.
    config = uvicorn.Config(
        server_app,
        host=host,
        port=port,
        log_config=None,
        access_log=ctx.logger.is_debug,
        log_level=ctx.logger.level or "error",
    )
    uvicorn.Server(config).run(sockets=[sock])


def _start_server(host: str, port: int) -> None:
    global _server_error

    try:
        ctx.setup(
            log_level=0,
            reset_db_on_failure=True,
        )
        _serve(host, port)
    except BaseException as e:  # noqa: BLE001 - the thread is the last handler
        # Nothing joins this thread, so an exception here is otherwise lost
        # entirely and shows up only as a server that never answers. SystemExit
        # counts: a library that exits the interpreter on failure must not be
        # allowed to look like a clean shutdown.
        _server_error = e
        logger.exception("Server thread stopped")


def _wait_to_connect(host: str, port: int, timeout: float) -> bool:
    """Whether something accepted a connection on `port` before `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_error is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _wait_for_server(host: str, port: int) -> None:
    if _wait_to_connect(host, port, SERVER_TIMEOUT):
        return
    if _server_error is not None:
        raise RuntimeError(f"The server failed to start: {_server_error!r}")
    raise RuntimeError(f"The server did not start within {SERVER_TIMEOUT:.0f}s")


def _build_url(host: str, port: int) -> str:
    _wait_for_server(host, port)
    token = ctx.users.generate_token(
        user=ctx.users.get_admin(),
        expiry_minutes=100 * 365 * 24 * 60,  # 100 years
        scopes=[UserRole.LOCAL],
    )
    return f"http://{host}:{port}/?authToken={token}"


def _page_target(host: str, cdp_port: int) -> str:
    """The debugger URL of the window we just opened.

    `/json` lists every target the browser knows about, in no guaranteed order
    and including ones that cannot be navigated: service workers, extension
    pages, and any target already attached to a debugger, which is listed
    without a `webSocketDebuggerUrl` at all. Taking the first entry on trust is
    how `Page.navigate` ends up addressed to a service worker, or raises
    KeyError on a target that has no socket to send it on.
    """
    with urlopen(f"http://{host}:{cdp_port}/json", timeout=10) as resp:
        targets = json.loads(resp.read())

    for target in targets:
        ws_url = target.get("webSocketDebuggerUrl")
        if ws_url and target.get("type") == "page":
            logger.info(f"CDP URL={ws_url}")
            return ws_url

    raise FallbackException("The browser exposed no page target to navigate")


def _close_app_window(proc: "subprocess.Popen", ws_url: Optional[str]) -> None:
    """Shut the app window, whoever ends up owning it.

    Killing `proc` is not always enough. When a browser is already running with
    this user-data-dir, the process we launch hands the window over to that
    instance and exits immediately — so there is nothing left for us to kill and
    the loading window stays on screen for good. While the debugger socket is
    still reachable, ask the page to close itself instead.
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(2)
        except BaseException:
            proc.kill()
        return

    if not ws_url:
        return
    try:
        from websockets.sync.client import connect as ws_connect

        with ws_connect(ws_url, open_timeout=5) as ws:
            ws.send(json.dumps({"id": 2, "method": "Page.close"}))
    except Exception as e:
        logger.info(f"Could not close the app window over CDP: {e}")


def _start_app_in_browser(
    host: str,
    port: int,
    cdp_port: int,
    storage_path: Path,
) -> None:
    from websockets.sync.client import connect as ws_connect

    # get chrome-like browser binary
    binary = pick_executable()
    if not binary:
        raise FallbackException("No chrome-like binary found")

    # Ask the OS for the screen before sizing against it: untouched, Screen
    # holds its 1920x1080 placeholders, and on a smaller display that builds a
    # window larger than the screen it opens on.
    if not Platform.has_display:
        logger.info("No display detected; sizing the app window from defaults")

    # start process
    logger.info(f"Opening app-mode browser: {binary}")
    width = min(1400, Screen.view_width - 20)
    height = min(1000, Screen.view_height - 80)
    args = [
        str(binary),
        f"--app={loading_path().as_uri()}",
        "--new-window",
        f"--window-size={width},{height}",
        f"--user-data-dir={storage_path}",
        f"--remote-debugging-port={cdp_port}",
    ]
    proc = subprocess.Popen(args)
    logger.info(f"Started app (pid={proc.pid})")

    ws_url: Optional[str] = None
    try:
        #  wait for CDP to be ready
        if not _wait_to_connect(host, cdp_port, CDP_TIMEOUT):
            raise FallbackException(
                f"The browser did not open its debugging port within {CDP_TIMEOUT:.0f}s"
            )

        # get CDP url
        ws_url = _page_target(host, cdp_port)

        # wait for the server
        url = _build_url(host, port)
        logger.info(f"Launching URL: {url}")

        # send URL via CDP
        with ws_connect(ws_url) as ws:
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Page.navigate",
                        "params": {"url": url},
                    }
                )
            )

        # wait for exit
        proc.wait()
    finally:
        _close_app_window(proc, ws_url)
        logger.info(f"Closed app (pid={proc.pid}): {proc.poll()}")


def _has_console() -> bool:
    """Whether there is a terminal to print to and read from.

    The frozen app calls `FreeConsole` before any of this runs, so the console
    it was launched with is gone even though stdin/stdout still exist as
    objects. Printing there reaches nobody and `input()` returns instantly at
    EOF, which would end the process on the very path meant to keep it alive.
    """
    if Platform.windows:
        with suppress(Exception):
            import ctypes

            return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]
        return False
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _hold_open(title: str, message: str) -> None:
    """Block the main thread until the person dismisses the notice.

    The server runs in a daemon thread, so returning from here ends the process
    and takes the server with it. With no console there is nothing to wait on,
    hence a window: it both explains what happened and gives them a way to quit.
    """
    if Platform.windows:
        with suppress(Exception):
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)  # type: ignore[attr-defined]
            return
    Event().wait()


def _start_console_ui(host: str, port: int) -> None:
    url = _build_url(host, port)

    with suppress(Exception):
        import webbrowser

        webbrowser.open(url)

    if _has_console():
        print("Opening the following URL in your browser:\n")
        print(url, end="\n\n")
        with suppress(EOFError, KeyboardInterrupt):
            input("Press Enter to stop the server...")
        return

    _hold_open(
        "Lightnovel Crawler",
        f"Lightnovel Crawler is running in your browser.\n\n{url}\n\nClick OK to stop it.",
    )


def _report_failure(error: BaseException, log_path: Optional[Path]) -> None:
    where = f"\n\nThe log is at:\n{log_path}" if log_path else ""
    message = f"Lightnovel Crawler could not start.\n\n{error}{where}"
    if _has_console():
        print(message, file=sys.stderr)
        return
    with suppress(Exception):
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Lightnovel Crawler", 0x10)  # type: ignore[attr-defined]


def start() -> None:
    host = "localhost"
    # The server listens on SERVER_HOST, so the port has to be claimed there.
    # The browser's debugging port is loopback-only, so that one is probed as
    # loopback.
    port = free_port(SERVER_HOST, 31580)
    cdp_port = free_port(host, 31590)

    # Record to a file before anything else can fail. The desktop app has no
    # console and runs at log level 0, so without this a failed launch leaves
    # nothing behind at all — the window closes and there is no way to tell
    # what went wrong.
    ctx.config.load()
    log_path = log_to_file(ctx.config.app.app_dir / "logs" / "app.log")
    logger.info(f"Starting app: build={ctx.config.app.build} port={port} cdp={cdp_port}")

    # Bind on all interfaces so phones/tablets on the same Wi-Fi can open the
    # Reader (the app window itself still connects via localhost). The whole
    # API stays behind sign-in, so LAN exposure is auth-gated.
    Thread(
        daemon=True,
        name="server",
        target=_start_server,
        args=("0.0.0.0", port),
    ).start()

    try:
        storage_path = ctx.config.app.app_dir / "app-browser"
        _start_app_in_browser(host, port, cdp_port, storage_path)
    except FallbackException as e:
        # The app window is the nicety, not the app. Anything that only stops
        # us driving a browser window leaves the server perfectly able to serve
        # the same UI in whatever browser they already have.
        logger.warning(f"Falling back to the default browser: {e}")
        try:
            _start_console_ui(host, port)
        except BaseException as fallback_error:
            logger.exception("The fallback browser launch failed")
            _report_failure(fallback_error, log_path)
            raise
    except BaseException as e:
        logger.exception("The app failed to start")
        _report_failure(e, log_path)
        raise
