"""Start Dynasty automatically when you log in (macOS, Windows or Linux).

    python3 autostart.py install      set it up and start it now
    python3 autostart.py uninstall    stop it and remove the auto-start

Run it from the folder that contains server.py. If you move that folder later,
run "install" again. Add --dry-run to see what it would do without changing anything.
"""
import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
PY = sys.executable
HOME = os.path.expanduser("~")
LABEL = "com.dynasty.app"
PORT = 4737
PLIST = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")
UNIT = os.path.join(HOME, ".config", "systemd", "user", "dynasty.service")
VBS = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "Dynasty.vbs")
DRY = "--dry-run" in sys.argv
SYSTEM = sys.argv[sys.argv.index("--os") + 1] if "--os" in sys.argv else platform.system()


def run(cmd):
    print("  $", " ".join(cmd))
    if DRY:
        return 0
    try:
        return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print("   (skipped: %s)" % e)
        return 1


def write(path, text):
    print("  writing", path)
    if DRY:
        print(text)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def remove(path):
    if os.path.exists(path):
        print("  removing", path)
        if not DRY:
            os.remove(path)


def free_port():
    """Stop a copy of the server that is already running (for example one started by hand)."""
    if DRY:
        return
    try:
        if SYSTEM == "Windows":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
            pids = {l.split()[-1] for l in out.splitlines() if ":%d " % PORT in l and "LISTENING" in l}
            for pid in pids:
                subprocess.call(["taskkill", "/PID", pid, "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            out = subprocess.run(["lsof", "-ti", ":%d" % PORT], capture_output=True, text=True).stdout.split()
            for pid in out:
                os.kill(int(pid), 15)
    except (OSError, ValueError):
        pass


def plist():
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>%s</string>
<key>ProgramArguments</key><array><string>%s</string><string>%s</string><string>--no-browser</string></array>
<key>WorkingDirectory</key><string>%s</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>%s</string>
<key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (LABEL, PY, SERVER, HERE, os.path.join(HERE, "dynasty.log"), os.path.join(HERE, "dynasty.log"))


def mac(install):
    target = "gui/%d" % os.getuid()
    run(["launchctl", "bootout", target + "/" + LABEL])
    if not install:
        free_port()
        remove(PLIST)
        return
    for folder in ("Desktop", "Documents", "Downloads"):
        if HERE.startswith(os.path.join(HOME, folder)) and "--force" not in sys.argv:
            raise SystemExit(
                "macOS blocks background apps from reading your %s folder, so Dynasty would fail to start.\n"
                "Move the dynasty folder to somewhere like %s and run this again." % (folder, os.path.join(HOME, "Dynasty")))
    free_port()
    write(PLIST, plist())
    run(["launchctl", "bootstrap", target, PLIST])


def win(install):
    if not install:
        free_port()
        remove(VBS)
        return
    pyw = PY.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = PY
    free_port()
    write(VBS, 'CreateObject("Wscript.Shell").Run """%s"" ""%s"" --no-browser", 0, False\r\n' % (pyw, SERVER))
    run(["wscript", VBS])


def linux(install):
    if not install:
        run(["systemctl", "--user", "disable", "--now", "dynasty"])
        remove(UNIT)
        return
    free_port()
    write(UNIT, "[Unit]\nDescription=Dynasty\n\n[Service]\nExecStart=%s %s --no-browser\nWorkingDirectory=%s\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n" % (PY, SERVER, HERE))
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", "dynasty"])


if __name__ == "__main__":
    cmd = next((a for a in sys.argv[1:] if a in ("install", "uninstall")), None)
    if not cmd:
        raise SystemExit(__doc__)
    if not os.path.exists(SERVER):
        raise SystemExit("server.py not found next to this script.")
    install = cmd == "install"
    {"Darwin": mac, "Windows": win, "Linux": linux}.get(SYSTEM, linux)(install)
    if install:
        print("\nDone. Dynasty now starts by itself when you log in.")
        print("Open it any time at http://127.0.0.1:%d  (give it a few seconds the first time)" % PORT)
    else:
        print("\nDone. Dynasty will no longer start automatically.")
