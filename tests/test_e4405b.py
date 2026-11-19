"""Offline check of e4405b.py and esa_panel.py. No GPIB, no instrument.

A fake VISA session stands in for the E4405B underneath the REAL instrument
class, so the command ordering, the reply parsing and the status decoding are
exercised as shipped; and the panel is built for real, every widget, with Tk
withdrawn, so run_one, save_run and a segmented sweep run the way a bench
session drives them. Same conventions as test_grab.py: nothing opens a VISA
session and CONFIG_PATH points at a temp folder.

The replies the fake gives are the ones the real analyser gave on 18 Nov 2026
(s/n MY41440490, firmware A.09.02), spelling included.

    python tests/test_e4405b.py
"""
import glob
import json
import os
import sys
import tempfile
import time

import numpy as np
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import e4405b as E                                   # noqa: E402
import powersteps as P                               # noqa: E402
import spectrum_grab as sg                           # noqa: E402

TMP = tempfile.mkdtemp(prefix="esa_test_")
sg.CONFIG_PATH = os.path.join(TMP, "config.json")
import esa_panel                                     # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

checks = 0


def ok(label, condition, detail=""):
    global checks
    checks += 1
    print(f"[{checks}] {'OK ' if condition else 'FAIL'} {label}"
          + (f"   {detail}" if detail else ""))
    if not condition:
        raise AssertionError(label + " " + detail)


sg.App.do_connect = lambda self: None
esa_panel.EsaApp.do_connect = lambda self: None
sg.App.pump = lambda self: None


# ---------------------------------------------------------------- the fake

class FakeVisa:
    """A VISA session that answers like the E4405B did."""

    def __init__(self):
        self.timeout = 20000
        self.sent = []
        self.errq = []
        self.cond = {"STAT:QUES:INT:COND": 0, "STAT:QUES:INT:UNC:COND": 0,
                     "STAT:QUES:FREQ:COND": 0, "STAT:QUES:POW:COND": 0,
                     "STAT:QUES:CAL:COND": 16384}
        self.state = {
            "FREQ:STAR": "+0.00000000000E+000",
            "FREQ:STOP": "+1.32000000000E+010",
            "FREQ:CENT": "+6.60000000000E+009",
            "FREQ:SPAN": "+1.32000000000E+010",
            "SWE:POIN": "+401", "BAND": "+3.00000000E+006", "BAND:AUTO": "1",
            "BAND:VID": "+3.00000000E+006", "BAND:VID:AUTO": "1",
            "SWE:TIME": "+3.30000000E-002", "SWE:TIME:AUTO": "1",
            "INIT:CONT": "1", "DISP:WIND:TRAC:Y:RLEV": "+0.0E+00",
            "POW:ATT": "+1.000E+01", "POW:ATT:AUTO": "1", "UNIT:POW": "DBM",
            "DISP:WIND:TRAC:Y:SPAC": "LOG", "DISP:WIND:TRAC:Y:PDIV": "+1.000E+01",
            "INP:COUP": "AC", "DET": "POS", "DET:AUTO": "1", "AVER": "0",
            "AVER:COUN": "+100", "AVER:TYPE": "VID", "TRAC1:MODE": "WRIT",
            "TRIG:SOUR": "IMM", "TRIG:EXT:SLOP": "POS",
            "TRIG:OFFS": "+0.00000000E+000", "TRIG:OFFS:STAT": "0",
            "TRIG:VID:LEV": "-2.500E+01",
        }
        self.opc = False
        self.flat = False
        self.triggers = True           # False: armed, and no trigger comes
        self.trace_fn = None           # n -> the trace, when a test wants one
        self.silent = set()            # queries that time out

    def write(self, cmd):
        self.sent.append(cmd)
        for part in cmd.split(";"):
            head, _, value = part.strip().partition(" ")
            if head == "*OPC":
                self.opc = self.triggers
            elif head == "*CLS":
                self.errq, self.opc = [], False
            elif head in self.state and value:
                if head in ("FREQ:STAR", "FREQ:STOP"):
                    # The real analyser will not hold start above stop: it
                    # drags the other edge. That is what _ordered works round.
                    other = "FREQ:STOP" if head == "FREQ:STAR" else "FREQ:STAR"
                    v, o = float(value), float(self.state[other])
                    if (head == "FREQ:STAR" and v >= o) or \
                            (head == "FREQ:STOP" and v <= o):
                        self.state[other] = f"{v:E}"
                        self.dragged = True
                    self.state[head] = f"{v:E}"
                    lo = float(self.state["FREQ:STAR"])
                    hi = float(self.state["FREQ:STOP"])
                    self.state["FREQ:SPAN"] = f"{hi - lo:E}"
                    self.state["FREQ:CENT"] = f"{(hi + lo) / 2:E}"
                else:
                    self.state[head] = value

    def query(self, q):
        q = q.strip()
        if q in self.silent:
            raise TimeoutError("VI_ERROR_TMO")
        if q == "*ESR?":
            done, self.opc = self.opc, False
            return "+1\n" if done else "+0\n"
        if q == "SYST:ERR?":
            return (self.errq.pop(0) if self.errq else '+0,"No error"') + "\n"
        if q == "SYST:OPT?":
            return '"1DR,A4H,B72,1D5"\n'
        key = q.rstrip("?")
        if key in self.cond:
            return f"+{self.cond[key]}\n"
        return self.state[key] + "\n"

    def query_ascii_values(self, q):
        n = int(float(self.state["SWE:POIN"]))
        if self.flat:
            return [-120.0] * n
        if self.trace_fn is not None:
            return list(self.trace_fn(n))
        return list(-60.0 + np.random.default_rng(4405).normal(0, 1.0, n))

    def clear(self):
        pass

    def close(self):
        pass


def fake_analyzer():
    an = E.E4405B(connect=False)
    an.inst = FakeVisa()
    an.idn = "Hewlett-Packard, E4405B, MY41440490, A.09.02"
    an.addr = "GPIB0::18::INSTR"
    an.options = "1DR,A4H,B72,1D5"
    return an


# ------------------------------------------------------------- the library

ok("suffixes parse, M is mega",
   E.parse_hz("1.5M") == 1.5e6 and E.parse_hz("100 kHz") == 1e5
   and E.parse_hz("2.4GHz") == 2.4e9 and E.parse_hz("1e6") == 1e6)
ok("segments parse in every separator",
   E.parse_segments("0-3G, 3G:6.7G; 100k to 1M, 1e6-10e6")
   == [(0, 3e9), (3e9, 6.7e9), (1e5, 1e6), (1e6, 1e7)])
for bad in ("5G-1G", "0-20G", "banana", "1G"):
    try:
        E.parse_segments(bad)
        ok(f"'{bad}' is refused", False)
    except ValueError:
        ok(f"'{bad}' is refused", True)
tiles = E.tile_segments(0, 3e9, 3)
ok("tiles share their edges exactly",
   tiles == [(0, 1e9), (1e9, 2e9), (2e9, 3e9)])
ok("13.2 GHz is shown to the hertz, not as 1.32e+10",
   E.fmt_setting(E.BY_KEY["FSTOP"], "+1.32000000000E+010") == "13200000000")
ok("ON, 1 and +1 are one answer",
   all(E.fmt_setting(E.BY_KEY["AVG"], r) == "On" for r in ("1", "ON", "+1")))
ok("the legacy average-type replies are read",
   E.fmt_setting(E.BY_KEY["AVGTYPE"], "LPOW") == "Video (log)"
   and E.fmt_setting(E.BY_KEY["AVGTYPE"], "POW") == "RMS (power)")
ok("an unknown reply is shown as it arrived",
   E.fmt_setting(E.BY_KEY["DET"], "QPE") == "QPE")
ok("panel text goes back to tokens and plain numbers",
   E.parse_setting(E.BY_KEY["DET"], "Sample") == "SAMP"
   and E.parse_setting(E.BY_KEY["FSTOP"], "1.5G") == "1500000000")

an = fake_analyzer()
snap = an.read_all_settings(retry_all=True)
ok("every setting reads back", len(snap) == len(E.ALL_SETTINGS))
ok("units and axis follow UNIT:POW",
   E.trace_units(snap) == "dBm" and E.trace_yscale(snap) == "linear"
   and E.trace_yscale({"YUNIT": "V"}) == "log")
ok("an unread unit is flagged as assumed", bool(E.readout_fault({})))

an.inst.sent.clear()
an.write_settings({"RBW": "1000", "RBWAUTO": "1"})
sent = [c for c in an.inst.sent if c.startswith("BAND")]
ok("an auto switch goes before the value beside it",
   sent == ["BAND:AUTO 1", "BAND 1000"], str(sent))

# Moving a band up past the present stop: the stop has to go first, or the
# analyser drags it and the start lands somewhere else.
an.inst.dragged = False
an.set_band(2e9, 3e9)
an.set_band(1e6, 2e6)
ok("start and stop never cross, upward or downward",
   not an.inst.dragged
   and float(an.inst.state["FREQ:STAR"]) == 1e6
   and float(an.inst.state["FREQ:STOP"]) == 2e6)

an.inst.errq = ['-222,"Data out of range"']
orig_write = an.inst.write
an.inst.write = lambda cmd: None if cmd == "*CLS" else orig_write(cmd)
_sent, errs = an.write_settings({"NAVG": "99999"})
an.inst.write = orig_write
ok("what the analyser says about a write is handed back",
   errs == ['-222,"Data out of range"'])

an.inst.sent.clear()
an.start()
ok("start is CLS, single sweep, then INIT and OPC as one message",
   an.inst.sent == ["*CLS", "INIT:CONT OFF", "INIT:IMM;*OPC"],
   str(an.inst.sent))
ok("wait_done sees the completion bit", an.wait_done(2) == "done")
ok("with nothing triggered it times out rather than hanging",
   an.wait_done(0.3, poll=0.05) == "timeout")
ok("Stop is felt", an.wait_done(5, stop=lambda: True) == "stopped")
an.inst.sent.clear()
ok("continuous sweep is put back, once",
   an.resume() is True and an.inst.sent == ["INIT:CONT ON"]
   and an.resume() is False)

an.inst.sent.clear()
f, a = an.trace()
ok("the trace is asked for in ASCII",
   an.inst.sent[0] == "FORM ASC" and len(f) == len(a) == 401
   and f[0] == 1e6 and f[-1] == 2e6)

st = an.refresh_status()
ok("Align Needed is a caution, not a fault",
   st.faults() == [] and "ALIGN NEEDED" in st.alignment()
   and st.describe() == "clean")
an.inst.cond["STAT:QUES:INT:UNC:COND"] = 1
an.inst.cond["STAT:QUES:INT:COND"] = 8 | 16
st = an.refresh_status()
ok("Meas Uncal and IF overload are faults",
   st.oversweep and st.overloaded and len(st.faults()) == 2, st.describe())
an.inst.cond["STAT:QUES:INT:UNC:COND"] = 0
an.inst.cond["STAT:QUES:INT:COND"] = 0
an.inst.silent.add("STAT:QUES:FREQ:COND?")
st = an.refresh_status()
ok("a register that did not answer is not a clean one",
   not st.complete and any("unverified" in x for x in st.faults()))
an.inst.silent.clear()

ok("View and Blank are not measurements",
   bool(E.sweep_fault({"TRMODE": "VIEW"}))
   and bool(E.sweep_fault({"TRMODE": "BLAN"}))
   and not E.sweep_fault({"TRMODE": "WRIT"}))
ok("a flat trace is flagged", bool(E.flat_fault([-120.0] * 401))
   and not E.flat_fault(a))
ok("a band the analyser moved is flagged",
   bool(E.band_fault(np.array([0.0, 13.2e9]), (0.0, 20e9)))
   and not E.band_fault(np.array([1e6, 2e6]), (1e6, 2e6)))
ok("Sample with points wider than the RBW is called out",
   "missed" in E.resolution_note({"FSPAN": "1e9", "POINTS": "401",
                                  "RBW": "1e3", "DET": "SAMP"})
   and "missed" not in E.resolution_note({"FSPAN": "1e9", "POINTS": "401",
                                          "RBW": "1e3", "DET": "POS"}))
ok("one trigger takes the average count of sweeps",
   E.sweep_count({"AVG": "1", "NAVG": "+50"}) == 50
   and E.sweep_count({"AVG": "0", "NAVG": "+50"}) == 1)


# --------------------------------------------------------------- the panel

def build(outdir, **cfg):
    base = {"outdir": outdir, "title": "esa test", "dated": False,
            "pause_cases": False}
    base.update(cfg)
    with open(os.path.join(TMP, "config_e4405b.json"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps(base))
    root = tk.Tk()
    root.withdraw()
    app = esa_panel.EsaApp(root)
    app.an = fake_analyzer()
    return root, app


def said(app):
    lines = []
    while not app.msgs.empty():
        lines.append(app.msgs.get())
    return lines


out = os.path.join(TMP, "one")
root, app = build(out)
ok("the selector offers both analysers and this panel is the E4405B's",
   app.instrument.get() == "E4405B" and sg.INSTRUMENTS == ("SR760", "E4405B")
   and sg.panel_class("E4405B") is esa_panel.EsaApp
   and sg.panel_class("SR760") is sg.App)
ok("it keeps its own config file",
   app.config_path().endswith("config_e4405b.json")
   and app.outdir.get() == out)
ok("the grid has every E4405B setting and none of the SR760's",
   set(app.set_vars) == set(E.BY_KEY))

app._grab_runs([""], [None], [None])
root.update()
log = said(app)
files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(out, "*")))
stamp = time.strftime("%Y%m%d")
ok("one grab writes csv, png and txt under one name",
   files == [f"esa test_0Hz-13.2GHz_{stamp}{e}"
             for e in (".csv", ".png", ".txt")],
   str(files))
text = open(os.path.join(out, files[2]), encoding="utf-8").read()
ok("the metadata carries status, quality, alignment and the commands",
   "trace quality        : clean" in text and "ALIGN NEEDED" in text
   and "(FREQ:STOP 13200000000)" in text and "options" in text)
ok("continuous sweep is put back after the capture",
   app.an.inst.sent.count("INIT:CONT ON") == 1
   and any("continuous sweep back on" in line for line in log))
fr, am, unit = E.read_csv(os.path.join(out, files[0]))
ok("the CSV reads back with its units", unit == "dBm" and len(fr) == 401)
root.update()
root.destroy()

out = os.path.join(TMP, "suspect")
root, app = build(out, resume_cont=False)
app.an.inst.cond["STAT:QUES:INT:UNC:COND"] = 1
app.an.inst.cond["STAT:QUES:INT:COND"] = 8
app.an.inst.flat = True
freqs, amps, snap, notes = app.run_one()
ok("an oversweep and a flat trace both reach the verdict",
   notes["trace quality"].startswith("SUSPECT")
   and "Meas Uncal" in notes["trace quality"]
   and "flat" in notes["trace quality"])
ok("the plot's notes line carries the warning",
   "SUSPECT" in app.plot_subtitle(snap, freqs, notes))
ok("left in single sweep when asked", "INIT:CONT ON" not in app.an.inst.sent)
root.update()
root.destroy()

out = os.path.join(TMP, "sweep")
root, app = build(out)
app.an.set_band(88e6, 108e6)
app.segs_txt.set("0-1G, 1G-2G, 2G-3G")
app.cases_txt.set("dark, light")
cases, segments = app.sweep_lists()
app._grab_runs(cases, segments, None)
root.update()
log = said(app)
names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(out, "*")))
want = sorted(f"esa test_sweep_{stamp}{s}" for s in
              ("_dark.csv", "_light.csv", ".txt", ".png", "_freqs.npy",
               "_amps.npy", "_axes.json"))
ok("a sweep writes one set of files, a CSV per case", names == want,
   str(names))
rows = np.loadtxt(os.path.join(out, f"esa test_sweep_{stamp}_dark.csv"),
                  delimiter=",", skiprows=1)
ok("the segments join into one sorted curve, numbered",
   rows.shape == (1203, 3) and rows[0, 0] == 0 and rows[-1, 0] == 3e9
   and np.all(np.diff(rows[:, 0]) >= 0) and set(rows[:, 2]) == {1, 2, 3})
m = np.load(os.path.join(out, f"esa test_sweep_{stamp}_amps.npy"))
ok("the matrices are [case][segment][point] and full",
   m.shape == (2, 3, 401) and not np.isnan(m).any())
ok("the analyser is put back on the band it was found on",
   float(app.an.inst.state["FREQ:STAR"]) == 88e6
   and float(app.an.inst.state["FREQ:STOP"]) == 108e6
   and any("analyser back on 88 MHz to 108 MHz" in line for line in log))
text = open(os.path.join(out, f"esa test_sweep_{stamp}.txt"),
            encoding="utf-8").read()
ok("the sweep describes its segments once",
   text.count("analyzer settings") == 1 and "(case 'light')" in text
   and "2 case(s) x 3 segment(s) = 6 runs" in text)

app.abort.set()
app.segs_txt.set("0-1G, 1G-2G")
app.cases_txt.set("")
app.keep_segments.set(False)
out2 = os.path.join(TMP, "stopped")
app.outdir.set(out2)
app._grab_runs([""], app.sweep_lists()[1], None)
ok("a stopped sweep still restores the analyser and writes nothing empty",
   float(app.an.inst.state["FREQ:STAR"]) == 88e6
   and not glob.glob(os.path.join(out2, "*")))
app.abort.clear()

app.tile_start.set("0")
app.tile_stop.set("3G")
app.tile_n.set("3")
app.do_tile()
ok("Fill segments writes what the sweep box parses back",
   E.parse_segments(app.segs_txt.get()) == tiles)
app.segs_txt.set("5G-1G")
ok("a bad segment list is refused before anything is sent",
   app.sweep_lists() is None)
ok("blank y limits mean autoscale",
   app.y_window("dBm") == (None, None))
app.ymin.set("-100")
app.ymax.set("0")
ok("typed y limits are used for dB traces only",
   app.y_window("dBm") == (-100.0, 0.0) and app.y_window("V") == (None, None))
root.update()
root.destroy()


# ------------------------------------------------ zero span and power steps

ok("the trigger is in the grid, slope under its real header",
   E.BY_KEY["TRIGSLOPE"].scpi == "TRIG:EXT:SLOP"
   and E.parse_setting(E.BY_KEY["TRIGSRC"], "External") == "EXT")
pvt = E.power_vs_time(9.19263177e9, 0.1, 1e6, 401)
ok("power against time is zero span, sample detector, VBW not below RBW",
   pvt["FSPAN"] == "0" and pvt["DET"] == "SAMP" and pvt["VBW"] == pvt["RBW"]
   and list(pvt)[:2] == ["FCENT", "FSPAN"] and "TRIGSRC" not in pvt)
ok("the shortest sweep is 10 us a point and never under 4 ms",
   E.min_zero_span_sweep(401) == 4e-3
   and abs(E.min_zero_span_sweep(8192) - 81.91e-3) < 1e-12)
try:
    E.power_vs_time(9.19e9, 1e-3)
    ok("a sweep too short for this unit is refused", False)
except ValueError:
    ok("a sweep too short for this unit is refused", True)

an = fake_analyzer()
an.inst.state["FREQ:SPAN"] = "+0.00000000000E+000"
an.inst.sent.clear()
an.write_settings({"RBWAUTO": "1", "SWTAUTO": "1", "ATTAUTO": "1",
                   "FSPAN": "1e9"})
sent = [c for c in an.inst.sent if c != "*CLS" and not c.endswith("?")]
ok("leaving zero span, the RBW and sweep-time autos go after the span",
   sent == ["POW:ATT:AUTO 1", "FREQ:SPAN 1000000000", "BAND:AUTO 1",
            "SWE:TIME:AUTO 1"], str(sent))
zs = {"FSPAN": "0", "FSTART": "9e9", "FSTOP": "9e9", "SWT": "0.1",
      "POINTS": "401", "RBW": "1e6", "YUNIT": "DBM", "ATT": "10",
      "RLEV": "10"}
ok("a zero-span point is described in time",
   "us a point" in E.resolution_note(zs)
   and "settle" in E.resolution_note(zs))
ok("a peak that overdrives the analyser's own mixer is a fault",
   "mixer" in E.level_fault(zs, [5.0, -40.0])
   and not E.level_fault(dict(zs, ATT="30"), [5.0, -40.0])
   and "reference level" in E.level_fault(dict(zs, ATT="40", RLEV="0"),
                                          [5.0]))
an = fake_analyzer()
an.inst.state.update({"FREQ:SPAN": "0", "FREQ:STAR": "9.0E+009",
                      "FREQ:STOP": "9.0E+009", "SWE:TIME": "1.0E-001",
                      "TRIG:OFFS": "-2.0E-003", "TRIG:OFFS:STAT": "1"})
tt, _ = an.trace()
ok("the time axis starts at the trigger offset",
   abs(tt[0] + 2e-3) < 1e-12 and abs(tt[-1] - 0.098) < 1e-12)
ok("free run waits for nothing, external is named",
   E.waits_for_trigger({"TRIGSRC": "IMM"}) == ""
   and E.waits_for_trigger({"TRIGSRC": "EXT"}) == "External")

# A staircase as the chain would play it: eight words, 5 ms steps 10 ms
# apart from 4 ms, a floor at -70 dBm, and a top that compresses.
WORDS = [102, 204, 409, 819, 1577, 2457, 3276, 4095]
SAT_MW = 10.0 ** (8.0 / 10.0)


def chain_dbm(word, linear=False):
    p = 10.0 ** (10.0 / 10.0) * (word / 4095.0) ** 2      # +10 dBm full scale
    return 10 * np.log10(p if linear else p / (1 + p / SAT_MW))


def staircase(n, words=WORDS, noise=0.05, seed=1, linear=False):
    t = np.linspace(0.0, 0.1, n)
    y = np.full(n, -70.0)
    for i, w in enumerate(words):
        on = (t >= 4e-3 + i * 10e-3) & (t < 9e-3 + i * 10e-3)
        y[on] = chain_dbm(w, linear)
    return t, y + np.random.default_rng(seed).normal(0, noise, n)


t, y = staircase(401)
steps = P.step_levels(t, y, 4e-3, 5e-3, 10e-3, 8)
true = np.array([chain_dbm(w) for w in WORDS])
ok("typed timing reads every step to a few hundredths of a dB",
   np.max(np.abs(np.array([s.dbm for s in steps]) - true)) < 0.05
   and all(s.points >= 10 for s in steps))
bursts, floor = P.find_bursts(t, y)
ok("the same steps are found as bursts, with no timing",
   len(bursts) == 8 and abs(floor + 70) < 0.3
   and np.max(np.abs(np.array([s.dbm for s in bursts]) - true)) < 0.05)
two = P._level(0, np.arange(4.0), np.array([-20.0, -30.0, -20.0, -30.0]),
               0.0, 3.0)
ok("a level is the mean of the power, not of the decibels",
   abs(two.dbm - 10 * np.log10((0.01 + 0.001) / 2)) < 1e-9
   and abs(two.dbm + 25) > 2)
late = P.step_levels(t, y, 4e-3, 5e-3, 10e-3, 12)
ok("a step past the end of the sweep is kept, and empty",
   len(late) == 12 and late[-1].points == 0 and np.isnan(late[-1].dbm))
rel, law, usable = P.response(WORDS, [s.dbm for s in steps],
                              P.floor_between(t, y, 4e-3, 5e-3, 10e-3, 8))
ok("against the word-squared law the low steps sit on it and the top "
   "falls away",
   abs(rel[1]) < 0.1 and rel[-1] < -3 and all(np.diff(rel[3:]) < 0)
   and usable.all(), str(np.round(rel, 2)))
_, lin = staircase(401, linear=True)
lin_steps = P.step_levels(t, lin, 4e-3, 5e-3, 10e-3, 8)
points = P.amplitude_table(WORDS, [s.dbm for s in lin_steps])
ok("a linear chain's table is power proportional to the word squared, "
   "1 W at the anchor",
   points[0] == (0.0, 0.0) and len(points) == 9
   and all(abs(pw / (f * 4095 / P.ANCHOR_WORD) ** 2 - 1) < 0.02
           for pw, f in points[1:]),
   str(points[5]))
points = P.amplitude_table(WORDS, [s.dbm for s in steps], 1.0, 1577)
ok("a compressing chain needs more than four times the word... "
   "its table tops out below the linear chain's",
   abs(points[5][0] - 1.0) < 1e-9 and points[-1][0] < (4095 / 1577) ** 2 * 0.8)
ok("the table file reads back as the compiler's format",
   P.read_table(P.table_text(points, ["a comment", ""])) ==
   [(float(f"{a:.10g}"), float(f"{b:.10g}")) for a, b in points])
for label, words, levels, kw in (
        ("a power that does not rise", [100, 200, 300], [-30, -20, -21], {}),
        ("a word played twice", [100, 100, 300], [-30, -29, -20], {}),
        ("an anchor outside the words played", [100, 200], [-30, -24], {}),
        ("a single step", [1577], [-10], {})):
    try:
        P.amplitude_table(words, levels, **kw)
        ok(f"{label} is refused", False)
    except ValueError:
        ok(f"{label} is refused", True)
for bad in ("204, x", "0", "5000"):
    try:
        P.parse_words(bad)
        ok(f"words '{bad}' are refused", False)
    except ValueError:
        ok(f"words '{bad}' are refused", True)
ok("words parse with commas or spaces", P.parse_words("204, 409 614") ==
   [204, 409, 614] and P.parse_words("") == [])


def zero_span_app(out, **cfg):
    root, app = build(out, **cfg)
    app.an.inst.state.update({"FREQ:SPAN": "0", "FREQ:CENT": "9.19263177E+009",
                              "FREQ:STAR": "9.19263177E+009",
                              "FREQ:STOP": "9.19263177E+009",
                              "SWE:TIME": "1.0E-001", "DET": "SAMP",
                              "BAND": "1.0E+006", "BAND:VID": "1.0E+006",
                              "POW:ATT": "+3.000E+01",
                              "DISP:WIND:TRAC:Y:RLEV": "+2.0E+01",
                              "TRIG:SOUR": "EXT"})
    return root, app


out = os.path.join(TMP, "zerospan")
root, app = zero_span_app(out)
high = root.winfo_reqheight()
ok("the panel fits a 1080-line screen", high <= 1000, f"{high} px")
app.an.inst.trace_fn = lambda n: staircase(n)[1]
app._grab_runs([""], [None], [None])
root.update()
log = said(app)
names = sorted(os.path.basename(f) for f in glob.glob(os.path.join(out, "*")))
ok("a zero-span grab is named for it and its CSV is headed in seconds",
   names[0] == f"esa test_zerospan_9.19263GHz_{stamp}.csv"
   and open(os.path.join(out, names[0])).readline().strip()
   == "Time (s),dBm", str(names))
ok("the log says the sweep waits for its trigger",
   any("waiting for the external trigger" in line for line in log))
ok("the panel's plot is labelled in time",
   getattr(app.last_plot[0], "xlabel", "").startswith("Time"))

app.open_steps()
dlg = app.steps_dialog
dlg.win.withdraw()
v = app.steps
v["first"].set("4e-3")
v["length"].set("5e-3")
v["period"].set("10e-3")
v["count"].set("8")
v["words"].set(", ".join(str(w) for w in WORDS))
v["table"].set(True)
out = os.path.join(TMP, "steps")
app.outdir.set(out)
dlg._capture_worker(dlg.params())
root.update()
log = said(app)
base = os.path.join(out, f"esa test_steps_9.19263GHz_{stamp}")
ok("a staircase capture writes levels, traces, plot, notes and the table",
   all(os.path.exists(base + e) for e in
       (".csv", "_traces.csv", ".png", ".txt", "_amplitude_table.txt")),
   str(os.listdir(out)))
rows = np.genfromtxt(base + ".csv", delimiter=",", skip_header=1)
ok("one row a step, with its word and level",
   rows.shape[0] == 8 and list(rows[:, 2]) == WORDS
   and np.max(np.abs(rows[:, 7] - true)) < 0.05)
table = P.read_table(open(base + "_amplitude_table.txt",
                          encoding="utf-8").read())
ok("the table on disk has 1 W at word 1577 and says how it was measured",
   abs(dict((round(f * 4095), pw) for pw, f in table)[1577] - 1) < 1e-6
   and "MEASURED" in open(base + "_amplitude_table.txt").read()
   and "pi" in open(base + "_amplitude_table.txt").read())
text = open(base + ".txt", encoding="utf-8").read()
ok("the notes carry the method, the trigger settings and every step",
   "middle 60%" in text and "(TRIG:SOUR EXT)" in text
   and text.count("word ") >= 8)
tt, traces = P.read_traces(base + "_traces.csv")
ok("the traces file reads back", len(traces) == 1 and len(tt) == 401)

# A power scan: one level a trigger, the steps counted across captures.
count = {"n": 0}


def one_pulse(n):
    word = WORDS[count["n"] % len(WORDS)]
    count["n"] += 1
    t1 = np.linspace(0.0, 0.1, n)
    y1 = np.full(n, -70.0)
    y1[(t1 >= 20e-3) & (t1 < 60e-3)] = chain_dbm(word)
    return y1 + np.random.default_rng(count["n"]).normal(0, 0.05, n)


app.an.inst.trace_fn = one_pulse
v["length"].set("")
v["period"].set("")
v["count"].set("1")
v["captures"].set("8")
out = os.path.join(TMP, "series")
app.outdir.set(out)
dlg._capture_worker(dlg.params())
root.update()
said(app)
base = os.path.join(out, f"esa test_steps_9.19263GHz_{stamp}")
rows = np.genfromtxt(base + ".csv", delimiter=",", skip_header=1)
ok("eight triggers of one burst each give the same eight levels",
   rows.shape[0] == 8 and list(rows[:, 1]) == list(range(1, 9))
   and np.max(np.abs(rows[:, 7] - true)) < 0.05
   and len(P.read_traces(base + "_traces.csv")[1]) == 8)

app.an.inst.triggers = False
app.timeout_s.set("0.3")
v["captures"].set("1")
out = os.path.join(TMP, "notrigger")
app.outdir.set(out)
app.an.inst.sent.clear()
dlg._capture_worker(dlg.params())
root.update()
log = said(app)
ok("no trigger: the sweep is aborted, said so, and nothing is written",
   "ABOR" in app.an.inst.sent and not glob.glob(os.path.join(out, "*"))
   and any("no external trigger arrived" in line for line in log),
   str(log[-3:]))
app.an.inst.triggers = True
app.an.inst.state["FREQ:SPAN"] = "1.0E+009"
dlg._capture_worker(dlg.params())
root.update()
ok("outside zero span it refuses rather than read a spectrum as steps",
   any("not in zero span" in line for line in said(app))
   and not glob.glob(os.path.join(out, "*")))
v["words"].set("")
try:
    dlg.params()
    ok("a table without words is refused before anything is armed", False)
except ValueError:
    ok("a table without words is refused before anything is armed", True)
app.save_config()
cfg = json.load(open(app.config_path(), encoding="utf-8"))
ok("the window's boxes are kept in the panel's config",
   cfg.get("steps_count") == "1" and cfg.get("steps_table") is True)
dlg.close()
ok("closing the window leaves the panel working",
   app.steps_dialog is None)
app.set_busy(False)
root.update()
root.destroy()

print(f"\nAll {checks} checks passed.")
