"""Persistent Codex app-server transport. Uses the existing CLI login, never copies tokens.

One owned process, multiplexed JSON-RPC, image inputs, streamed public answers,
bounded queues and real turn interruption. No per-question CLI startup.
"""
from __future__ import annotations

import itertools
from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass


class BrainError(RuntimeError):
    pass


class Cancelled(BrainError):
    pass


class RetryableError(BrainError):
    """A bounded retry may succeed; never used for auth/model/policy errors."""


class RpcTimeout(RetryableError):
    pass


def find_codex():
    """Prefer the native executable so Windows does not create a shell per server."""
    exe = shutil.which("codex.exe")
    if exe:
        return exe
    shim = shutil.which("codex.cmd") or shutil.which("codex")
    if shim:
        root = Path(shim).parent / "node_modules" / "@openai"
        for pattern in ("codex/node_modules/@openai/codex-win32-*/vendor/*/codex/codex.exe",
                        "codex/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
                        "codex/vendor/*/codex/codex.exe"):
            found = sorted(root.glob(pattern))
            if found:
                return str(found[0])
        return shim
    raise BrainError("Codex CLI is missing. Install it and run codex login.")


def isolated_config():
    config = {
        "model_reasoning_effort": "low", "model_verbosity": "low",
        "web_search": "disabled", "approval_policy": "never",
        "sandbox_mode": "read-only", "project_doc_max_bytes": 0,
        "features.code_mode": False, "features.shell_tool": False,
        "features.multi_agent": False, "features.apps": False,
        "features.memories": False, "features.personality": False,
        "features.fast_mode": True,
    }
    # Disable inherited connectors for THIS child only. Do not change the user's config.
    try:
        path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for name in data.get("mcp_servers", {}):
            if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                config[f"mcp_servers.{name}.enabled"] = False
    except (OSError, ValueError):
        pass
    return config


class CodexServer:
    def __init__(self, executable=None):
        self.executable = executable
        self.proc = None
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._lifecycle = threading.RLock()
        self._pending = {}
        self._streams = {}
        self._workspace = None
        self.stderr = deque(maxlen=6)

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    @contextmanager
    def _startup_lock(self, deadline, cancel):
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Question replaced.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpcTimeout("Codex timed out waiting for connection startup.")
            if self._lifecycle.acquire(timeout=min(.05, remaining)):
                break
        try:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Question replaced.")
            if time.monotonic() >= deadline:
                raise RpcTimeout("Codex timed out waiting for connection startup.")
            yield
        finally:
            self._lifecycle.release()

    def start_with_budget(self, *, deadline=None, cancel=None):
        self.start(deadline=deadline, cancel=cancel)

    def start(self, *, deadline=None, cancel=None):
        deadline = time.monotonic() + 25 if deadline is None else deadline
        with self._startup_lock(deadline, cancel):
            if self.alive:
                return
            if self.proc is not None:
                self.stop()
            self.stderr.clear()
            self._workspace = tempfile.TemporaryDirectory(prefix="halo_brain_")
            cmd = [self.executable or find_codex(), "app-server", "--listen", "stdio://"]
            for key, value in isolated_config().items():
                cmd += ["-c", key + "=" + json.dumps(value)]
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=self._workspace.name, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", bufsize=1,
                    creationflags=0x08000000 if os.name == "nt" else 0)
                threading.Thread(target=self._read, args=(self.proc,), daemon=True,
                                 name="halo-rpc").start()
                threading.Thread(target=self._read_errors, args=(self.proc,), daemon=True).start()
                self.rpc("initialize", {"clientInfo": {"name": "halo", "title": "HALO",
                                                       "version": "2.0.0"},
                                        "capabilities": {"experimentalApi": True}},
                         timeout=min(15, max(0, deadline - time.monotonic())), cancel=cancel)
                self.send({"method": "initialized", "params": {}}, deadline=deadline, cancel=cancel)
            except Exception as exc:
                self.stop()
                if isinstance(exc, (Cancelled, RetryableError)):
                    raise  # Preserve cancellation and retry eligibility through setup.
                raise BrainError(f"Could not start Codex: {exc} {' '.join(self.stderr)[-600:]}") from exc

    def _read_errors(self, proc):
        try:
            for line in proc.stderr:
                self.stderr.append(line.strip()[:300])
        except (OSError, ValueError):
            pass

    def send(self, message, *, deadline=None, cancel=None):
        deadline = time.monotonic() + 10 if deadline is None else deadline
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Question replaced.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpcTimeout("Codex timed out waiting for its connection writer.")
            if self._write_lock.acquire(timeout=min(.05, remaining)):
                break
        try:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Question replaced.")
            if time.monotonic() >= deadline:
                raise RpcTimeout("Codex timed out waiting for its connection writer.")
            if not self.alive:
                raise RetryableError("Codex stopped. Retry to reconnect.")
            proc = self.proc
            write_done = threading.Event()
            interrupted = []

            def guard_write():
                # A full OS pipe blocks inside write/flush, beyond the lock timer.
                # Killing this captured generation closes its read end and releases
                # the writer. Never call stop() here: it can wait on stream locks.
                while not write_done.is_set():
                    reason = None
                    if cancel is not None and cancel.is_set():
                        reason = Cancelled("Question replaced.")
                    elif time.monotonic() >= deadline:
                        reason = RpcTimeout("Codex timed out writing to its connection.")
                    if reason is not None:
                        interrupted.append(reason)
                        try:
                            if proc.poll() is None:
                                proc.kill()
                        except OSError:
                            pass
                        return
                    write_done.wait(min(.05, max(0, deadline - time.monotonic())))

            guard = threading.Thread(target=guard_write, daemon=True, name="halo-write-deadline")
            guard.start()
            try:
                proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                if interrupted:
                    raise interrupted[0] from exc
                raise RetryableError("Codex connection closed.") from exc
            finally:
                write_done.set()
                guard.join()
            if interrupted:
                raise interrupted[0]
        finally:
            self._write_lock.release()

    def rpc(self, method, params, timeout=10, cancel=None):
        deadline = time.monotonic() + timeout
        ident = next(self._ids)
        result = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[ident] = result
        try:
            self.send({"id": ident, "method": method, "params": params}, deadline=deadline, cancel=cancel)
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("Question replaced.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RpcTimeout(f"Codex timed out during {method}.")
                try:
                    message = result.get(timeout=min(0.05, remaining))
                    break
                except queue.Empty:
                    continue
            if "error" in message:
                error = message["error"]
                kind = RetryableError if error.get("retryable") else BrainError
                raise kind(str(error.get("message", "Codex request failed"))[:600])
            return message.get("result", {})
        finally:
            with self._lock:
                self._pending.pop(ident, None)

    def subscribe(self, thread_id):
        stream = queue.Queue(maxsize=2048)
        with self._lock:
            self._streams[thread_id] = stream
        return stream

    def unsubscribe(self, thread_id, *, deadline=None, cancel=None):
        with self._lock:
            self._streams.pop(thread_id, None)
        if self.alive:
            self.send({"id": next(self._ids), "method": "thread/unsubscribe",
                       "params": {"threadId": thread_id}}, deadline=deadline, cancel=cancel)

    def _dispatch(self, message):
        ident, method = message.get("id"), message.get("method")
        if method and ident is not None:
            # HALO only reads supplied text/images. Never approve tool execution or elicitation.
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self.send({"id": ident, "result": {"decision": "decline"}})
            else:
                self.send({"id": ident, "error": {"code": -32601,
                                                   "message": "HALO does not execute tools."}})
            return
        with self._lock:
            if ident in self._pending:
                target = self._pending[ident]
            else:
                params = message.get("params") or {}
                target = self._streams.get(params.get("threadId"))
            if target is not None:
                try:
                    target.put_nowait(message)
                except queue.Full:
                    # Overflow must fail visibly rather than lose completion and hang.
                    while not target.empty():
                        try:
                            target.get_nowait()
                        except queue.Empty:
                            break
                    target.put_nowait({"error": {"message": "Codex event queue overflow."}})

    def _read(self, proc):
        try:
            for line in proc.stdout:
                if self.proc is not proc:
                    return
                try:
                    self._dispatch(json.loads(line))
                except (ValueError, TypeError):
                    continue
        except (OSError, BrainError):
            pass
        finally:
            with self._lock:
                if self.proc is proc:
                    for target in list(self._pending.values()) + list(self._streams.values()):
                        try:
                            target.put_nowait({"error": {"message": "Codex connection closed.", "retryable": True}})
                        except queue.Full:
                            pass

    def recover(self, failed_proc, *, deadline=None, cancel=None):
        """Replace only the failed generation, once even if both lanes fail."""
        deadline = time.monotonic() + 25 if deadline is None else deadline
        with self._startup_lock(deadline, cancel):
            if self.proc is failed_proc:
                self.stop()
            self.start(deadline=deadline, cancel=cancel)

    def stop(self):
        with self._lifecycle:
            with self._lock:
                for target in list(self._pending.values()) + list(self._streams.values()):
                    try:
                        target.put_nowait({"error": {"message": "Codex connection reset.", "retryable": True}})
                    except queue.Full:
                        pass
            proc = self.proc
            if proc is not None:
                if proc.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                       capture_output=True, creationflags=0x08000000, timeout=8)
                    else:
                        proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        pipe.close()
                    except (OSError, AttributeError):
                        pass
            self.proc = None
            with self._lock:
                self._streams.clear()
            if self._workspace:
                self._workspace.cleanup()
                self._workspace = None


@dataclass
class Reply:
    text: str
    model: str
    effort: str
    service_tier: str
    first_token_s: float | None
    total_s: float


class BrainSession:
    """A serial lane with bounded conversation history, sharing a persistent server."""
    def __init__(self, server, model, instructions, *, effort="low", service_tier="priority",
                 max_turns=8):
        self.server, self.model, self.instructions = server, model, instructions
        self.effort, self.service_tier = effort, service_tier
        self.max_turns, self.turns = max_turns, 0
        self.thread_id = None
        self.events = None
        self._lock = threading.Lock()
        self.confirmed = {}
        self._proc = None

    def open(self, cancel=None, deadline=None):
        start_with_budget = getattr(self.server, "start_with_budget", None)
        if start_with_budget is not None:
            start_with_budget(deadline=deadline, cancel=cancel)
        else:
            self.server.start()  # Lightweight alternate transports retain their old interface.
        self._proc = getattr(self.server, "proc", None)
        remaining = 20 if deadline is None else min(20, deadline - time.monotonic())
        if cancel is not None and cancel.is_set():
            raise Cancelled("Question replaced.")
        if remaining <= 0:
            raise RetryableError(f"{self.model} exceeded its answer deadline during setup.")
        result = self.server.rpc("thread/start", {
            "model": self.model, "serviceTier": self.service_tier,
            "cwd": self.server._workspace.name, "ephemeral": True,
            "approvalPolicy": "never", "sandbox": "read-only",
            "baseInstructions": self.instructions, "developerInstructions": "",
            "personality": "none", "environments": [],
            "config": {"model_reasoning_effort": self.effort},
            "allowProviderModelFallback": False,
        }, timeout=remaining, cancel=cancel)
        self.confirmed = {k: result.get(k) for k in ("model", "reasoningEffort", "serviceTier")}
        self.thread_id = result["thread"]["id"]
        self.events = self.server.subscribe(self.thread_id)
        if result.get("model") != self.model or result.get("reasoningEffort") != self.effort:
            self.close()
            raise BrainError(f"Requested {self.model}/{self.effort}, got {self.confirmed}.")
        if self.service_tier in ("fast", "priority") and result.get("serviceTier") not in ("fast", "priority"):
            self.close()
            raise BrainError("Fast mode was not accepted by Codex.")
        self.turns = 0

    def close(self, *, deadline=None, cancel=None):
        tid, self.thread_id = self.thread_id, None
        self.events = None
        if tid and self._proc is getattr(self.server, "proc", None):
            try:
                if deadline is not None or cancel is not None:
                    self.server.unsubscribe(tid, deadline=deadline, cancel=cancel)
                else:
                    self.server.unsubscribe(tid)
            except BrainError:
                pass  # Cleanup must not mask the timeout/cancellation that caused it.

    def recover(self, error, *, deadline=None, cancel=None):
        if cancel is not None and cancel.is_set():
            raise Cancelled("Question replaced.")
        self.close(deadline=deadline, cancel=cancel)
        if isinstance(error, RpcTimeout):
            self.server.recover(self._proc, deadline=deadline, cancel=cancel)

    def ask(self, text, *, image=None, on_delta=None, cancel=None, timeout=30, reserve_turns=0,
            preceding_images=()):
        cancel = cancel or threading.Event()
        with self._lock:
            started = time.monotonic()
            if cancel.is_set():
                raise Cancelled("Question replaced.")
            if not self.server.alive or self._proc is not getattr(self.server, "proc", None):
                self.thread_id = None
            # Keep an optional image-detail follow-up in this same conversation.
            if not self.thread_id or self.turns + reserve_turns >= self.max_turns:
                self.close()
                self.open(cancel=cancel, deadline=started + timeout)
            if cancel.is_set():
                raise Cancelled("Question replaced.")
            while not self.events.empty():
                self.events.get_nowait()
            inputs = [{"type": "text", "text": text}]
            for label, previous_image in preceding_images:
                inputs.extend([{"type": "text", "text": label},
                               {"type": "image", "url": previous_image, "detail": "original"}])
            if image:
                if preceding_images:
                    inputs.append({"type": "text", "text": "LATEST captured frame (t = 0). Solve this state. Any ZOOM coordinates refer only to this image."})
                inputs.append({"type": "image", "url": image, "detail": "original"})
            turn_id = None
            parts = {}
            first = None
            try:
                result = self.server.rpc("turn/start", {"threadId": self.thread_id, "input": inputs,
                                         "model": self.model, "effort": self.effort,
                                         "serviceTier": self.service_tier, "summary": "none"},
                                         timeout=min(10, max(0, timeout - (time.monotonic() - started))),
                                         cancel=cancel)
                turn_id = result["turn"]["id"]
                accepted_at = time.monotonic()
                last_event_at, last_event = accepted_at, "turn/start acknowledgement"
                event_count = 0
                while True:
                    if cancel.is_set():
                        raise Cancelled("Question replaced.")
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        phase = ("waiting for answer completion" if any(part.strip() for part in parts.values())
                                 else "waiting for first answer text")
                        raise RetryableError(
                            f"{self.model} exceeded {timeout:g}s: {phase}; "
                            f"turn accepted {time.monotonic() - accepted_at:.1f}s ago; "
                            f"{event_count} events; last={last_event} "
                            f"{time.monotonic() - last_event_at:.1f}s ago.")
                    try:
                        event = self.events.get(timeout=min(0.05, remaining))
                    except queue.Empty:
                        continue
                    if "error" in event:
                        kind = RetryableError if event["error"].get("retryable") else BrainError
                        raise kind(event["error"]["message"])
                    method, params = event.get("method"), event.get("params") or {}
                    if params.get("turnId", turn_id) != turn_id:
                        continue
                    # Timing and event names only: never log question/answer contents.
                    last_event_at = time.monotonic()
                    last_event = str(method or "unknown")[:80]
                    event_count += 1
                    if method == "model/rerouted":
                        raise BrainError(f"Model changed to {params.get('toModel')}; answer withheld.")
                    if method == "item/agentMessage/delta":
                        ident = params["itemId"]
                        parts[ident] = parts.get(ident, "") + params.get("delta", "")
                        answer = "\n\n".join(parts.values())
                        if answer.strip() and first is None:
                            first = time.monotonic() - started
                        if on_delta and answer.strip():
                            on_delta(answer)
                    elif method == "item/completed":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage":
                            parts[item["id"]] = item.get("text", "")
                            answer = "\n\n".join(parts.values())
                            if answer.strip() and first is None:
                                first = time.monotonic() - started
                            if on_delta and answer.strip():
                                on_delta(answer)
                    elif method == "turn/completed":
                        turn = params["turn"]
                        if turn["id"] != turn_id:
                            continue
                        if turn["status"] != "completed":
                            raise BrainError((turn.get("error") or {}).get("message", "Answer interrupted."))
                        answer = "\n\n".join(parts.values()).strip()
                        if not answer:
                            raise RetryableError("The model returned no answer.")
                        if on_delta:
                            on_delta(answer)
                        self.turns += 1
                        return Reply(answer, self.model, self.effort, self.service_tier,
                                     first, time.monotonic() - started)
            except BaseException:
                try:
                    if turn_id is not None:
                        self.server.rpc("turn/interrupt", {"threadId": self.thread_id,
                                                           "turnId": turn_id}, timeout=2)
                except BrainError:
                    pass
                self.close()  # a fresh thread cannot receive late events from this turn
                raise
