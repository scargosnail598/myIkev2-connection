"""Continuously sampled host metrics with bounded in-memory history."""

from collections import deque
from pathlib import Path
import re
import shlex
import threading
import time


INTERVAL = 5
WINDOW = 300
MAX_READ = 1024 * 1024
INTERFACE = re.compile(r"[A-Za-z0-9_.:-]{1,15}")


def read_text(path, encoding="ascii"):
    with path.open(encoding=encoding) as source:
        content = source.read(MAX_READ + 1)
    if len(content) > MAX_READ:
        raise ValueError("Oversized metrics source")
    return content


class MetricsReader:
    def __init__(self, proc=Path("/proc"), state=Path("/var/lib/ikev2-easy-installer/state.env"),
                 monotonic=time.monotonic, clock=time.time):
        self.proc, self.state = proc, state
        self.monotonic, self.clock = monotonic, clock
        self.lock = threading.Lock()
        self.previous = None
        self.latest = None
        self.history = deque(maxlen=WINDOW // INTERVAL + 1)
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _cpu(self):
        try:
            lines = read_text(self.proc / "stat").splitlines()
            fields = lines[0].split()
            if fields[0] != "cpu" or len(fields) < 9:
                raise ValueError("Missing CPU counters")
            # Guest counters are already included in user/nice; use the first eight.
            values = tuple(int(value) for value in fields[1:9])
            cores = sum(re.match(r"cpu[0-9]+\s", line) is not None for line in lines)
            if any(value < 0 for value in values) or cores < 1:
                raise ValueError("Invalid CPU counters")
            return values, cores
        except (OSError, UnicodeError, ValueError, IndexError):
            return None, None

    def _memory(self):
        empty = {"total_bytes": None, "used_bytes": None, "percent": None}
        try:
            fields = {}
            for line in read_text(self.proc / "meminfo").splitlines():
                match = re.fullmatch(r"(MemTotal|MemAvailable):\s+(\d+) kB", line.strip())
                if match:
                    fields[match[1]] = int(match[2]) * 1024
            total, available = fields["MemTotal"], fields["MemAvailable"]
            if total <= 0 or not 0 <= available <= total:
                raise ValueError("Invalid memory counters")
            used = total - available
            return {"total_bytes": total, "used_bytes": used, "percent": round(used / total * 100, 2)}
        except (OSError, UnicodeError, ValueError, KeyError):
            return empty

    def _interface(self):
        try:
            entries = [line[7:] for line in read_text(self.state, encoding="utf-8").splitlines() if line.startswith("OUT_IF=")]
            if entries:
                if len(entries) != 1:
                    return None, "unavailable"
                values = shlex.split(entries[0])
                if len(values) != 1 or not INTERFACE.fullmatch(values[0]) or values[0] == "lo":
                    return None, "unavailable"
                return values[0], "configured"
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, ValueError):
            return None, "unavailable"
        try:
            routes = []
            for line in read_text(self.proc / "net/route").splitlines()[1:]:
                fields = line.split()
                if (len(fields) >= 8 and fields[1] == "00000000" and fields[7] == "00000000"
                        and int(fields[3], 16) & 1 and INTERFACE.fullmatch(fields[0]) and fields[0] != "lo"):
                    routes.append((int(fields[6]), fields[0]))
            return (min(routes)[1], "default_route") if routes else (None, "unavailable")
        except (OSError, UnicodeError, ValueError):
            return None, "unavailable"

    def _network(self):
        interface, source = self._interface()
        network = {"interface": interface, "source": source, "rx_bytes": None, "tx_bytes": None,
                   "rx_bytes_per_second": None, "tx_bytes_per_second": None}
        if interface is None:
            return network
        try:
            for line in read_text(self.proc / "net/dev").splitlines():
                if ":" not in line:
                    continue
                name, counters = line.rsplit(":", 1)
                if name.strip() != interface:
                    continue
                values = [int(value) for value in counters.split()]
                if len(values) != 16 or any(value < 0 for value in values):
                    raise ValueError("Invalid interface counters")
                network.update(rx_bytes=values[0], tx_bytes=values[8])
                break
        except (OSError, UnicodeError, ValueError):
            pass
        return network

    def start(self):
        """Start one collector for this broker, independent of HTTP requests."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.collect()
            self._thread = threading.Thread(target=self._collect_loop, name="ikev2-metrics", daemon=True)
            self._thread.start()

    def stop(self):
        with self._lifecycle_lock:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=2)

    def _collect_loop(self):
        deadline = self.monotonic() + INTERVAL
        while not self._stop.wait(max(0, deadline - self.monotonic())):
            self.collect()
            # Stay on the schedule despite small timing differences between wakes.
            # A delayed wake records a real gap instead of backfilling invented data.
            deadline += INTERVAL
            now = self.monotonic()
            if deadline <= now:
                deadline = now + INTERVAL

    def collect(self):
        """Record one actual sample; called by the server-side collector only."""
        with self.lock:
            self._collect_locked(self.monotonic())

    def _collect_locked(self, now):
        counters, cores = self._cpu()
        memory, network = self._memory(), self._network()
        percent = None
        previous = self.previous
        elapsed = now - previous["time"] if previous else 0
        if previous and 0 < elapsed <= INTERVAL * 3:
            before = previous["cpu"]
            if counters is not None and before is not None and cores == previous["cores"]:
                deltas = [current - old for current, old in zip(counters, before)]
                total = sum(deltas)
                if total > 0 and all(value >= 0 for value in deltas):
                    percent = round((total - deltas[3] - deltas[4]) / total * 100, 2)
            old_network = previous["network"]
            if network["interface"] == old_network["interface"]:
                for key in ("rx", "tx"):
                    current, old = network[key + "_bytes"], old_network[key + "_bytes"]
                    if current is not None and old is not None and current >= old:
                        network[key + "_bytes_per_second"] = round((current - old) / elapsed, 2)
        self.latest = {"sampled_at": self.clock(), "cpu": {"percent": percent, "cores": cores},
                       "memory": memory, "network": network}
        point = {"cpu_percent": percent, "memory_percent": memory["percent"],
                 "interface": network["interface"], "rx_bytes_per_second": network["rx_bytes_per_second"],
                 "tx_bytes_per_second": network["tx_bytes_per_second"]}
        self.history.append((now, point))
        self.previous = {"time": now, "cpu": counters, "cores": cores, "network": network.copy()}

    def snapshot(self):
        """Read the buffer without moving the collector's sampling schedule."""
        with self.lock:
            now = self.monotonic()
            if self.latest is None:
                self._collect_locked(now)
            while self.history and now - self.history[0][0] > WINDOW:
                self.history.popleft()
            age = max(0, now - self.previous["time"])
            return {"ok": age <= INTERVAL * 3, **self.latest, "sample_age_seconds": round(age, 3),
                    "interval_seconds": INTERVAL, "window_seconds": WINDOW,
                    "history": [{"age_seconds": round(now - timestamp, 3), **point} for timestamp, point in self.history]}
