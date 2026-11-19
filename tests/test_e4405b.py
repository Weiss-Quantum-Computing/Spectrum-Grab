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
        }
        self.opc = False
        self.flat = False
        self.silent = set()            # queries that time out

    def write(self, cmd):
        self.sent.append(cmd)
        for part in cmd.split(";"):
            head, _, value = part.strip().partition(" ")
            if head == "*OPC":
                self.opc = True
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

print(f"\nAll {checks} checks passed.")
