"""Command line interface for esp32-serial-scope.

Reads a stream of ``key:value`` (or CSV) lines coming from an ESP32 over a
serial port -- or from the built-in ``--demo`` generator -- and draws them as
live line charts. A ``--print-only`` mode is included for headless machines
and for piping the values into other tools.

The reader runs on a background thread; the main thread only drains a queue
whenever matplotlib asks for a new frame, so a slow plot never blocks the
serial port and vice versa.
"""
from __future__ import annotations

import argparse
import math
import queue
import random
import re
import sys
import threading
import time
from collections import OrderedDict, deque
from typing import Dict, List, Optional

from . import __version__
from .parser import LineParser

__all__ = ["main", "build_arg_parser"]

DEFAULT_BAUD = 115200
DEFAULT_WINDOW = 300
DEFAULT_INTERVAL_MS = 100
STATIC_EXTENSIONS = (".png", ".jpg", ".jpeg", ".svg", ".pdf", ".webp")


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------
def build_arg_parser():
    """Build the argparse parser (also handy for tests and docs)."""
    parser = argparse.ArgumentParser(
        prog="esp32-serial-scope",
        description="Plot ESP32 serial key:value output as live charts.",
        epilog=(
            "examples:\n"
            "  esp32-serial-scope --demo\n"
            "  esp32-serial-scope -p COM3 -b 115200\n"
            "  esp32-serial-scope -p /dev/ttyUSB0 --csv-out log.csv --duration 30\n"
            "  esp32-serial-scope --demo --print-only --duration 5 --window 50\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-p", "--port", help="serial port, e.g. COM3 or /dev/ttyUSB0")
    parser.add_argument(
        "-b", "--baud", type=int, default=DEFAULT_BAUD,
        help="baud rate (default: %(default)s)",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="generate fake data instead of opening a serial port",
    )
    parser.add_argument(
        "--demo-signals", type=int, default=3,
        help="number of demo signals, 1-7 (default: %(default)s)",
    )
    parser.add_argument(
        "--rate", type=float, default=20.0,
        help="demo samples per second (default: %(default)s)",
    )
    parser.add_argument(
        "--labels",
        help="comma separated names for CSV input, e.g. temp,hum,vbus",
    )
    parser.add_argument(
        "--mode", choices=("auto", "kv", "csv"), default="auto",
        help="input format (default: %(default)s)",
    )
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW,
        help="points kept in the rolling window (default: %(default)s)",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_MS,
        help="redraw interval in milliseconds (default: %(default)s)",
    )
    parser.add_argument(
        "--duration", type=float, default=0.0,
        help="stop after N seconds, 0 runs until Ctrl+C (default: %(default)s)",
    )
    parser.add_argument(
        "--pattern",
        help="only parse lines matching this regular expression",
    )
    parser.add_argument(
        "--timeout", type=float, default=1.0,
        help="serial read timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--csv-out", metavar="FILE",
        help="also write parsed samples to a CSV file",
    )
    parser.add_argument(
        "--save", metavar="FILE",
        help="save a chart instead of showing it: .png/.svg static, .gif animated",
    )
    parser.add_argument(
        "--print-only", action="store_true",
        help="print parsed values to the terminal, do not open a window",
    )
    parser.add_argument(
        "--list-ports", action="store_true",
        help="list available serial ports and exit",
    )
    parser.add_argument(
        "--version", action="version",
        version="esp32-serial-scope %s" % __version__,
    )
    return parser


# --------------------------------------------------------------------------
# data sources
# --------------------------------------------------------------------------
class DemoSource:
    """Sine waves plus noise that look like real sensor output.

    Lets anyone verify the tool end to end without an ESP32 or a serial port.
    """

    _CATALOG = (
        ("temp", 26.0, 1.5, 20.0, 0.0),
        ("hum", 58.0, 6.0, 35.0, 1.0),
        ("vbus", 5.02, 0.06, 8.0, 2.0),
        ("adc", 2048.0, 250.0, 12.0, 0.5),
        ("ax", 0.0, 0.35, 5.0, 0.2),
        ("ay", 0.0, 0.35, 5.0, 2.1),
        ("az", 1.0, 0.15, 7.0, 3.3),
    )

    def __init__(self, signals=3, rate=20.0, noise=0.02):
        self.rate = max(float(rate), 0.1)
        self.noise = noise
        count = max(1, min(int(signals), len(self._CATALOG)))
        self.channels = list(self._CATALOG[:count])

    def lines(self, stop_event):
        """Yield one ``key:value,key:value`` line per sample."""
        period = 1.0 / self.rate
        start = time.time()
        while not stop_event.is_set():
            elapsed = time.time() - start
            parts = []
            for name, offset, amplitude, wave_period, phase in self.channels:
                value = offset + amplitude * math.sin(
                    2.0 * math.pi * elapsed / wave_period + phase
                )
                value += random.uniform(-self.noise, self.noise) * max(abs(amplitude), 1e-6)
                parts.append("%s:%.3f" % (name, value))
            yield ",".join(parts)
            time.sleep(period)


class SerialSource:
    """Read newline separated text from a real serial port."""

    def __init__(self, port, baud, timeout):
        self.port = port
        self.baud = baud
        self.timeout = timeout

    def lines(self, stop_event):
        try:
            import serial  # imported lazily so --demo needs no pyserial
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is required for real ports; install it with "
                "'pip install pyserial'"
            ) from exc

        try:
            connection = serial.Serial(self.port, self.baud, timeout=self.timeout)
        except Exception as exc:
            raise RuntimeError("cannot open %s: %s" % (self.port, exc)) from exc

        try:
            while not stop_event.is_set():
                try:
                    raw = connection.readline()
                except Exception as exc:
                    raise RuntimeError("serial read failed on %s: %s" % (self.port, exc)) from exc
                if not raw:
                    continue
                yield raw.decode("utf-8", errors="replace")
        finally:
            try:
                connection.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# stateful helpers
# --------------------------------------------------------------------------
class ScopeModel:
    """Rolling buffers, one deque per discovered signal."""

    def __init__(self, window=300):
        self.window = max(int(window), 2)
        self.names: List[str] = []
        self.series: "OrderedDict[str, deque]" = OrderedDict()
        self.x: deque = deque(maxlen=self.window)
        self.start: Optional[float] = None
        self.count = 0

    def push(self, record, timestamp):
        """Append one sample, keeping every series aligned with ``self.x``."""
        if not record:
            return
        if self.start is None:
            self.start = timestamp
        for name in record:
            if name not in self.series:
                buffer = deque(maxlen=self.window)
                buffer.extend([float("nan")] * len(self.x))
                self.series[name] = buffer
                self.names.append(name)
        self.x.append(timestamp - self.start)
        for name in self.names:
            value = record.get(name)
            self.series[name].append(float("nan") if value is None else float(value))
        self.count += 1

    def value_bounds(self):
        """Return ``(low, high)`` across all finite samples, or ``None``."""
        low = None
        high = None
        for buffer in self.series.values():
            for value in buffer:
                if math.isnan(value):
                    continue
                if low is None or value < low:
                    low = value
                if high is None or value > high:
                    high = value
        if low is None:
            return None
        return low, high


class CsvLogger:
    """Write parsed samples to CSV. The header is fixed by the first sample."""

    def __init__(self, path):
        self.path = path
        self._handle = None
        self._names: Optional[List[str]] = None
        self._start: Optional[float] = None
        self._warned = False

    def write(self, record, timestamp):
        if self._start is None:
            self._start = timestamp
        if self._handle is None:
            self._handle = open(self.path, "w", encoding="utf-8", newline="")
            self._names = list(record.keys())
            self._handle.write("t," + ",".join(self._names) + "\n")

        extra = [name for name in record if self._names and name not in self._names]
        if extra and not self._warned:
            print(
                "note: ignoring new column(s) %s in %s (header is fixed)"
                % (", ".join(extra), self.path),
                file=sys.stderr,
            )
            self._warned = True

        cells = ["%.4f" % (timestamp - self._start)]
        for name in self._names or []:
            value = record.get(name)
            cells.append("" if value is None else ("%g" % value))
        self._handle.write(",".join(cells) + "\n")
        self._handle.flush()

    def close(self):
        if self._handle is not None:
            self._handle.close()
            self._handle = None


# --------------------------------------------------------------------------
# reader thread
# --------------------------------------------------------------------------
def _pump(source, parser, pattern, sink, stop_event, errors):
    """Read lines on a background thread and hand parsed records to ``sink``."""
    try:
        for raw in source.lines(stop_event):
            if stop_event.is_set():
                break
            text = raw.strip()
            if not text:
                continue
            if pattern is not None and not pattern.search(text):
                continue
            record = parser.parse(text)
            if record:
                sink(record)
    except Exception as exc:  # surfaced on the main thread
        errors.append(exc)
    finally:
        stop_event.set()


# --------------------------------------------------------------------------
# output modes
# --------------------------------------------------------------------------
def _run_text(args, queue_, stop_event, errors, parser, logger):
    """Terminal-only mode: print each sample as a small aligned table."""
    start = time.time()
    names: List[str] = []
    header_needed = True
    count = 0

    while not stop_event.is_set():
        if errors:
            print("error: %s" % errors[0], file=sys.stderr)
            return 1
        if args.duration and time.time() - start >= args.duration:
            break
        try:
            record = queue_.get(timeout=0.2)
        except queue.Empty:
            continue

        count += 1
        now = time.time()
        if logger is not None:
            logger.write(record, now)

        new_names = [name for name in record if name not in names]
        if new_names:
            names.extend(new_names)
            header_needed = True
        if header_needed:
            print("  ".join(["%11s" % "time"] + ["%11s" % name for name in names]))
            header_needed = False

        cells = ["%10.2fs" % (now - start)]
        for name in names:
            value = record.get(name)
            cells.append("%11s" % ("-" if value is None else "%.3f" % value))
        print("  ".join(cells))

    print("parsed %d sample(s) from %d line(s)" % (count, parser.lines_seen))
    return 0


def _run_plot(args, queue_, stop_event, errors, logger):
    """Chart mode: live matplotlib window, an animated file, or a static file."""
    save_is_static = bool(args.save) and args.save.lower().endswith(STATIC_EXTENSIONS)
    if save_is_static:
        import matplotlib

        matplotlib.use("Agg", force=True)

    try:
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
    except ImportError as exc:  # pragma: no cover - environment dependent
        print(
            "matplotlib is required for charts (%s); install it with "
            "'pip install matplotlib' or use --print-only" % exc,
            file=sys.stderr,
        )
        return 1

    model = ScopeModel(args.window)
    fig, ax = plt.subplots(figsize=(10.0, 5.5))
    label = "demo" if args.demo else str(args.port)
    ax.set_title("esp32-serial-scope  [%s]" % label)
    ax.set_xlabel("seconds")
    ax.set_ylabel("value")
    ax.grid(True, alpha=0.3)
    try:
        fig.canvas.manager.set_window_title("esp32-serial-scope")
    except Exception:
        pass

    lines: Dict[str, object] = {}
    holder = {"animation": None}
    started_at = time.time()

    def drain(limit=2000):
        drained = 0
        while drained < limit:
            try:
                record = queue_.get_nowait()
            except queue.Empty:
                break
            now = time.time()
            if logger is not None:
                logger.write(record, now)
            model.push(record, now)
            drained += 1
        return drained

    def redraw():
        for name in model.names:
            if name not in lines:
                (line,) = ax.plot([], [], label=name, linewidth=1.4)
                lines[name] = line
        xs = list(model.x)
        for name, line in lines.items():
            buffer = model.series.get(name)
            line.set_data(xs, list(buffer) if buffer is not None else [])
        if xs:
            if xs[-1] > xs[0]:
                ax.set_xlim(xs[0], xs[-1])
            else:
                ax.set_xlim(xs[0], xs[0] + 1.0)
            bounds = model.value_bounds()
            if bounds is not None:
                low, high = bounds
                if low == high:
                    low, high = low - 1.0, high + 1.0
                pad = (high - low) * 0.08
                ax.set_ylim(low - pad, high + pad)
        if model.series:
            ax.legend(
                loc="upper left", fontsize=8,
                ncols=min(4, max(1, len(model.series))),
            )

    def stop_now():
        stop_event.set()
        animation = holder["animation"]
        if animation is not None:
            try:
                animation.event_source.stop()
            except Exception:
                pass

    # --- static snapshot -------------------------------------------------
    if save_is_static:
        run_for = args.duration if args.duration > 0 else 5.0
        deadline = time.time() + run_for
        while not stop_event.is_set() and time.time() < deadline:
            drain()
            redraw()
            time.sleep(max(args.interval, 1) / 1000.0)
        if errors:
            print("error: %s" % errors[0], file=sys.stderr)
            return 1
        redraw()
        try:
            fig.savefig(args.save, dpi=140, bbox_inches="tight")
        except Exception as exc:
            print("could not save %s: %s" % (args.save, exc), file=sys.stderr)
            return 1
        print("saved chart to %s (%d sample(s))" % (args.save, model.count))
        plt.close(fig)
        return 0

    # --- animation / live window ----------------------------------------
    def update(_frame):
        drain()
        redraw()
        if errors:
            stop_now()
        if args.duration and time.time() - started_at >= args.duration:
            stop_now()
        return list(lines.values())

    if args.save:
        run_for = args.duration if args.duration > 0 else 5.0
        deadline = started_at + run_for

        def frame_source():
            while not stop_event.is_set() and time.time() < deadline:
                yield None
                time.sleep(max(args.interval, 1) / 1000.0)

        animation = FuncAnimation(
            fig, update, frames=frame_source, interval=args.interval,
            blit=False, cache_frame_data=False,
        )
        holder["animation"] = animation
        try:
            animation.save(args.save, fps=max(1.0, 1000.0 / max(args.interval, 1)))
        except Exception as exc:
            print(
                "could not save animation (%s); try a .gif extension" % exc,
                file=sys.stderr,
            )
            return 1
        print("saved animation to %s (%d sample(s))" % (args.save, model.count))
        plt.close(fig)
        return 0

    animation = FuncAnimation(
        fig, update, interval=args.interval, blit=False, cache_frame_data=False,
    )
    holder["animation"] = animation
    try:
        plt.show()
    except Exception as exc:
        print(
            "could not open a window (%s); rerun with --print-only or "
            "--save chart.png" % exc,
            file=sys.stderr,
        )
        return 1

    if errors:
        print("error: %s" % errors[0], file=sys.stderr)
        return 1
    return 0


def _list_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial is not installed; run 'pip install pyserial'", file=sys.stderr)
        return 1
    ports = list(list_ports.comports())
    if not ports:
        print("no serial ports found")
        return 0
    for port in ports:
        print("%-24s %s" % (port.device, port.description))
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def main(argv=None):
    """Run the tool. Returns a process exit code."""
    args = build_arg_parser().parse_args(argv)

    if args.list_ports:
        return _list_ports()

    if not args.demo and not args.port:
        print(
            "error: provide --port COM3, or use --demo for a hardware-free demo",
            file=sys.stderr,
        )
        return 2

    labels = None
    if args.labels:
        labels = [item.strip() for item in args.labels.split(",") if item.strip()]
    parser = LineParser(labels=labels, mode=args.mode)

    pattern = None
    if args.pattern:
        try:
            pattern = re.compile(args.pattern)
        except re.error as exc:
            print("error: bad --pattern: %s" % exc, file=sys.stderr)
            return 2

    if args.demo:
        source = DemoSource(args.demo_signals, args.rate)
    else:
        source = SerialSource(args.port, args.baud, args.timeout)

    work_queue: "queue.Queue" = queue.Queue(maxsize=20000)
    stop_event = threading.Event()
    errors: List[BaseException] = []
    logger = CsvLogger(args.csv_out) if args.csv_out else None

    def sink(record):
        try:
            work_queue.put(record, timeout=0.5)
        except queue.Full:
            pass  # drop a sample rather than block the reader

    worker = threading.Thread(
        target=_pump,
        args=(source, parser, pattern, sink, stop_event, errors),
        daemon=True,
    )
    worker.start()

    try:
        if args.print_only:
            return _run_text(args, work_queue, stop_event, errors, parser, logger)
        return _run_plot(args, work_queue, stop_event, errors, logger)
    except KeyboardInterrupt:
        print("stopped")
        return 0
    finally:
        stop_event.set()
        worker.join(timeout=2.0)
        if logger is not None:
            logger.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
