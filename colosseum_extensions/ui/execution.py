"""Asynchronous CLI process ownership; Qt dependencies are never modified."""
import codecs
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

_STOP_TIMEOUT_MS = 5000
_INTERRUPTED_RESULT = "result: INCOMPLETE (exit 1)"


@dataclass(frozen=True)
class RunRequest:
    kind: str
    path: str
    config: str = ""
    parameters: str = ""
    metadata: str = ""
    debug: bool = False
    no_sleep: bool = False
    dry_run: bool = False

    def arguments(self):
        args = ["-u", "-m", "colosseum.runner.cli", self.kind, self.path]
        for flag, value in (("config", self.config), ("parameters", self.parameters), ("metadata", self.metadata)):
            if value.strip():
                args.extend(["--" + flag, value.strip()])
        if self.debug:
            args.append("--debug")
        if self.no_sleep:
            args.append("--no-sleep")
        if self.dry_run:
            args.append("--dry-run")
        return args


class Execution(QObject):
    output = Signal(str)
    state_changed = Signal(str)
    completed = Signal(int)

    def __init__(self, cwd, parent=None):
        super().__init__(parent)
        self.state = "Ready"
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(cwd))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        self.environment = environment
        self.process.readyReadStandardOutput.connect(self.read)
        self.process.started.connect(self.started)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.error)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self.force_stop)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.stop_directory = None

    @property
    def active(self):
        return self.state in {"Starting", "Running", "Stopping"}

    def change_state(self, state):
        self.state = state
        self.state_changed.emit(state)

    def start(self, request):
        if self.active:
            return False
        self.decoder.reset()
        self.forced = False
        self.interrupted = False
        self.output_tail = ""
        self.stop_directory = TemporaryDirectory(prefix="colosseum-run-")
        self.stop_marker = Path(self.stop_directory.name) / "stop"
        self.environment.insert("COLOSSEUM_GUI_STOP_FILE", str(self.stop_marker))
        self.process.setProcessEnvironment(self.environment)
        self.change_state("Starting")
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        args = request.arguments()
        if getattr(sys, "frozen", False):
            args = args[3:]
        self.process.start(str(executable), args)
        return True

    def started(self):
        if self.state == "Starting":
            self.change_state("Running")

    def read(self):
        text = self.decoder.decode(bytes(self.process.readAllStandardOutput()))
        if text:
            combined = self.output_tail + text
            self.interrupted |= _INTERRUPTED_RESULT in combined
            self.output_tail = combined[-len(_INTERRUPTED_RESULT):]
            self.output.emit(text)

    def send_input(self, value):
        if self.state == "Running":
            self.process.write((value + "\n").encode("utf-8"))

    def stop(self):
        if self.state not in {"Starting", "Running"}:
            return
        self.change_state("Stopping")
        try:
            self.stop_marker.touch()
        except OSError as exc:
            self.output.emit(f"Could not request a graceful stop: {exc}\n")
        self.deadline.start(_STOP_TIMEOUT_MS)

    def force_stop(self):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.forced = True
            self.process.kill()

    def error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.output.emit(self.process.errorString() + "\n")
            self.finished(-1)

    def finished(self, code, status=None):
        self.deadline.stop()
        self.read()
        tail = self.decoder.decode(b"", final=True)
        if tail:
            self.output.emit(tail)
        if self.stop_directory is not None:
            self.stop_directory.cleanup()
            self.stop_directory = None
        if self.forced:
            self.change_state("Force stopped")
        elif code == 1 and self.interrupted:
            self.change_state("Stopped")
        else:
            self.change_state(f"Finished (exit {code})")
        self.completed.emit(code)
