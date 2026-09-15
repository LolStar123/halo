"""Local, same-user HALO status/quit control. No keyboard or mouse injection."""
import argparse
import json
import os
from pathlib import Path
import time

from PySide6.QtCore import QCoreApplication, QTimer, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def server_name(pid):
    return f"halo-control-{int(pid)}"


class ControlServer(QLocalServer):
    def __init__(self, status, quit_callback, parent=None):
        super().__init__(parent)
        self.status = status
        self.clients = set()
        self.quit_callback = quit_callback
        self.setSocketOptions(QLocalServer.UserAccessOption)
        self.newConnection.connect(self._accept)
        if not self.listen(server_name(os.getpid())):
            raise RuntimeError(self.errorString())

    def _accept(self):
        while self.hasPendingConnections():
            socket = self.nextPendingConnection()
            self.clients.add(socket)
            socket.setReadBufferSize(1024)
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(self._expire)
            timer.start(3000)
            socket.disconnected.connect(self._disconnected)
            socket.readyRead.connect(self._ready)
            if socket.bytesAvailable():
                self._read(socket)

    @Slot()
    def _expire(self):
        self.sender().parent().abort()

    @Slot()
    def _ready(self):
        self._read(self.sender())

    @Slot()
    def _disconnected(self):
        self._forget(self.sender())

    def _forget(self, socket):
        for timer in socket.findChildren(QTimer):
            timer.stop()
        self.clients.discard(socket)
        socket.deleteLater()

    def _read(self, socket):
        if socket.property("handled"):
            return
        if not socket.canReadLine():
            if socket.bytesAvailable() >= 1024:
                socket.abort()
            return
        socket.setProperty("handled", True)
        action = bytes(socket.readLine()).decode("utf-8", errors="replace").strip()
        reply = {"pid": os.getpid(), "accepted": action in ("status", "quit"), "action": action}
        if action == "status":
            reply["state"] = self.status()
        elif action != "quit":
            reply["error"] = "Supported commands: status, quit"
        socket.write(json.dumps(reply).encode("utf-8") + b"\n")
        socket.flush()
        socket.disconnectFromServer()
        if action == "quit":
            QTimer.singleShot(50, self.quit_callback)


def request(pid, action, timeout=3):
    socket = QLocalSocket()
    socket.connectToServer(server_name(pid))
    if not socket.waitForConnected(int(timeout * 1000)):
        raise RuntimeError("HALO control unavailable: " + socket.errorString())
    try:
        socket.write(action.encode("utf-8") + b"\n")
        socket.flush()
        deadline = time.monotonic() + timeout
        while not socket.canReadLine():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not socket.waitForReadyRead(max(1, int(remaining * 1000))):
                if not socket.canReadLine():
                    raise RuntimeError("HALO did not acknowledge the command")
        reply = json.loads(bytes(socket.readLine()))
        if reply.get("pid") != int(pid):
            raise RuntimeError("Unexpected HALO process identity")
        return reply
    finally:
        socket.abort()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "quit"))
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    app = QCoreApplication.instance() or QCoreApplication([])
    try:
        pid = args.pid
        if pid is None:
            health = Path(__file__).parent / ".runtime" / "health.json"
            pid = json.loads(health.read_text(encoding="utf-8"))["pid"]
        reply = request(pid, args.action)
        print(json.dumps(reply, indent=2))
        return 0 if reply["accepted"] else 1
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"accepted": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
