"""Install TodoNess as a Windows startup application via Task Scheduler."""

import subprocess
import sys
import os
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAY_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "todoness_tray.pyw")
TASK_NAME = "TodoNess"

# Run from anywhere; the stale-PID guard lives under src/.
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def ensure_dependencies():
    """Install the web runtime and tray dependencies when missing."""
    missing = []
    packages = (
        ("tornado", "tornado"),
        ("jinja2", "jinja2"),
        ("pystray", "pystray"),
        ("PIL", "Pillow"),
        ("dateutil", "python-dateutil"),
    )
    for module_name, package_name in packages:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(package_name)

    if missing:
        print(f"Installing missing dependencies: {', '.join(missing)}")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + missing,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Failed to install dependencies:\n{result.stderr}")
            return False
        print("Dependencies installed successfully.")
    else:
        print("All dependencies already installed.")
    return True


def find_pythonw():
    """Find pythonw.exe in the same directory as the current Python interpreter."""
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    if not os.path.isfile(pythonw):
        print(f"ERROR: pythonw.exe not found at {pythonw}")
        return None
    return pythonw


def register_scheduled_task(pythonw):
    """Register TodoNess as a scheduled task that runs at logon."""
    ps_script = f'''
$action = New-ScheduledTaskAction -Execute '"{pythonw}"' -Argument '"{TRAY_SCRIPT}"' -WorkingDirectory '"{PROJECT_ROOT}"'
$trigger = New-ScheduledTaskTrigger -AtLogon -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "{TASK_NAME}" -Action $action -Trigger $trigger -Settings $settings -Force
'''
    print(f"Registering scheduled task '{TASK_NAME}'...")
    result = subprocess.run(
        ["powershell", "-Command", ps_script],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        print(f"ERROR: Failed to register scheduled task.\n{result.stderr}")
        return False

    print(f"Scheduled task '{TASK_NAME}' registered successfully.")
    print(f"  Python:    {pythonw}")
    print(f"  Script:    {TRAY_SCRIPT}")
    print(f"  WorkDir:   {PROJECT_ROOT}")
    print(f"  Trigger:   At logon for current user")
    return True


def start_tray_now(pythonw):
    """Start the tray application immediately, and verify it actually came up.

    Two things used to go wrong together. A deploy stops the old tray with a
    forced kill, so it never clears ``data/todoness.pid``; the new tray then
    sees that file, assumes another instance owns the port, and exits silently.
    This function meanwhile printed success unconditionally.

    The result was three consecutive deploys that reported "Done." while nothing
    was listening. Clear a stale lock first, then confirm the child survived
    rather than assuming it.
    """
    pidfile = os.path.join(PROJECT_ROOT, "data", "todoness.pid")
    try:
        from src.services.instance_guard import clear_stale_pidfile

        if clear_stale_pidfile(pidfile):
            print("  Cleared a stale PID file (the recorded process is gone).")
    except Exception as exc:  # noqa: BLE001
        print(f"  Note: could not check the PID file ({exc}).")

    print("Starting TodoNess tray app...")
    try:
        proc = subprocess.Popen(
            [pythonw, TRAY_SCRIPT],
            cwd=PROJECT_ROOT,
            creationflags=subprocess.DETACHED_PROCESS,
        )
    except Exception as e:
        print(f"ERROR: Failed to start tray app: {e}")
        return False

    # A silent exit on the single-instance guard is fast, so a short wait is
    # enough to tell "running" from "gave up immediately".
    for _ in range(20):
        time.sleep(0.25)
        if proc.poll() is not None:
            print(
                f"ERROR: The tray exited immediately (code {proc.returncode}).\n"
                f"       Another instance may hold port 8766, or "
                f"{pidfile} may name a live process.\n"
                f"       Check data/todoness.log for details."
            )
            return False

    print("TodoNess tray app started.")
    return True


def main():
    print("=" * 50)
    print("  TodoNess Startup Installer")
    print("=" * 50)
    print()

    # Step 1: Check/install dependencies
    if not ensure_dependencies():
        sys.exit(1)
    print()

    # Step 2: Find pythonw.exe
    pythonw = find_pythonw()
    if not pythonw:
        sys.exit(1)
    print()

    # Step 3: Register scheduled task
    if not register_scheduled_task(pythonw):
        sys.exit(1)
    print()

    # Step 4: Optionally start now
    answer = input("Start TodoNess tray app now? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        start_tray_now(pythonw)
    else:
        print("Skipped. The tray app will start at next logon.")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
