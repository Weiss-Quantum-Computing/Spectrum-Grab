#!/usr/bin/env python3
"""
Spectrum Grab - one-click capture from an SRS SR760 FFT spectrum analyzer.

Click a button, get a CSV of the trace, a PNG of the plot and a metadata text
file in your chosen folder. This is the read_sr760fft_data*.py bench scripts
rolled into one program: single grabs, span sweeps, stitched start-frequency
sweeps, the fast SPEB? binary transfer and the autorange-then-freeze routine.

Requires: NI-488.2 (or any VISA with GPIB support) + `pip install pyvisa numpy
          matplotlib pillow`
          (matplotlib draws the plots, pillow only sharpens the preview - the
          CSV and the metadata file are written without either)
Run with:  pythonw spectrum_grab.py      (pythonw = no console window)
"""

import datetime
import json
import os
import queue
import threading
import time
import tkinter as tk
from collections import namedtuple
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pyvisa

try:
    from PIL import Image, ImageTk        # smooth (Lanczos) preview rescale
except ImportError:                       # without pillow: Tk's integer subsample
    Image = ImageTk = None

try:
    import matplotlib
    matplotlib.use("Agg")                  # plots are files, not pop-up windows, so
    matplotlib.rcParams["font.size"] = 14  # they can be drawn off the main thread
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
except ImportError:                       # everything but the plots still works
    Figure = None

# Remembered between sessions: folder, title, sweep lists, acquisition options.
# Kept out of the program folder so a git pull cannot clobber it.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "SpectrumGrab", "config.json")

DEFAULT_ADDRESS = "GPIB0::10::INSTR"
TRACE = 0                     # 0 = the active trace
N_BINS = 400                  # the SR760's x-axis resolution, fixed for FFT displays
CONNECT_TIMEOUT_MS = 5000
OP_TIMEOUT_MS = 20000
SETTINGS_TIMEOUT_MS = 2000    # a query the analyzer dislikes costs a whole timeout
PLOT_DPI = 300
PREVIEW_W, PREVIEW_H = 440, 330

# Plot window used unless the trace falls outside it, and only for dB traces -
# a linear trace is autoscaled instead.
DEFAULT_YMIN, DEFAULT_YMAX = -160.0, -20.0

# Widgets that already use Space themselves. Space is only the GRAB shortcut
# when the focus is not sitting on one of these - otherwise typing a space in
# the title box (or toggling a focused checkbox) would fire an acquisition.
SPACE_OWNERS = {
    "Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox",
    "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton",
    "Button", "TButton",
}

BAD_NAME_CHARS = r'<>:"/\|?*'

# Span is set by code, not by frequency, so this table is the only way back to
# Hz - which the file names, the stitch helper and the metadata all need.
SPANS = [
    ("191 mHz", 0.191), ("382 mHz", 0.382), ("763 mHz", 0.763), ("1.5 Hz", 1.5),
    ("3.1 Hz", 3.1), ("6.1 Hz", 6.1), ("12.2 Hz", 12.2), ("24.4 Hz", 24.4),
    ("48.75 Hz", 48.75), ("97.5 Hz", 97.5), ("195 Hz", 195.0), ("390 Hz", 390.0),
    ("780 Hz", 780.0), ("1.56 kHz", 1560.0), ("3.125 kHz", 3125.0),
    ("6.25 kHz", 6250.0), ("12.5 kHz", 12500.0), ("25 kHz", 25000.0),
    ("50 kHz", 50000.0), ("100 kHz", 100000.0),
]
SPAN_CHOICES = tuple(f"{i} - {label}" for i, (label, _) in enumerate(SPANS))

# Settings the panel shows and can push back. `key` doubles as the dict key
# everywhere.
#   num  - free-form number
#   enum - the analyzer answers with an integer; `choices` maps code -> label
#
# An SR760 command either takes the graph number as its first parameter or it
# does not, and that one fact decides how it is both queried and written. Which
# way round a command goes cannot be guessed reliably - WNDO is written
# "WNDO 0,2" but the manual's own summary reads "WNDO i", while FMTS and MRLK go
# the other way - and getting it wrong is silent: the analyzer accepts
# "WNDO 2" as a graph number and changes nothing.
#
# So `queries` holds both spellings, likeliest first, and the app asks with each
# until one answers. `writes` holds the matching write for each, so whichever
# query turned out to be right also fixes the shape of the write. A command that
# answers neither query ends up write-only, using the first spelling.
Setting = namedtuple("Setting", "label key queries writes kind choices")


def spellings(root, trace, first_indexed):
    """The (query, write) pair for each way the command could be spelled,
    likeliest first."""
    t = 0 if trace is None else trace
    indexed = (f"{root}? {t}", f"{root} {t},{{v}}")
    plain = (root + "?", root + " {v}")
    pair = (indexed, plain) if first_indexed else (plain, indexed)
    return tuple(p[0] for p in pair), tuple(p[1] for p in pair)


def num(label, key, root, first_indexed=False):
    queries, writes = spellings(root, None, first_indexed)
    return Setting(label, key, queries, writes, "num", None)


def enum(label, key, root, choices, trace=None, first_indexed=None):
    if first_indexed is None:
        first_indexed = trace is not None
    queries, writes = spellings(root, trace, first_indexed)
    return Setting(label, key, queries, writes, "enum", choices)


SETTING_GROUPS = [
    ("Frequency", [
        enum("Span", "SPAN", "SPAN", SPAN_CHOICES),
        num("Start freq (Hz)", "STRF", "STRF"),
        num("Center freq (Hz)", "CTRF", "CTRF"),
        # The window trades frequency resolution against spectral leakage:
        # uniform resolves best, flattop is most accurate on sine amplitude,
        # Hanning is the one to use on a noise floor, BMH separates close peaks.
        # WNDO is graph-indexed both ways: it answers "WNDO? 0" and needs
        # "WNDO 0,i". A bare "WNDO i" is taken as a graph number and does
        # nothing, which is what the bench scripts were sending.
        enum("Window", "WNDO", "WNDO",
             ("Uniform", "Flattop", "Hanning", "BMH"), first_indexed=True),
    ]),
    ("Measurement (trace 0)", [
        enum("Measurement", "MEAS0", "MEAS",
             ("Spectrum", "PSD", "Time record", "Octave"), TRACE),
        enum("Display", "DISP0", "DISP",
             ("LogMag", "LinMag", "Real", "Imag", "Phase"), TRACE),
        enum("Units", "UNIT0", "UNIT",
             ("Vpk / deg", "Vrms / rad", "dBV", "dBVrms"), TRACE),
        enum("Volts / EU", "VOEU0", "VOEU", ("Volts", "EU"), TRACE),
    ]),
    ("Input", [
        enum("Source", "ISRC", "ISRC", ("A", "A-B")),
        enum("Coupling", "ICPL", "ICPL", ("AC", "DC")),
        enum("Grounding", "IGND", "IGND", ("Float", "Ground")),
        num("Range (dBV)", "IRNG", "IRNG"),
        enum("Auto range", "ARNG", "ARNG", ("Manual", "Auto")),
        enum("Auto offset", "AOFM", "AOFM", ("Off", "On")),
    ]),
    ("Averaging", [
        enum("Averaging", "AVGO", "AVGO", ("Off", "On")),
        num("Number", "NAVG", "NAVG"),
        enum("Type", "AVGT", "AVGT", ("RMS", "Vector", "Peak hold")),
        enum("Mode", "AVGM", "AVGM", ("Linear", "Exponential")),
        num("Overlap (%)", "OVLP", "OVLP"),
    ]),
    ("Display", [
        enum("Active trace", "ACTG", "ACTG", ("Trace 0", "Trace 1")),
        # Single/dual and linked markers are global underneath: they answer
        # the plain query and take a single parameter, so "FMTS 0,1" sets
        # nothing but the leading 0.
        enum("Format", "FMTS0", "FMTS", ("Single", "Dual"), TRACE,
             first_indexed=False),
        enum("Grid", "GRID0", "GRID", ("Off", "8 div", "10 div"), TRACE),
        enum("Style", "FILS0", "FILS", ("Line", "Filled"), TRACE),
        enum("X axis", "XAXS0", "XAXS", ("Linear", "Log"), TRACE),
        enum("Expand", "EXPD0", "EXPD",
             ("8 bins", "15 bins", "30 bins", "64 bins", "128 bins",
              "No expand"), TRACE),
        enum("Marker", "MRKR0", "MRKR", ("Off", "On", "Track"), TRACE),
        enum("Marker width", "MRKW0", "MRKW", ("Norm", "Wide", "Spot"), TRACE),
        enum("Marker seek", "MRKM0", "MRKM", ("Max", "Min", "Mean"), TRACE),
        enum("Linked markers", "MRLK0", "MRLK", ("Off", "On"), TRACE,
             first_indexed=False),
    ]),
]
ALL_SETTINGS = [s for _, group in SETTING_GROUPS for s in group]
BY_KEY = {s.key: s for s in ALL_SETTINGS}

# What the bench scripts set at the top of every run, as instrument codes. The
# Defaults button stages these in the panel rather than writing them, so nothing
# reaches the analyzer without being looked at first.
SCRIPT_DEFAULTS = {
    "SPAN": "11", "STRF": "0", "WNDO": "2",
    "MEAS0": "1", "DISP0": "0", "UNIT0": "1", "VOEU0": "0",
    "ISRC": "0", "ICPL": "0", "IGND": "0", "ARNG": "1", "AOFM": "1",
    "AVGO": "1", "NAVG": "1000", "AVGT": "0", "AVGM": "0",
    "ACTG": "0", "FMTS0": "0", "GRID0": "2", "FILS0": "0", "XAXS0": "0",
    "EXPD0": "5", "MRKR0": "2", "MRKW0": "0", "MRKM0": "0", "MRLK0": "0",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_name(name):
    """Turn typed text into something safe to put in a CSV header or a file
    name: ASCII word characters only, so no delimiter or encoding surprises."""
    out = "".join(c if (c.isascii() and (c.isalnum() or c in "-.")) else "_"
                  for c in name.strip())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def unique_base(path, base, suffixes):
    """First of '<base>', '<base>_1', '<base>_2' ... for which none of
    `suffixes` exists yet, returned as a path with no suffix on it. Testing
    every suffix together keeps the csv, png and txt of one capture sharing a
    name instead of drifting apart by a counter."""
    name, n = base, 0
    while any(os.path.exists(os.path.join(path, name + suffix))
              for suffix in suffixes):
        n += 1
        name = f"{base}_{n}"
    return os.path.join(path, name)


def fmt_setting(s, raw):
    """Normalise an analyzer reply into what the panel displays. An unexpected
    reply is shown as it arrived rather than swallowed."""
    raw = (raw or "").strip()
    if s.kind == "enum":
        try:
            return s.choices[int(float(raw))]
        except (ValueError, IndexError):
            return raw
    try:
        return f"{float(raw):g}"
    except ValueError:
        return raw


def parse_setting(s, shown):
    """Panel text back to the code the analyzer expects. Raises ValueError on
    anything that is not a listed choice or a number."""
    shown = shown.strip()
    if s.kind == "enum":
        return str(s.choices.index(shown))
    return f"{float(shown):g}"


def parse_list(text):
    """Comma or space separated numbers, or 'start:stop:step'. An empty box
    gives [], which callers read as 'leave the instrument where it is'."""
    text = text.strip()
    if not text:
        return []
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 3:
            raise ValueError("ranges are start:stop:step")
        first, last, step = (float(p) for p in parts)
        if step == 0:
            raise ValueError("step cannot be 0")
        out, value = [], first
        while (value <= last + 1e-9) if step > 0 else (value >= last - 1e-9):
            out.append(value)
            value += step
            if len(out) > 2000:
                raise ValueError("that range is more than 2000 steps")
        return out
    return [float(p) for p in text.replace(",", " ").split()]


def code_of(snap, key, default=None):
    """One instrument code out of a settings snapshot, as an int."""
    try:
        return int(float(snap[key]))
    except (KeyError, TypeError, ValueError):
        return default


def trace_units(snap):
    """Y-axis label implied by the measurement, display and unit codes.

    A LogMag display hands back dB even when the units are set to volts, which
    is why the scripts that hard-coded 'dbV' were right by accident - so the
    label follows the display mode as well as UNIT, and PSD adds the per root
    hertz that a spectrum does not have."""
    meas = code_of(snap, "MEAS0", 0)
    disp = code_of(snap, "DISP0", 0)
    unit = code_of(snap, "UNIT0", 2)
    if unit not in (0, 1, 2, 3):
        unit = 2
    if disp == 4:                                     # phase
        return "deg" if unit == 0 else "rad"
    log_scale = disp == 0 or unit in (2, 3)
    label = (("dBVpk", "dBVrms", "dBV", "dBVrms") if log_scale
             else ("Vpk", "Vrms", "V", "Vrms"))[unit]
    if meas == 1:                                     # PSD
        label += "/sqrtHz"
    return label


def span_hz(code):
    return SPANS[code][1] if 0 <= code < len(SPANS) else float("nan")


# ---------------------------------------------------------------------------
# Instrument layer
# ---------------------------------------------------------------------------

class Analyzer:
    """The SR760 over GPIB. One VISA session, so every exchange the GUI makes
    is serialised through the worker thread."""

    def __init__(self):
        self.rm = None
        self.inst = None
        self.idn = ""
        self.addr = ""

    def connect(self, addr=None):
        self.close()
        self.rm = pyvisa.ResourceManager()
        if addr:
            candidates = [addr]
        else:
            candidates = [r for r in self.rm.list_resources()
                          if r.upper().startswith("GPIB")]
        if not candidates:
            raise RuntimeError("No GPIB resources found - check the cable and "
                               "that NI MAX sees the SR760.")
        problems = []
        for res in candidates:
            dev = None
            try:
                dev = self.rm.open_resource(res)
                dev.timeout = CONNECT_TIMEOUT_MS
                # The SR760 ends a command at the first line feed, and pyvisa's
                # default terminator leaves a stray carriage return behind it,
                # so pin the write terminator down. Replies finish on EOI, which
                # is why nothing is set for reads.
                dev.write_termination = "\n"
                try:
                    idn = dev.query("*IDN?").strip()
                except Exception:
                    # An analyzer left on the RS232 output takes GPIB commands
                    # but answers down the serial port, so the first query just
                    # times out. Point the output back at GPIB and ask again -
                    # this is the "*IDN? always times out" the scripts lived with.
                    dev.clear()
                    dev.write("OUTP 1")
                    idn = dev.query("*IDN?").strip()
            except Exception as exc:
                problems.append(f"  {res}: {exc}")
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
                continue
            # A typed address is taken at its word; a scan only claims an SR760.
            if addr or "SR760" in idn.upper():
                dev.timeout = OP_TIMEOUT_MS
                self.inst, self.idn, self.addr = dev, idn, res
                return idn
            problems.append(f"  {res}: not an SR760 ({idn})")
            dev.close()
        raise RuntimeError("No SR760 found.\n" + "\n".join(problems))

    def close(self):
        for obj in (self.inst, self.rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.inst = self.rm = None

    # -- primitives -------------------------------------------------------

    def put(self, cmd):
        self.inst.write(cmd)

    def get(self, query):
        return self.inst.query(query).strip()

    def recover(self):
        """Flush a half-finished exchange after a timeout, so the next query
        cannot read the previous reply."""
        try:
            self.inst.clear()
        except Exception:
            pass

    def error_byte(self):
        """SR760 error status byte, cleared by reading it. Non-zero means the
        analyzer objected to something we sent; the manual's ERRS table names
        the individual bits."""
        try:
            return int(self.get("ERRS?"))
        except Exception:
            return 0

    def lock_panel(self, locked):
        """OVRM 0 locks the front panel for the duration of a measurement, so a
        stray knob cannot change the settings the metadata says were used."""
        self.put("OVRM 0" if locked else "OVRM 1")

    def autoscale(self):
        self.put("AUTS 0")

    # -- acquisition ------------------------------------------------------

    def autorange(self, seconds, stop=None, poll=0.5):
        """Step the input range down to the most sensitive setting that does not
        overload, then freeze it.

        Auto range walks the range up from -60 dBV until the overload clears, so
        it needs to be left on long enough to settle before going back to
        manual - otherwise a later run in the same sweep moves the range and the
        noise floors no longer compare. Returns the range it settled on and how
        often an overload bit showed up while it worked."""
        self.put("ARNG 0")
        self.put("ARNG 1")
        overloads = polls = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            if stop is not None and stop():
                break
            try:
                # The two overload bits the bench scripts watched while ranging.
                errs = int(self.get("ERRS?7"))
                ffts = int(self.get("FFTS?5"))
            except Exception:
                self.recover()
                break
            polls += 1
            if errs or ffts:
                overloads += 1
            time.sleep(poll)
        self.put("ARNG 0")
        try:
            rng = self.get("IRNG?")
        except Exception:
            self.recover()
            rng = "?"
        return rng, overloads, polls

    def bin_spacing(self, trace, n_bins):
        """Hz between neighbouring bins at the span the analyzer is on.

        Read from the instrument rather than worked out from the span table:
        the table holds the manual's printed values, which are rounded (390 Hz
        for a span that is really 390.625), and that error would accumulate into
        a visible misalignment over a long stitch.

        Measured across the whole trace rather than between two adjacent bins.
        The analyzer prints its bin frequencies to a handful of digits, and
        differencing neighbours would keep all of that rounding while
        differencing end to end divides it by the number of intervals. It also
        makes the spacing the same one trace_binary lays into the frequency
        column of the CSV, so a stitch overlap lands on the saved points."""
        first = self.inst.query_ascii_values(f"BVAL? {trace},0")[0]
        last = self.inst.query_ascii_values(f"BVAL? {trace},{n_bins - 1}")[0]
        return (last - first) / (n_bins - 1)

    def start(self):
        """Restart the average. Same as the [START] key."""
        self.put("STRT")

    def wait_done(self, timeout, stop=None, poll=0.25):
        """Wait for the averaged measurement to finish.

        Bit 0 of the serial poll byte is clear while a measurement runs, so the
        poll starts after a short settle: asking immediately can catch the bit
        still set from the previous run and return at once. Returns 'done',
        'timeout', 'stopped' or 'error'."""
        time.sleep(0.5)
        deadline = time.time() + timeout
        misses = 0
        while time.time() < deadline:
            if stop is not None and stop():
                return "stopped"
            try:
                if int(self.get("*STB?0")):
                    return "done"
                misses = 0
            except Exception:
                self.recover()
                misses += 1
                if misses >= 3:
                    return "error"
            time.sleep(poll)
        return "timeout"

    def trace_ascii(self, trace, n_bins, progress=None):
        """Bin-by-bin ASCII readout: two queries per bin, so slow, but valid
        whatever the display is set to."""
        freqs, amps = [], []
        for i in range(n_bins):
            f = self.inst.query_ascii_values(f"BVAL? {trace},{i}")[0]
            time.sleep(0.0005)   # the analyzer drops replies if pushed harder
            a = self.inst.query_ascii_values(f"SPEC? {trace},{i}")[0]
            time.sleep(0.0005)
            freqs.append(f)
            amps.append(a)
            if progress is not None and (i + 1) % 100 == 0:
                progress(i + 1, n_bins)
        return np.array(freqs), np.array(amps)

    def trace_binary(self, trace, n_bins):
        """SPEB? dump: the whole trace as int16 display counts in one read, some
        two orders of magnitude faster than asking bin by bin.

        The counts carry a dB mapping, so this is only valid while the display
        is LogMag - the caller checks before choosing it. Bin 0 is also read the
        slow way and used to put the dump back on the analyzer's own scale,
        which absorbs whatever reference the chosen units imply."""
        start_freq = self.inst.query_ascii_values(f"BVAL? {trace},0")[0]
        stop_freq = self.inst.query_ascii_values(f"BVAL? {trace},{n_bins - 1}")[0]
        freqs = np.linspace(start_freq, stop_freq, n_bins)

        first_bin = self.inst.query_ascii_values(f"SPEC? {trace},0")[0]
        self.inst.write(f"SPEB? {trace}")
        raw = self.inst.read_bytes(2 * n_bins, break_on_termchar=False)
        counts = np.frombuffer(raw, dtype="<i2")
        amps = (3.0103 * counts) / 512.0 - 114.3914
        return freqs, amps + (first_bin - amps[0])


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def write_csv(path, freqs, amps, ylabel):
    """Two columns, headed the way the trace was actually measured rather than
    with a fixed 'dbV' that goes stale as soon as the units change."""
    rows = np.column_stack([freqs, amps])
    np.savetxt(path, rows, delimiter=",", comments="",
               header=f"Frequency (Hz),{safe_name(ylabel)}")


def save_plot(path, traces, title, ylabel, ymin, ymax, legend=True):
    """Draw one or more traces to a PNG. The default y window is kept unless the
    data falls outside it, and the title says so when it had to be widened - so
    a plot that looks unlike the others is flagged rather than silently
    rescaled. Linear traces are left to autoscale."""
    fig = Figure(figsize=(8, 6))
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    for freqs, amps, label in traces:
        ax.plot(freqs, amps, lw=1.2, label=label,
                color="blue" if len(traces) == 1 else None)

    rescaled = False
    if ymin is not None and ymax is not None:
        low = min(float(np.min(a)) for _, a, _ in traces)
        high = max(float(np.max(a)) for _, a, _ in traces)
        if high > ymax:
            ymax, rescaled = high, True
        if low < ymin:
            ymin, rescaled = low, True
        ax.set_ylim([ymin, ymax])

    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel(ylabel)
    ax.set_title(f"y-scale changed :: {title}" if rescaled else title)
    ax.grid(True)
    if legend and 1 < len(traces) <= 12:
        ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=PLOT_DPI)


def metadata_text(an, snap, extra, command):
    """The capture described by the same settings snapshot the panel is showing,
    so the two can never disagree about what the analyzer was doing. Each
    setting is followed by the command that would put it back, in the spelling
    this analyzer turned out to use."""
    lines = [
        f"captured             : {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"instrument           : {an.idn}",
        f"visa address         : {an.addr}",
    ]
    for label, value in extra.items():
        lines.append(f"{label:<21}: {value}")
    lines.append("")
    lines.append("analyzer settings")
    for group, settings in SETTING_GROUPS:
        lines.append(f"  [{group}]")
        for s in settings:
            raw = snap.get(s.key)
            if raw is None:
                continue
            lines.append(f"    {s.label:<17}: {fmt_setting(s, raw):<14} "
                         f"({command(s.key, raw)})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    last_ylabel = "dBV"       # unit of the most recent snapshot, for sweep plots

    def __init__(self, root):
        self.root = root
        self.an = Analyzer()
        self.msgs = queue.Queue()
        self.busy = False
        self.abort = threading.Event()
        self.auto_job = None
        self.dead = {}            # setting key -> consecutive query failures
        self.qform = {}           # setting key -> index of the query form that works

        root.title("Spectrum Grab - SR760 FFT")
        win_w = min(1180, root.winfo_screenwidth() - 60)
        win_h = min(950, root.winfo_screenheight() - 80)
        root.geometry(f"{win_w}x{win_h}+30+15")

        pad = dict(padx=8, pady=4)
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="left", fill="y")

        self.build_connection(left, pad)
        self.build_output(left, pad)
        self.build_grab(left, pad)
        self.build_sweep(left, pad)
        self.build_options(left, pad)
        self.build_log(left, pad)
        self.build_preview(right, pad)
        self.build_settings(right, pad)

        root.bind("<space>", self.on_space)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.saved_cfg = None
        if Figure is None:
            self.log("matplotlib is not installed for this Python, so there are "
                     "no plots and no preview.")
            self.log("  CSV and metadata are unaffected. To get plots: "
                     "pip install matplotlib")
        self.load_config()
        self.root.after(100, self.pump)
        self.root.after(300, self.do_connect)
        self.load_latest_preview()

    # -- layout -----------------------------------------------------------

    def build_connection(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Analyzer")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Address:").pack(side="left")
        self.addr = tk.StringVar(value=DEFAULT_ADDRESS)
        ttk.Entry(row, textvariable=self.addr, width=22).pack(side="left", padx=6)
        self.connect_btn = ttk.Button(row, text="Connect", command=self.do_connect)
        self.connect_btn.pack(side="right")
        self.status = ttk.Label(f, text="Not connected", foreground="#a00")
        self.status.pack(anchor="w", padx=8, pady=(0, 6))

    def build_output(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Save to")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(6, 2))
        default_dir = os.path.join(os.path.expanduser("~"), "Desktop",
                                   "spectrum_data")
        self.outdir = tk.StringVar(value=default_dir)
        ttk.Entry(row, textvariable=self.outdir).pack(side="left", fill="x",
                                                      expand=True)
        ttk.Button(row, text="...", width=3,
                   command=self.pick_dir).pack(side="left", padx=6)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="Title:").pack(side="left")
        self.title = tk.StringVar(value="sr760")
        ttk.Entry(row, textvariable=self.title).pack(side="left", fill="x",
                                                     expand=True, padx=6)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(2, 6))
        self.dated = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="dated subfolder", variable=self.dated,
                        command=self.load_latest_preview).pack(side="left")
        self.save_csv = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="csv", variable=self.save_csv).pack(side="left",
                                                                     padx=8)
        self.save_png = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="plot", variable=self.save_png).pack(side="left")
        self.save_txt = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="metadata", variable=self.save_txt).pack(
            side="left", padx=8)

    def build_grab(self, parent, pad):
        f = ttk.Frame(parent)
        f.pack(fill="x", **pad)
        self.grab_btn = ttk.Button(f, text="GRAB  (or press Space)",
                                   command=self.do_grab, state="disabled")
        self.grab_btn.pack(side="left", fill="x", expand=True, ipady=8)
        self.stop_btn = ttk.Button(f, text="Stop", width=6,
                                   command=self.do_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6, ipady=8)

        f = ttk.Frame(parent)
        f.pack(fill="x", **pad)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Auto-grab every", variable=self.auto,
                        command=self.toggle_auto).pack(side="left")
        self.interval = tk.StringVar(value="60")
        ttk.Entry(f, textvariable=self.interval, width=6).pack(side="left", padx=4)
        ttk.Label(f, text="seconds").pack(side="left")

    def build_sweep(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Sweep  (blank = one grab at the current "
                                        "settings)")
        f.pack(fill="x", **pad)
        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=6, pady=(6, 2))
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Span codes:").grid(row=0, column=0, sticky="e",
                                                 padx=(0, 4), pady=1)
        self.spans_txt = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.spans_txt).grid(row=0, column=1,
                                                          sticky="ew", pady=1)
        ttk.Label(grid, text="Start freqs (Hz):").grid(row=1, column=0, sticky="e",
                                                       padx=(0, 4), pady=1)
        self.starts_txt = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.starts_txt).grid(row=1, column=1,
                                                           sticky="ew", pady=1)
        ttk.Label(grid, text="Cases:").grid(row=2, column=0, sticky="e",
                                            padx=(0, 4), pady=1)
        self.cases_txt = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.cases_txt).grid(row=2, column=1,
                                                          sticky="ew", pady=1)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="Stitch to").pack(side="left")
        self.stitch_stop = tk.StringVar(value="1000")
        ttk.Entry(row, textvariable=self.stitch_stop, width=8).pack(side="left",
                                                                    padx=4)
        ttk.Label(row, text="Hz, overlap").pack(side="left")
        self.stitch_overlap = tk.StringVar(value="10")
        ttk.Entry(row, textvariable=self.stitch_overlap, width=5).pack(side="left",
                                                                       padx=4)
        ttk.Label(row, text="points").pack(side="left")
        ttk.Button(row, text="Fill start freqs",
                   command=self.do_stitch).pack(side="left", padx=8)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(2, 6))
        self.save_npy = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="save .npy matrices",
                        variable=self.save_npy).pack(side="left")
        self.combined = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="combined plot",
                        variable=self.combined).pack(side="left", padx=10)
        self.pause_cases = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="pause before each case",
                        variable=self.pause_cases).pack(side="left")

    def build_options(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Acquisition")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(6, 2))
        self.binary = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="fast binary transfer (SPEB?)",
                        variable=self.binary).pack(side="left")
        self.lock = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="lock front panel while measuring",
                        variable=self.lock).pack(side="left", padx=10)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        self.do_arng = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="auto-range then freeze, for",
                        variable=self.do_arng).pack(side="left")
        self.arng_s = tk.StringVar(value="15")
        ttk.Entry(row, textvariable=self.arng_s, width=5).pack(side="left", padx=4)
        ttk.Label(row, text="s").pack(side="left")

        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=6, pady=(2, 6))
        for i, (label, attr, default) in enumerate(
                (("settle before start (s)", "settle_s", "0"),
                 ("measurement timeout (s)", "timeout_s", "600"),
                 ("plot y min (dB)", "ymin", f"{DEFAULT_YMIN:g}"),
                 ("plot y max (dB)", "ymax", f"{DEFAULT_YMAX:g}"))):
            r, c = divmod(i, 2)
            ttk.Label(grid, text=label + ":").grid(row=r, column=c * 2,
                                                   sticky="e", padx=(0, 4), pady=1)
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            ttk.Entry(grid, textvariable=var, width=8).grid(
                row=r, column=c * 2 + 1, sticky="w", padx=(0, 12), pady=1)

    def build_log(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Log")
        f.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(f, height=8, wrap="none", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)

    def build_preview(self, parent, pad):
        self.shot_frame = ttk.LabelFrame(parent, text="Last plot")
        self.shot_frame.pack(fill="x", **pad)
        box = tk.Frame(self.shot_frame, width=PREVIEW_W, height=PREVIEW_H)
        box.pack(padx=4, pady=4)
        box.pack_propagate(False)     # keep the box from shrinking to the label
        self.preview = ttk.Label(box, text="(no plot yet)", anchor="center")
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Double-Button-1>", self.open_preview)
        self.preview_img = None
        self.preview_path = None

    def build_settings(self, parent, pad):
        self.set_vars = {}        # key -> StringVar shown in the panel
        self.set_marks = {}       # key -> "edited" marker label
        self.set_inst = {}        # key -> value the analyzer last reported
        self.read_stamp = ""      # when the panel last matched the instrument

        for group, settings in SETTING_GROUPS:
            frame = ttk.LabelFrame(parent, text=group)
            frame.pack(fill="x", padx=8, pady=2)
            grid = ttk.Frame(frame)
            grid.pack(fill="x", padx=6, pady=4)
            for i, s in enumerate(settings):
                row, col = divmod(i, 2)
                ttk.Label(grid, text=s.label + ":").grid(
                    row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
                self.setting_widget(grid, s, row, col * 2 + 1)

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=(2, 6))
        self.read_btn = ttk.Button(bar, text="Read", command=self.do_read_settings,
                                   state="disabled")
        self.read_btn.pack(side="left")
        self.apply_btn = ttk.Button(bar, text="Apply changes",
                                    command=self.do_apply_settings,
                                    state="disabled")
        self.apply_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Defaults", command=self.do_defaults).pack(side="left",
                                                                       padx=4)
        self.aoff_btn = ttk.Button(bar, text="Auto-offset",
                                   command=lambda: self.do_action(
                                       "auto offset", lambda: self.an.put("AOFF")),
                                   state="disabled")
        self.aoff_btn.pack(side="left", padx=4)
        self.auts_btn = ttk.Button(bar, text="Auto-scale",
                                   command=lambda: self.do_action(
                                       "auto scale", self.an.autoscale),
                                   state="disabled")
        self.auts_btn.pack(side="left", padx=4)
        self.set_status = ttk.Label(parent, text="not read yet", foreground="#666")
        self.set_status.pack(anchor="w", padx=10)

    def setting_widget(self, parent, s, row, col):
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=col, sticky="w", padx=2, pady=1)
        var = tk.StringVar()
        if s.kind == "enum":
            ttk.Combobox(cell, textvariable=var, values=list(s.choices),
                         width=13, state="readonly").pack(side="left")
        else:
            ttk.Entry(cell, textvariable=var, width=15).pack(side="left")
        mark = ttk.Label(cell, text=" ", width=1, foreground="#c60")
        mark.pack(side="left")
        self.set_vars[s.key] = var
        self.set_marks[s.key] = mark
        self.set_inst[s.key] = ""
        var.trace_add("write", lambda *_: self.refresh_marks())

    # -- helpers ----------------------------------------------------------

    def log(self, text):
        self.msgs.put(text)

    def pump(self):
        while not self.msgs.empty():
            self.logbox.insert("end", self.msgs.get() + "\n")
            self.logbox.see("end")
        self.root.after(100, self.pump)

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or ".")
        if d:
            self.outdir.set(d)
            self.load_latest_preview()
            self.save_config()

    def safe_title(self):
        t = "".join("_" if c in BAD_NAME_CHARS else c
                    for c in self.title.get()).strip()
        return t or "sr760"

    def target_dir(self):
        r"""Where this session writes. The dated subfolder reproduces the
        ...\YYYYMMDD\... layout the scripts built into their PATHNAME."""
        outdir = self.outdir.get()
        if self.dated.get():
            outdir = os.path.join(outdir,
                                  datetime.datetime.now().strftime("%Y%m%d"))
        return outdir

    def float_of(self, var, default, name):
        try:
            return float(var.get())
        except ValueError:
            self.log(f"  ({name} is not a number, using {default})")
            return default

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy or not self.an.inst else "normal"
        for btn in (self.grab_btn, self.read_btn, self.apply_btn,
                    self.aoff_btn, self.auts_btn):
            btn.configure(state=state)
        # Connect is the one button that means something while disconnected.
        self.connect_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def confirm(self, message):
        """Ask on the main thread and block the worker until it is answered -
        the bench scripts' input('Press Enter to continue') between cases."""
        answered = threading.Event()
        box = {}

        def ask():
            box["go"] = messagebox.askokcancel("Spectrum Grab", message)
            answered.set()

        self.root.after(0, ask)
        answered.wait()
        return box.get("go", False)

    # -- preview ----------------------------------------------------------

    def show_preview(self, path):
        """Put a PNG in the preview box. Main thread only (Tk images are not
        thread safe)."""
        try:
            if Image is not None:
                im = Image.open(path)
                im.load()
                k = min(PREVIEW_W / im.width, PREVIEW_H / im.height, 1.0)
                if k < 1.0:
                    im = im.resize((max(1, round(im.width * k)),
                                    max(1, round(im.height * k))),
                                   Image.LANCZOS)
                img = ImageTk.PhotoImage(im)
            else:
                img = tk.PhotoImage(file=path)     # Tk 8.6 reads PNG natively
                k = 1
                while img.width() // k > PREVIEW_W or img.height() // k > PREVIEW_H:
                    k += 1
                if k > 1:
                    img = img.subsample(k)         # integer factors only
        except Exception as exc:
            self.log(f"  (preview failed: {exc})")
            return
        self.preview_img = img            # keep a reference or Tk drops it
        self.preview_path = path
        self.preview.configure(image=img, text="")
        self.shot_frame.configure(
            text=f"Last plot - {os.path.basename(path)}  "
                 f"(double-click to open full size)")

    def load_latest_preview(self):
        outdir = self.target_dir()
        try:
            shots = [os.path.join(outdir, n) for n in os.listdir(outdir)
                     if n.lower().endswith(".png")]
        except OSError:
            return
        if shots:
            self.show_preview(max(shots, key=os.path.getmtime))

    def open_preview(self, _event=None):
        if self.preview_path:
            try:
                os.startfile(self.preview_path)
            except Exception as exc:
                self.log(f"ERROR: {exc}")

    # -- config -----------------------------------------------------------

    def config_vars(self):
        return {
            "addr": self.addr, "outdir": self.outdir, "title": self.title,
            "dated": self.dated, "save_csv": self.save_csv,
            "save_png": self.save_png, "save_txt": self.save_txt,
            "spans": self.spans_txt, "starts": self.starts_txt,
            "cases": self.cases_txt, "stitch_stop": self.stitch_stop,
            "stitch_overlap": self.stitch_overlap, "save_npy": self.save_npy,
            "combined": self.combined, "pause_cases": self.pause_cases,
            "binary": self.binary, "lock": self.lock, "autorange": self.do_arng,
            "autorange_s": self.arng_s, "settle_s": self.settle_s,
            "timeout_s": self.timeout_s, "ymin": self.ymin, "ymax": self.ymax,
            "interval": self.interval,
        }

    def current_cfg(self):
        return {k: v.get() for k, v in self.config_vars().items()}

    def load_config(self):
        """Restore what the last session was using. Anything missing, malformed
        or of the wrong type is ignored and leaves the default in place."""
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            return
        except Exception as exc:
            self.log(f"Ignoring unreadable {CONFIG_PATH}: {exc}")
            return

        for key, var in self.config_vars().items():
            value = cfg.get(key)
            if isinstance(var, tk.BooleanVar):
                if isinstance(value, (bool, int)):
                    var.set(bool(value))
            elif isinstance(value, str):
                var.set(value)
        self.saved_cfg = self.current_cfg()
        self.log(f"Restored last session from {CONFIG_PATH}")

    def save_config(self):
        """Called after a grab, when the folder is picked, and on close. Writes
        only when something actually changed."""
        cfg = self.current_cfg()
        if cfg == self.saved_cfg:
            return
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            self.saved_cfg = cfg
        except Exception as exc:
            self.log(f"Could not save {CONFIG_PATH}: {exc}")

    # -- actions ----------------------------------------------------------

    def on_space(self, event):
        try:
            cls = event.widget.winfo_class()
        except AttributeError:
            cls = ""
        if cls in SPACE_OWNERS:
            return
        self.do_grab()

    def do_connect(self):
        # Connecting drops the VISA session and opens a new one, so it has to
        # wait its turn like everything else - otherwise the startup auto-connect
        # or an impatient click would pull the instrument out from under a sweep.
        if self.busy:
            self.log("Busy - stop the current job before reconnecting.")
            return
        self.set_busy(True)

        def work():
            try:
                idn = self.an.connect(self.addr.get().strip() or None)
                self.root.after(0, lambda: self.status.configure(
                    text=idn[:70], foreground="#060"))
                self.log(f"Connected: {idn}")
                self.log(f"Address:   {self.an.addr}")
                self.root.after(0, lambda: self.addr.set(self.an.addr))
                values = self.read_all_settings(retry_all=True)
                self.root.after(0, lambda v=values: self.show_settings(v))
            except Exception as exc:
                self.root.after(0, lambda: self.status.configure(
                    text="Not connected", foreground="#a00"))
                self.log(f"ERROR: {exc}")
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_stop(self):
        self.abort.set()
        self.log("Stopping after the current step...")

    def do_stitch(self):
        """Fill the start-frequency list with segments that tile 0 Hz up to the
        stop frequency, each repeating a fixed number of the previous segment's
        frequency points.

        Overlapping by points rather than by hertz is what makes the pieces line
        up: stepping by (bins - overlap) spacings puts segment n+1's first point
        exactly on segment n's (400 - overlap)th, so the shared points are the
        same frequencies in both runs and can be matched or averaged directly.
        Zero overlap still leaves no gap - the next segment starts one spacing
        past the last point of the one before."""
        if self.busy:
            return
        try:
            stop = float(self.stitch_stop.get())
            overlap = int(float(self.stitch_overlap.get()))
        except ValueError:
            self.log("Stitch stop and overlap must be numbers.")
            return
        if not 0 <= overlap < N_BINS:
            self.log(f"Overlap must be from 0 to {N_BINS - 1} points.")
            return
        if self.edited("SPAN"):
            self.log("Apply the span change first - the point spacing is read "
                     "from the analyzer, so it would use the old span.")
            return

        if not self.an.inst:
            # Offline: the span table is all there is, and its printed values
            # are rounded, so say so rather than quietly stepping slightly wrong.
            try:
                code = SPAN_CHOICES.index(self.set_vars["SPAN"].get())
            except ValueError:
                self.log("Connect first, or pick a span, to fill the stitch.")
                return
            self.fill_stitch(span_hz(code) / N_BINS, stop, overlap,
                             "estimated from the rounded span table")
            return

        self.set_busy(True)

        def work():
            try:
                spacing = self.an.bin_spacing(TRACE, N_BINS)
                self.root.after(0, lambda: self.fill_stitch(
                    spacing, stop, overlap, "measured on the analyzer"))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.an.recover()
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def fill_stitch(self, spacing, stop, overlap, source):
        """Main thread. Lay the segments out at the given point spacing."""
        step = (N_BINS - overlap) * spacing
        if step <= 0 or not np.isfinite(step):
            self.log(f"A point spacing of {spacing:g} Hz gives no usable step.")
            return
        starts, f = [], 0.0
        while f <= stop and len(starts) < 2000:
            starts.append(f)
            f += step
        self.starts_txt.set(", ".join(f"{v:.10g}" for v in starts))
        self.log(f"{len(starts)} segments of {N_BINS} points at "
                 f"{spacing:.6g} Hz per point ({source}), stepping "
                 f"{N_BINS - overlap} points ({step:.6g} Hz) to {stop:g} Hz - "
                 f"neighbours share {overlap} point(s).")

    def do_action(self, name, fn):
        """One-shot instrument command from a button, then re-read the panel."""
        if self.busy or not self.an.inst:
            return
        self.set_busy(True)

        def work():
            try:
                fn()
                self.log(f"{name} sent")
                values = self.read_all_settings()
                self.root.after(0, lambda v=values: self.show_settings(v))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_grab(self):
        if self.busy or not self.an.inst:
            return
        try:
            spans = [int(round(v)) for v in parse_list(self.spans_txt.get())]
            for code in spans:
                if not 0 <= code < len(SPANS):
                    raise ValueError(f"span code {code} is not in 0-19")
            starts = parse_list(self.starts_txt.get())
        except ValueError as exc:
            self.log(f"Sweep list: {exc}")
            return
        cases = [c.strip() for c in self.cases_txt.get().split(",") if c.strip()]
        self.abort.clear()
        self.set_busy(True)
        threading.Thread(target=self._grab_worker,
                         args=(cases or [""], starts or [None], spans or [None]),
                         daemon=True).start()

    def _grab_worker(self, cases, starts, spans):
        outdir = self.target_dir()
        stamp = datetime.datetime.now().strftime("%Y%m%d")
        total = len(cases) * len(starts) * len(spans)

        # Axes kept in the shape the bench scripts used, so a sweep with one
        # case squeezes down to their [variable, span, bin] matrices. Cells are
        # NaN until a run fills them: a sweep that ends early is then missing
        # data rather than carrying a floor of zeros into the analysis, and the
        # axes still line up with the values written to the JSON beside them.
        freqs_m = np.full((len(cases), len(starts), len(spans), N_BINS), np.nan)
        amps_m = np.full_like(freqs_m, np.nan)
        done = []
        ended = ""

        try:
            os.makedirs(outdir, exist_ok=True)
            if total > 1:
                self.log(f"Sweep: {len(cases)} case(s) x {len(starts)} start "
                         f"freq(s) x {len(spans)} span(s) = {total} runs")

            for ic, case in enumerate(cases):
                if case and self.pause_cases.get():
                    if not self.confirm(f"Set up case '{case}', then continue."):
                        raise KeyboardInterrupt
                for isf, start in enumerate(starts):
                    if start is not None:
                        # .10g, not %g: a stitch step is rarely a round number
                        # and 6 digits would shave a fraction of a bin off it.
                        self.an.put(f"STRF {start:.10g}")
                    for isp, span in enumerate(spans):
                        if span is not None:
                            self.an.put(f"SPAN {span}")
                        if self.abort.is_set():
                            raise KeyboardInterrupt
                        label = " ".join(
                            p for p in (case,
                                        f"span {span}" if span is not None else "",
                                        f"start {start:g} Hz" if start else "")
                            if p)
                        if label:
                            self.log(f"--- {label}")
                        freqs, amps, snap, notes = self.run_one()
                        freqs_m[ic, isf, isp] = freqs
                        amps_m[ic, isf, isp] = amps
                        done.append((freqs, amps, label or self.safe_title()))
                        self.save_run(outdir, stamp, case, freqs, amps, snap,
                                      notes)
                        self.root.after(0, lambda v=snap: self.show_settings(v))
        except KeyboardInterrupt:
            ended = "stopped"
        except Exception as exc:
            ended = "failed"
            self.log(f"ERROR: {exc}")

        try:
            self.an.lock_panel(False)
        except Exception:
            pass

        # Whatever ended the sweep, write up the runs that did complete: the
        # point of stopping early is usually that the segments captured so far
        # are enough, so they still get their matrices and their combined plot.
        if ended:
            self.log(f"{ended.capitalize()} after {len(done)} of {total} run(s).")
        try:
            if done and total > 1:
                self.save_sweep(outdir, stamp, cases, starts, spans,
                                freqs_m, amps_m, done, total, ended)
        except Exception as exc:
            self.log(f"ERROR: the sweep files could not be written: {exc}")
        self.root.after(0, self.save_config)
        self.root.after(0, lambda: self.set_busy(False))

    def run_one(self):
        """One measurement: range, average, read out. Returns the trace, the
        settings snapshot it was taken under and the notes for the metadata."""
        notes = {}
        locked = self.lock.get()
        if locked:
            self.an.lock_panel(True)
            self.log("  front panel locked")
        try:
            if self.do_arng.get():
                seconds = self.float_of(self.arng_s, 15.0, "auto-range time")
                rng, overloads, polls = self.an.autorange(
                    seconds, stop=self.abort.is_set)
                notes["auto range"] = (f"{seconds:g} s, settled at {rng} dBV, "
                                       f"overload on {overloads}/{polls} polls")
                self.log(f"  auto-range done, range {rng} dBV "
                         f"(overload on {overloads}/{polls} polls)")
            settle = self.float_of(self.settle_s, 0.0, "settle")
            if settle > 0:
                time.sleep(settle)
            if self.abort.is_set():
                # Stopped while ranging or settling: no point restarting the
                # average just to abandon it on the first poll.
                raise KeyboardInterrupt

            timeout = self.float_of(self.timeout_s, 600.0, "timeout")
            self.log("  measuring...")
            t0 = time.perf_counter()
            self.an.start()
            state = self.an.wait_done(timeout, stop=self.abort.is_set)
            measured = time.perf_counter() - t0
            if state == "stopped":
                raise KeyboardInterrupt
            if state != "done":
                self.log(f"  (measurement {state} after {measured:.1f} s - "
                         f"reading the trace as it stands)")
            else:
                self.log(f"  measured in {measured:.1f} s")
            notes["measure time (s)"] = f"{measured:.3f}"
            notes["measurement"] = state
            self.an.autoscale()

            # Read the settings before the transfer: the display mode decides
            # whether the binary dump is valid, and the same snapshot goes in
            # the metadata and the panel.
            snap = self.read_all_settings()
            log_display = code_of(snap, "DISP0", 0) == 0
            binary = self.binary.get() and log_display
            if self.binary.get() and not binary:
                self.log("  (linear display: falling back to the ASCII readout, "
                         "the binary dump is a dB mapping)")

            t0 = time.perf_counter()
            if binary:
                freqs, amps = self.an.trace_binary(TRACE, N_BINS)
            else:
                freqs, amps = self.an.trace_ascii(
                    TRACE, N_BINS,
                    progress=lambda i, n: self.log(f"    {i}/{n} bins"))
            transferred = time.perf_counter() - t0
            notes["transfer"] = ("binary dump (SPEB?)" if binary
                                 else "bin by bin (BVAL?/SPEC?)")
            notes["transfer time (s)"] = f"{transferred:.3f}"
            self.log(f"  {len(freqs)} points in {transferred:.2f} s "
                     f"({'binary' if binary else 'ascii'})")
            return freqs, amps, snap, notes
        finally:
            if locked:
                self.an.lock_panel(False)
                self.log("  front panel unlocked")

    def save_run(self, outdir, stamp, case, freqs, amps, snap, notes):
        """CSV, plot and metadata for one measurement, under one shared base
        name so the three files of a capture always belong together."""
        code = code_of(snap, "SPAN")
        start = float(freqs[0])
        maxfreq = round(float(np.max(freqs)))
        ylabel = trace_units(snap)

        parts = [self.safe_title()]
        if case:
            parts.append(safe_name(case))
        if code is not None:
            parts.append(f"span{code}")
        if start:
            parts.append(f"strf{start:g}Hz")
        parts += [f"{maxfreq}Hz", stamp]
        wanted = [e for e, on in ((".csv", self.save_csv.get()),
                                  (".png", self.save_png.get()),
                                  (".txt", self.save_txt.get())) if on]
        if not wanted:
            self.log("  (nothing ticked to save)")
            return
        base = unique_base(outdir, "_".join(parts), wanted)
        title = os.path.basename(base)

        if self.save_csv.get():
            write_csv(base + ".csv", freqs, amps, ylabel)
            self.log(f"  {title}.csv")
        if self.save_txt.get():
            extra = {
                "span": (f"{code} - {SPANS[code][0]}"
                         if code is not None and 0 <= code < len(SPANS) else "?"),
                "start frequency (Hz)": f"{start:g}",
                "stop frequency (Hz)": f"{float(freqs[-1]):g}",
                "bins": str(len(freqs)),
                "trace units": ylabel,
            }
            extra.update(notes)
            with open(base + ".txt", "w", encoding="utf-8") as fh:
                fh.write(metadata_text(self.an, snap, extra, self.command))
        if self.save_png.get():
            self.write_plot(base + ".png", [(freqs, amps, title)], title, ylabel)

    def write_plot(self, path, traces, title, ylabel):
        if Figure is None:
            self.log("  (no matplotlib: skipping the plot)")
            return
        ymin = ymax = None
        if ylabel.startswith("dB"):
            ymin = self.float_of(self.ymin, DEFAULT_YMIN, "y min")
            ymax = self.float_of(self.ymax, DEFAULT_YMAX, "y max")
        try:
            save_plot(path, traces, title, ylabel, ymin, ymax)
        except Exception as exc:
            self.log(f"  (plot failed: {exc})")
            return
        self.log(f"  {os.path.basename(path)}")
        self.root.after(0, lambda p=path: self.show_preview(p))

    def save_sweep(self, outdir, stamp, cases, starts, spans, freqs_m, amps_m,
                   done, planned, ended=""):
        """The whole sweep in one place: the raw matrices, a JSON note of what
        each axis means, and every trace on one pair of axes.

        Written the same way whether the sweep ran to the end or was stopped
        partway, so a run cut short still gives the concatenated picture of the
        segments it did capture. What is missing says so - unmeasured cells stay
        NaN, the JSON counts the runs, and the plot title carries the shortfall
        rather than passing a partial sweep off as a whole one."""
        base = unique_base(outdir, f"{self.safe_title()}_sweep_{stamp}",
                           ["_freqs.npy", "_amps.npy", "_axes.json", ".png"])
        title = os.path.basename(base)
        if len(done) < planned:
            title += f"  ({ended or 'incomplete'} after {len(done)} of {planned})"
        if self.save_npy.get():
            np.save(base + "_freqs.npy", freqs_m)
            np.save(base + "_amps.npy", amps_m)
            axes = {
                "shape": "[case][start frequency][span][bin]",
                "cases": cases,
                "start_freqs_hz": ["current" if s is None else s for s in starts],
                "span_codes": ["current" if s is None else s for s in spans],
                "span_hz": ["current" if s is None else span_hz(s) for s in spans],
                "bins": N_BINS,
                "runs_planned": planned,
                "runs_completed": len(done),
                "ended_early": ended or None,
                "unmeasured": "NaN",
            }
            with open(base + "_axes.json", "w", encoding="utf-8") as fh:
                json.dump(axes, fh, indent=2)
            self.log(f"{os.path.basename(base)}_freqs.npy / _amps.npy "
                     f"{freqs_m.shape}"
                     + (f", {len(done)} of {planned} filled"
                        if len(done) < planned else ""))
        if self.combined.get():
            # Every run in a sweep is measured the same way, so they share a y
            # axis - the label comes from the last settings snapshot.
            self.write_plot(base + ".png", done, title, self.last_ylabel)

    # -- settings panel ---------------------------------------------------

    def edited(self, key):
        """True if the panel value differs from what the analyzer last said."""
        return self.set_vars[key].get().strip() != self.set_inst[key]

    def refresh_marks(self):
        pending = 0
        for key, mark in self.set_marks.items():
            if self.edited(key):
                pending += 1
                mark.configure(text="*")
            else:
                mark.configure(text=" ")
        if not self.read_stamp:
            self.set_status.configure(text="not read yet", foreground="#666")
        elif pending:
            self.set_status.configure(
                text=f"{pending} edit(s) not applied - press Apply changes",
                foreground="#c60")
        else:
            self.set_status.configure(
                text=f"in sync with the analyzer ({self.read_stamp})",
                foreground="#060")

    def show_settings(self, values, overwrite=False):
        """Main thread only. Puts instrument values in the panel, keeping any
        edit not applied yet - unless overwrite is set, which is the case after
        an Apply, when the analyzer is the authority on what took effect."""
        kept = 0
        for key, raw in values.items():
            s = BY_KEY[key]
            value = fmt_setting(s, raw)
            was_edited = self.edited(key)
            self.set_inst[key] = value
            if overwrite or not was_edited:
                self.set_vars[key].set(value)
            elif self.set_vars[key].get().strip() != value:
                kept += 1
        if values:
            self.last_ylabel = trace_units(values)
        self.read_stamp = datetime.datetime.now().strftime("%H:%M:%S")
        if kept:
            self.log(f"  (panel: kept {kept} unapplied edit(s), the analyzer "
                     f"reports something else)")
        self.refresh_marks()

    def read_all_settings(self, retry_all=False):
        """Instrument thread only. Returns the analyzer's own replies as
        {key: reply}.

        Each setting is asked with the query form that worked last time, or with
        each candidate form in turn the first time round. A setting nothing
        answers costs a whole VISA timeout per form, so one that fails twice is
        dropped until the next explicit Read - it stays editable and writable,
        it just cannot be read back."""
        if retry_all:
            self.dead.clear()
            self.qform.clear()
        values = {}
        previous = self.an.inst.timeout
        self.an.inst.timeout = SETTINGS_TIMEOUT_MS
        try:
            for s in ALL_SETTINGS:
                if self.dead.get(s.key, 0) >= 2:
                    continue
                known = self.qform.get(s.key)
                tries = (s.queries[known],) if known is not None else s.queries
                for query in tries:
                    try:
                        values[s.key] = self.an.get(query)
                    except Exception as exc:
                        self.an.recover()
                        failure = f"{query} failed: {exc}"
                        continue
                    if known is None:
                        self.qform[s.key] = s.queries.index(query)
                        if query != s.queries[0]:
                            self.log(f"  ({s.label} answers {query}, "
                                     f"not {s.queries[0]})")
                    self.dead[s.key] = 0
                    break
                else:
                    self.dead[s.key] = self.dead.get(s.key, 0) + 1
                    self.log(f"  {failure}")
                    if self.dead[s.key] >= 2:
                        self.log(f"  ({s.label} cannot be read back - leaving it "
                                 f"write-only until the next Read)")
        finally:
            self.an.inst.timeout = previous
        return values

    def do_read_settings(self):
        if self.busy or not self.an.inst:
            return
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(None,),
                         daemon=True).start()

    def do_apply_settings(self):
        if self.busy or not self.an.inst:
            return
        changes = {}
        for key, var in self.set_vars.items():
            if not self.edited(key):
                continue
            try:
                changes[key] = parse_setting(BY_KEY[key], var.get())
            except ValueError:
                self.log(f"  {key}: '{var.get()}' is not a valid value, skipped")
        if not changes:
            self.log("No setting changes to apply.")
            return
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(changes,),
                         daemon=True).start()

    def do_defaults(self):
        """Stage what the bench scripts wrote at the top of every run. Nothing
        goes to the analyzer until Apply, so the whole block can be looked over
        first."""
        for key, code in SCRIPT_DEFAULTS.items():
            self.set_vars[key].set(fmt_setting(BY_KEY[key], code))
        self.log("Script defaults staged - press Apply changes to send them.")

    def _settings_worker(self, changes):
        try:
            if changes:
                for key, value in changes.items():
                    self.an.put(self.command(key, value))
                    self.log(f"  {self.command(key, value)}")
                self.report_status()
            # Read back either way: after a write the analyzer is the authority
            # on what it accepted, since it clamps values it dislikes.
            values = self.read_all_settings(retry_all=not changes)
            self.root.after(0,
                            lambda v=values, c=changes: self.after_settings(v, c))
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def report_status(self):
        """Log the error status byte if the analyzer set anything. Bit 7 is the
        input overload the ranging routine watches - it says nothing about the
        commands just sent, so it is called out separately rather than read as a
        rejection."""
        err = self.an.error_byte()
        if not err:
            return
        bits = [i for i in range(8) if err & (1 << i)]
        if bits == [7]:
            self.log("  (input overload flagged - ERRS bit 7. Not a rejected "
                     "command; check the input range.)")
        else:
            self.log(f"  ERRS = {err}, bits {bits} set - see the ERRS table in "
                     f"the manual")

    def after_settings(self, values, changes):
        """Main thread. Show what the analyzer reported, then say plainly which
        writes did not stick and let the rest stand."""
        self.show_settings(values, overwrite=bool(changes))
        for key, code in sorted((changes or {}).items()):
            if key not in values:
                continue
            asked = fmt_setting(BY_KEY[key], code)
            got = fmt_setting(BY_KEY[key], values[key])
            if asked != got:
                # Either the analyzer clamped the value, or the command was not
                # in the shape it wanted - which it accepts without complaining.
                self.log(f"  ({BY_KEY[key].label}: asked for {asked}, the "
                         f"analyzer reports {got})")
        blind = sorted(k for k in (changes or {}) if k not in values)
        for key in blind:
            shown = fmt_setting(BY_KEY[key], changes[key])
            self.set_inst[key] = shown
            self.set_vars[key].set(shown)
        if blind:
            self.log("  (" + ", ".join(BY_KEY[k].label for k in blind)
                     + ": written but not readable, so the panel takes the "
                       "write at face value)")
            self.refresh_marks()

    def command(self, key, value):
        """The write for a setting, in the spelling its query turned out to use.
        Falls back to the likelier spelling for a setting that has never been
        read back successfully."""
        s = BY_KEY[key]
        return s.writes[self.qform.get(key, 0)].format(v=value)

    # -- auto-grab --------------------------------------------------------

    def toggle_auto(self):
        if self.auto.get():
            self.schedule_auto()
        elif self.auto_job is not None:
            self.root.after_cancel(self.auto_job)
            self.auto_job = None

    def schedule_auto(self):
        try:
            ms = max(1000, int(float(self.interval.get()) * 1000))
        except ValueError:
            ms = 60000
        self.do_grab()
        self.auto_job = self.root.after(ms, self.schedule_auto)

    def on_close(self):
        self.save_config()
        self.abort.set()
        self.auto.set(False)
        self.toggle_auto()
        self.an.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
