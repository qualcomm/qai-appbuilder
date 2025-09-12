from pathlib import Path
import subprocess
import psutil
import time
import os

FLAG_DETACHED_PROCESS = 0x00000008
FLAG_CREATE_NO_WINDOW = 0x08000000
# Start Genie Service


class GenieServiceLauncher:
    def __init__(self, model_name: str = "Qwen2.0-7B-SSD", debug_mode: bool = False):
        self.model_name = model_name
        self.service_process: subprocess.Popen | None = None
        # Assume current working directory is 'samples/'
        self.cwd = Path.cwd()
        self.debug_mode = debug_mode
        # Build paths to executable and config file
        self.exe_path = os.path.join(self.cwd, "GenieAPIService", "GenieAPIService.exe")
        self.config_path = os.path.join("genie", "python", "models", model_name, "config.json")

        # Preemptively kill any running GenieAPIService instances
        self._terminate_existing_processes()

    def _terminate_existing_processes(self):
        if self.debug_mode:
            print("[Init] Checking for running GenieAPIService.exe processes...")
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                if proc.info["name"] and "GenieAPIService.exe" in proc.info["name"]:
                    if self.debug_mode:
                        print(f"[Kill] Terminating existed GenieAPIService.exe PID={proc.pid}")
                    proc.kill()
                    time.sleep(1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def launch(self, extra_args: str = "-l") -> bool:
        command_str = f'cmd /c "cd /d {self.cwd} && {self.exe_path} -c {self.config_path} {extra_args}"'
        if self.debug_mode:
            print(command_str)
        print(f"[Launch] Opening GenieAPIService in new window with model '{self.model_name}'...")

        try:
            if self.debug_mode:
                FLAGS = FLAG_DETACHED_PROCESS
            else:
                FLAGS = FLAG_CREATE_NO_WINDOW
            self.service_process = subprocess.Popen(
                command_str,
                shell=True,
                creationflags=FLAGS
            )
            time.sleep(10)
            return True
        except Exception as e:
            print(f"[Error] Failed to launch GenieAPIService: {e}")
            return False

    def terminate(self):
        try:
            parent = psutil.Process(self.service_process.pid)
            for child in parent.children(recursive=True):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass

            try:
                parent.terminate()
            except psutil.NoSuchProcess:
                pass

        except psutil.NoSuchProcess:
            pass
        time.sleep(1)

    def restart(self, extra_args: str = "-l") -> bool:
        if self.debug_mode:
            print("[Restart] Restarting GenieAPIService...")
        self.terminate()
        return self.launch(extra_args)

"""
# for script test
if __name__ == "__main__":
    launcher = GenieServiceLauncher("Qwen2.0-7B-SSD", True)
    launcher.launch()
    input("input any key to stop")
    launcher.terminate()
"""