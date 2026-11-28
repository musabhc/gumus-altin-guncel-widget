"""Windows startup registration and application updates."""

import os
import sys
import tempfile
import winreg
from tkinter import messagebox

import requests

from piyasa.paths import app_path


GITHUB_REPO = "musabhc/gumus-altin-guncel-widget"

try:
    import _version
    VERSION = _version.__version__
except ImportError:
    VERSION = "0.0.0-dev"


class AutoStartManager:
    def __init__(self, app_name="PiyasaWidget"):
        self.app_name = app_name
        self.key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def is_enabled(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, self.app_name)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False

    def set_autostart(self, enable=True):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_ALL_ACCESS)
            if enable:
                # Script path logic
                exe_path = sys.executable
                script_path = app_path("main.py")
                # If running as .py, use pythonw.exe to run it without console
                if exe_path.endswith("python.exe") or exe_path.endswith("pythonw.exe"):
                     cmd = f'"{exe_path.replace("python.exe", "pythonw.exe")}" "{script_path}"'
                else:
                     # Frozen exe (pyinstaller)
                     cmd = f'"{sys.executable}"'

                winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, self.app_name)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            messagebox.showerror("Hata", f"Kayıt defteri hatası: {e}")

class UpdateManager:
    def __init__(self, current_version, repo_name):
        self.current_version = current_version
        self.repo_name = repo_name
        self.api_url = f"https://api.github.com/repos/{repo_name}/releases/latest"

    def check_for_updates(self):
        try:
            response = requests.get(self.api_url, timeout=(5, 15))
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                latest_tag = data.get("tag_name", "").replace("v", "")
                download_url = ""

                # Varlıklar içinde .exe ara (Setup öncelikli)
                for asset in data.get("assets", []):
                    if asset["name"].endswith("Setup.exe"):
                        download_url = asset["browser_download_url"]
                        break
                    elif asset["name"].endswith(".exe"):
                        download_url = asset["browser_download_url"]

                if self._version_parts(latest_tag) > self._version_parts(self.current_version) and download_url:
                    return True, latest_tag, download_url
            return False, None, None
        except (requests.RequestException, ValueError, KeyError) as error:
            raise RuntimeError("Güncelleme bilgisi alınamadı.") from error

    @staticmethod
    def _version_parts(version):
        return tuple(map(int, version.lstrip("v").split("-", 1)[0].split(".")))

    def update_application(self, download_url):
        prefix = f"https://github.com/{self.repo_name}/releases/download/"
        if not download_url.startswith(prefix) or not download_url.endswith("/PiyasaWidget_Setup.exe"):
            raise ValueError("Kurulum paketi beklenen GitHub sürümünde bulunamadı.")
        directory = tempfile.mkdtemp(prefix="piyasa-update-")
        target = os.path.join(directory, "PiyasaWidget_Setup.exe")
        try:
            with requests.get(download_url, stream=True, timeout=(5, 30)) as response:
                response.raise_for_status()
                with open(target, "wb") as file:
                    for chunk in response.iter_content(chunk_size=65536):
                        file.write(chunk)
            with open(target, "rb") as file:
                if file.read(2) != b"MZ":
                    raise ValueError("İndirilen dosya geçerli bir Windows kurulum paketi değil.")
        except (OSError, ValueError):
            if os.path.exists(target):
                os.remove(target)
            os.rmdir(directory)
            raise
        return target
