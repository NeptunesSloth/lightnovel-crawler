import os
import platform
import sys

from .common import static_cached_property


class Screen:
    width = 1920
    height = 1080
    view_width = 1920
    view_height = 1080


class Platform:
    @static_cached_property
    @staticmethod
    def name():
        return platform.platform()

    @static_cached_property
    @staticmethod
    def system():
        return platform.system()

    @static_cached_property
    @staticmethod
    def java():
        return Platform.system == "Java"

    @static_cached_property
    @staticmethod
    def mac():
        return Platform.system == "Darwin"

    @static_cached_property
    @staticmethod
    def linux():
        return Platform.system == "Linux"

    @static_cached_property
    @staticmethod
    def cygwin():
        return sys.platform.startswith("cygwin")

    @static_cached_property
    @staticmethod
    def posix():
        return sys.platform.startswith(("darwin", "cygwin", "linux"))

    @static_cached_property
    @staticmethod
    def windows():
        return Platform.system == "Windows"

    @static_cached_property
    @staticmethod
    def wsl():
        return "wsl" in platform.release().lower()

    @static_cached_property
    @staticmethod
    def ci():
        return "CI" in os.environ

    @static_cached_property
    @staticmethod
    def frozen():
        return bool(__package__ and getattr(sys, "frozen", False))

    @static_cached_property
    @staticmethod
    def docker():
        if os.path.isfile("/.dockerenv"):
            return True
        path = "/proc/self/cgroup"
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                if "docker" in f.read():
                    return True
        return False

    @static_cached_property
    @staticmethod
    def has_display():
        # Windows asks the OS directly. Measuring the screen through Tk meant the
        # frozen build had to carry the whole Tcl runtime for this one question,
        # and PyInstaller's tkinter hook installs a startup hook that aborts the
        # process when it cannot find the Tcl data directory it expects — so a
        # missing data folder killed `lncrawl.exe` before main() ran, on a code
        # path the CLI never uses.
        if Platform.windows:
            try:
                import ctypes

                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                # Report real pixels rather than the DPI-virtualised ones.
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass
                width = int(user32.GetSystemMetrics(0))  # SM_CXSCREEN
                height = int(user32.GetSystemMetrics(1))  # SM_CYSCREEN
                if width <= 0 or height <= 0:
                    return False
                Screen.width = width
                Screen.height = height
                # The area a maximised window gets, i.e. minus the taskbar. This
                # is what tk.maxsize() reported, and it is what the callers size
                # their browser windows against.
                view_width = int(user32.GetSystemMetrics(16))  # SM_CXFULLSCREEN
                view_height = int(user32.GetSystemMetrics(17))  # SM_CYFULLSCREEN
                Screen.view_width = view_width if view_width > 0 else width
                Screen.view_height = view_height if view_height > 0 else height
                return True
            except Exception:
                return False

        try:
            from tkinter import Tk

            tk = Tk()
            Screen.width = tk.winfo_screenwidth()
            Screen.height = tk.winfo_screenheight()
            Screen.view_width = tk.maxsize()[0]
            Screen.view_height = tk.maxsize()[1]
            tk.destroy()
            del tk

            return True
        except Exception:
            try:
                import matplotlib  # type: ignore

                backend = matplotlib.get_backend()
                return "Agg" not in backend
            except Exception:
                return False
