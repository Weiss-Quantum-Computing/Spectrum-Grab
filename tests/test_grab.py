"""Offline check of spectrum_grab.py and sr760.py. No GPIB, no instrument.

A fake analyzer stands in for the SR760 and the panel is built for real - every
widget, with Tk withdrawn - so run_one, save_run, _grab_worker and the settings
panel are exercised the way a bench session drives them.

Nothing here opens a VISA session or touches the real config. App.do_connect is
neutralised before the first panel is built, because __init__ schedules an
auto-connect 300 ms after startup, and CONFIG_PATH is pointed at a temp file so
a test run cannot rewrite what the last bench session was using.

Almost all of these are regressions, and each one names the wrong answer it
used to give. They are worth keeping precisely because none of the failures
looked like failures: a mislabelled trace, an error bar five times too good, a
clean-looking overload line. The analyzer never complains, so the test has to.

    python tests/test_grab.py
"""
import datetime
import glob
import json
import os
import shutil
import sys
import tempfile
import time

import numpy as np
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sr760 as S                                    # noqa: E402
import spectrum_grab as sg                           # noqa: E402
from spectrum_grab import load_sequences             # noqa: E402
from sr760 import trace_units                        # noqa: E402

# Units are drawn as "Vpk/√Hz", and a Windows console is cp1252, which cannot
# encode it - so printing a check's detail would fail on the character rather
# than on the check. The panel itself never meets this: it logs into a Tk
# widget, and under pythonw there is no stdout at all.
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


# The panel must never reach for the bus from a test. __init__ queues a connect
# for 300 ms after startup, which would otherwise fire in the middle of whatever
# test happened to be running by then.
sg.App.do_connect = lambda self: None

# pump() drains the log queue into the Text widget and reschedules itself every
# 100 ms. A test builds and destroys a panel a dozen times, and each destroy
# leaves that timer pointing at an interpreter that is going away, which Tk
# reports as a page of "invalid command name ...pump". The tests read the queue
# directly instead, so the timer has no job here.
sg.App.pump = lambda self: None

TMP = tempfile.mkdtemp(prefix="spectrum_grab_test_")
sg.CONFIG_PATH = os.path.join(TMP, "config.json")


# ---------------------------------------------------------------- the fake

class FakeAn:
    """Just enough SR760 for run_one and save_run to go all the way through."""

    def __init__(self, snap, errs=0, ffts=0):
        self.inst = object()
        self.idn = "Stanford_Research_Systems,SR760,s/n1234,v1.0"
        self.addr = "GPIB0::10::INSTR"
        self.snap = dict(snap)
        self.errs, self.ffts = errs, ffts
        self.qform = {}
        self.sent = []
        self._status = None
        self.last_raw = {}

    def command(self, key, value):
        return S.BY_KEY[key].writes[self.qform.get(key, 0)].format(v=value)

    def query_for(self, key):
        return S.BY_KEY[key].queries[self.qform.get(key, 0)]

    def put(self, cmd):
        self.sent.append(cmd)

    def get(self, query):
        return "0"

    def recover(self):
        pass

    # The real settings-write path, so the SPAN/OVLP ordering the sweep
    # depends on is exercised as shipped rather than reimplemented here.
    # read_settings is FakeAn's own, below - it answers from the snapshot.
    write_settings = S.SR760.write_settings
    _remember = S.SR760._remember

    def close(self):
        self.inst = None

    def lock_panel(self, locked):
        self.sent.append("OVRM %d" % (0 if locked else 1))

    def autoscale(self):
        self.sent.append("AUTS 0")

    def pin_range(self, dbv):
        self.put(self.command("ARNG", 0))
        self.put(self.command("IRNG", f"{dbv:g}"))

    def input_range(self):
        return float(self.snap.get("IRNG", "-30"))

    def start(self):
        self._status = None
        self.sent.append("STRT")

    def wait_done(self, timeout, stop=None, poll=0.25):
        return "done"

    def dwell(self, seconds, stop=None, poll=0.25):
        return "done"

    def settle(self, recs, span_code, stop=None, log=None):
        return {"settle": f"{recs:g} record lengths"}

    def average_finishes(self):
        if S.code_of(self.snap, "AVGO", 0) != 1:
            return False, "no averaging"
        return True, "100 linear averages"

    def refresh_status(self, log=None):
        self._status = S.Status(self.errs, self.ffts, time.perf_counter())
        return self._status

    def status(self, refresh_if_stale=True):
        if self._status is None and refresh_if_stale:
            self.refresh_status()
        return self._status

    def trace_binary(self, trace, n_bins, in_db=True):
        return (np.linspace(0.0, 390.0, n_bins),
                np.full(n_bins, -120.0 if in_db else 1e-6))

    def trace_ascii(self, trace, n_bins, progress=None):
        return np.linspace(0.0, 390.0, n_bins), np.full(n_bins, 1e-6)

    def read_settings(self, *keys):
        return {k: self.snap[k] for k in keys if k in self.snap}

    def read_all_settings(self, retry_all=False, log=None):
        return dict(self.snap)


# A healthy analyzer: PSD, LogMag, dBV, 100 linear RMS averages, range pinned.
GOOD = {"SPAN": "11", "STRF": "0", "WNDO": "2", "MEAS0": "1", "DISP0": "0",
        "UNIT0": "2", "ISRC": "0", "ICPL": "0", "IRNG": "-30", "ARNG": "0",
        "AVGO": "1", "NAVG": "100", "AVGT": "0", "AVGM": "0", "OVLP": "0"}


def build_app(snap=None, errs=0, ffts=0, outdir=None):
    """A real App with every widget, the fake analyzer swapped in, and its
    output pointed somewhere disposable."""
    outdir = outdir or TMP
    with open(sg.CONFIG_PATH, "w", encoding="utf-8") as fh:
        # Written rather than set afterwards so that load_latest_preview, which
        # __init__ calls, looks in the temp folder and not in the real one.
        # json.dumps, not %r: a Python repr of a Windows path is not JSON, and
        # load_config would quietly reject the file and leave the default
        # output folder - the user's real one - in place.
        json.dump({"outdir": outdir, "dated": False, "title": "sr760"}, fh)
    root = tk.Tk()
    root.withdraw()
    # __init__ schedules a pump for 100 ms and an auto-connect for 300 ms. Every
    # test finishes and destroys the root long before either is due, and a timer
    # left pointing at a dead interpreter is what Tk reports as "invalid command
    # name". So collect what construction schedules and cancel it afterwards -
    # neither is wanted here, and neither can be cancelled without its id.
    scheduled, real_after = [], root.after

    def remember(ms, func=None, *args):
        tid = real_after(ms) if func is None else real_after(ms, func, *args)
        scheduled.append(tid)
        return tid

    root.after = remember
    try:
        app = sg.App(root)
    finally:
        root.after = real_after
    for tid in scheduled:
        try:
            root.after_cancel(tid)
        except Exception:
            pass
    app.an = FakeAn(GOOD if snap is None else snap, errs, ffts)
    root.update()
    return root, app


def logged(app):
    """Everything the app has logged, read straight from the queue - pump() is
    a no-op here, so the Text widget stays empty."""
    return "\n".join(list(app.msgs.queue))


def clear_log(app):
    app.msgs.queue.clear()


# ------------------------------------------------ 1. reading a reply back

print("\n--- enum replies ---")

w = S.BY_KEY["WNDO"]
ok("a listed code resolves", S.fmt_setting(w, "2") == "Hanning")
# Was: choices[-1], so a reply of -1 displayed as BMH - and went into the
# metadata file as BMH - because a negative index wraps instead of raising.
ok("a negative code is shown as it arrived, not wrapped",
   S.fmt_setting(w, "-1") == "-1", S.fmt_setting(w, "-1"))
ok("a negative code deep enough to wrap twice is still raw",
   S.fmt_setting(w, "-4") == "-4")
ok("an out-of-range positive code is still raw", S.fmt_setting(w, "9") == "9")
ok("a non-numeric reply is still raw", S.fmt_setting(w, "what") == "what")


print("\n--- sweep lists ---")

ok("a comma list parses", S.parse_list("0, 10, 20") == [0.0, 10.0, 20.0])
ok("a range parses", len(S.parse_list("0:100:10")) == 11)
ok("an empty box means leave it alone", S.parse_list("  ") == [])
try:
    S.parse_list("0:1e9:1")
    ok("an endless range is refused", False)
except ValueError as exc:
    ok("an endless range is refused", "more than" in str(exc))
# Was: only the start:stop:step form was capped, so a pasted column of numbers
# went through unbounded and straight into the sweep matrix allocation.
try:
    S.parse_list(",".join(str(i) for i in range(S.MAX_LIST_ITEMS + 1)))
    ok("an over-long comma list is refused too", False)
except ValueError as exc:
    ok("an over-long comma list is refused too", "values" in str(exc),
       str(exc)[:52])


# ------------------------------------------------------ 2. the status byte

print("\n--- the overload flags ---")

both = S.Status(0, 0, 0.0)
half = S.Status(0, None, 0.0)
neither = S.Status(None, None, 0.0)
half_over = S.Status(None, 32, 0.0)

ok("both bytes clear reads as no overload", both.describe() == "no")
ok("both bytes answered is complete", both.complete)
# Was: read is true if EITHER byte answered, so describe() said "no" - a
# verified-clean answer - on a status where half the evidence timed out.
ok("one byte missing is not complete", not half.complete)
ok("one byte missing does not report as clean", half.describe() != "no",
   half.describe())
ok("... it says which byte went quiet", "FFTS" in half.describe())
# Was: 'set' if bit else 'clear', and errs_bit returns None when ERRS never
# answered - so the metadata asserted a bit was clear that was never read.
ok("a bit that was never read prints as unread, not clear",
   "unread" in half_over.describe() and "clear" not in half_over.describe(),
   half_over.describe())
ok("neither byte is still unread", neither.describe() == "unread")
ok("an overload on the byte that did answer is still caught",
   half_over.overloaded)


# ------------------------------------------- 3. the scale a trace is on

print("\n--- the readout scale ---")

live = {"MEAS0": "1", "DISP0": "0", "UNIT0": "1"}
dropped = {"MEAS0": "1"}                       # UNIT0 and DISP0 went dead
linear = {"DISP0": "1", "UNIT0": "1"}

ok("the binary dump is allowed on a known LogMag display",
   S.binary_valid(live))
ok("the binary dump is refused on a linear display", not S.binary_valid(linear))
# Was: code_of(snap, "DISP0", 0) - a missing DISP0 defaulted to LogMag and a
# missing UNIT0 to dBV, so the dump was rebased the wrong way round and a volt
# trace came out about 160 dB adrift with nothing saying so.
ok("the binary dump is refused when the display was never read",
   not S.binary_valid(dropped))
ok("... and when only the units were never read",
   not S.binary_valid({"DISP0": "0"}))
ok("a dropped setting is called out by name",
   "Units" in S.readout_fault(dropped) and "Display" in S.readout_fault(dropped),
   S.readout_fault(dropped)[:58])
ok("nothing is said when both were read", S.readout_fault(live) == "")
ok("the refusal explains the unknown case",
   "read back" in S.binary_refusal(dropped))
ok("the refusal still explains the linear case",
   "linear display" in S.binary_refusal(linear))
ok("no refusal when the dump is usable", S.binary_refusal(live) == "")


# ------------------------------------------ 4. what a run is actually worth

print("\n--- the error bar ---")

on = S.record_stats(11, 30.0, navg=100, ovlp=0, averaged=True)
off = S.record_stats(11, 30.0, navg=100, ovlp=0, averaged=False)

ok("an averaged run counts elapsed/T_rec", round(on["n_indep"]) == 29,
   f"n_indep {on['n_indep']:.1f}, 1 sigma {on['rel_err']:.3g}")
# Was: the same elapsed/T_rec count with averaging switched off, which claimed
# 29 records and a 0.19 bar for a trace that is one record and a 1.0 bar. The
# analyzer keeps acquiring with AVGO off; it just throws each record away.
ok("an unaveraged run is worth one record", off["n_indep"] == 1.0)
ok("... so its 1 sigma is 1, not 0.19", off["rel_err"] == 1.0,
   f"the old model said {on['rel_err']:.3g}")
ok("the NAVG ratio is dropped when nothing was averaged",
   not np.isfinite(off["navg_over_indep"]))
notes = S.stats_notes(off)
ok("the metadata says the averaging was off",
   notes.get("averaging", "").startswith("OFF"))
ok("... and that NAVG is not in force",
   "not in force" in notes["averages reported (NAVG)"],
   notes["averages reported (NAVG)"])
# run_protocol calls record_stats without this argument, so the default has to
# go on meaning what it always did.
ok("the default is the old behaviour",
   S.record_stats(11, 30.0, navg=100, ovlp=0)["n_indep"] == on["n_indep"])

print("\n--- the averaging itself ---")

ok("a linear RMS average is not a fault", S.averaging_fault(GOOD) == "")
ok("vector averaging is", "vector" in S.averaging_fault(dict(GOOD, AVGT="1")))
ok("exponential averaging is",
   "exponential" in S.averaging_fault(dict(GOOD, AVGM="1")))
ok("averaging switched off is a separate question",
   S.averaging_fault(dict(GOOD, AVGO="0")) == "")
# Was: code_of(snap, "AVGO", 0) treated a missing AVGO as off, silently, and
# record_stats then had no way to know what it was working out a bar for.
ok("an AVGO that could not be read is a fault of its own",
   "could not be read" in S.averaging_fault({k: v for k, v in GOOD.items()
                                             if k != "AVGO"}))


# ------------------------------------------------- 5. the command spelling

print("\n--- writes follow the spelling the analyzer answered ---")

an = S.SR760(connect=False)
fake = FakeAn(GOOD)
an.put = fake.put
an.get = lambda q: "-30"
ok("the plain spelling is the default", an.command("ARNG", 0) == "ARNG 0")
# Was: pin_range, autorange and the sweep all spelled these out by hand, so an
# analyzer that answered the graph-indexed form got a bare write it reads as a
# graph number - accepted, and silently changing nothing.
an.qform = {"ARNG": 1, "IRNG": 1}
fake.sent = []
an.pin_range(-30)
ok("pin_range follows the learned spelling",
   fake.sent == ["ARNG 0,0", "IRNG 0,-30"], str(fake.sent))
ok("the read follows it too", an.query_for("IRNG") == "IRNG? 0")
ok("SPAN and STRF have both spellings to follow",
   an.command("SPAN", 11) == "SPAN 11" and
   S.BY_KEY["SPAN"].writes[1] == "SPAN 0,{v}")


# ------------------------------------------------------------ 6. the panel

print("\n--- settling ---")

root, app = build_app()
app.settle_due.set()


def stopped(*a, **k):
    raise KeyboardInterrupt


app.an.settle = stopped
try:
    app.settle_for(11)
    ok("a stopped settle propagates", False)
except KeyboardInterrupt:
    ok("a stopped settle propagates", True)
# Was: cleared in a finally, so Stop during the wait after a span change left
# the analyzer marked as settled and the next trace came off an unflushed
# filter chain with nothing saying so.
ok("a settle cut short stays due", app.settle_due.is_set())
app.an.settle = lambda *a, **k: {"settle": "5 record lengths"}
app.settle_for(11)
ok("a settle that was served clears", not app.settle_due.is_set())
root.destroy()


print("\n--- file naming ---")

out = os.path.join(TMP, "naming")
os.makedirs(out, exist_ok=True)
root, app = build_app(outdir=out)
freqs = np.linspace(0, 390, sg.N_BINS)
amps = np.full(sg.N_BINS, -120.0)

app.save_csv.set(False), app.save_png.set(False), app.save_txt.set(True)
app.save_run(out, "20260830", "", freqs, amps, GOOD, {"measurement": "done"})
app.save_csv.set(True), app.save_png.set(False), app.save_txt.set(False)
app.save_run(out, "20260830", "", freqs, amps, GOOD, {"measurement": "done"})
root.update()
names = sorted(os.listdir(out))
# Was: only the ticked extensions were handed to unique_base, so this second
# capture landed on the first one's stem and adopted its .txt - two files that
# read as one capture while describing different settings.
ok("a capture cannot land on an earlier one's stem",
   len({os.path.splitext(n)[0] for n in names}) == 2, str(names))
root.destroy()


print("\n--- a sweep that goes wrong ---")

root, app = build_app()
app.set_busy(True)
root.update()
ok("busy while a run is in flight", app.busy)


def explode(*a, **k):
    raise MemoryError("Unable to allocate 8.00 TiB")


app._grab_runs = explode
try:
    # Was: the matrices were allocated before the try and set_busy(False) was
    # not in a finally, so this killed the worker and left every control
    # disabled with Stop unable to clear it. Under pythonw the traceback had no
    # console to reach either - the app just stopped responding.
    app._grab_worker([""], [None], [None])
finally:
    del app._grab_runs
root.update()
ok("the buttons come back after the worker dies", not app.busy)
ok("GRAB is clickable again", str(app.grab_btn["state"]) == "normal")
ok("the failure reached the log", "8.00 TiB" in logged(app))

clear_log(app)
app.set_busy(True)
app._grab_worker([""] * 101, [None] * 201, [None])      # 20301 runs
root.update()
ok("an oversized sweep is refused, not allocated for",
   "Trim the sweep lists" in logged(app),
   " ".join(logged(app).split())[:76])
ok("... and the buttons come back", not app.busy)


print("\n--- auto-grab ---")

clear_log(app)
app.busy = True
app.interval.set("60")
app.schedule_auto()
if app.auto_job is not None:
    root.after_cancel(app.auto_job)
    app.auto_job = None
# Was: do_grab returned without a word when the panel was busy or the analyzer
# had gone, so an unattended overnight run came back to missing files with
# nothing in the log to say which ones or why.
ok("a grab skipped because the last one is still running says so",
   "Auto-grab skipped" in logged(app) and "not finished" in logged(app))
app.busy = False
clear_log(app)
app.an.inst = None
app.schedule_auto()
if app.auto_job is not None:
    root.after_cancel(app.auto_job)
    app.auto_job = None
ok("a grab skipped because the analyzer went away says so",
   "not connected" in logged(app))
root.destroy()


# ------------------------------------------------ 7. one whole measurement

print("\n--- run_one, end to end ---")


def capture(snap, errs=0, ffts=0, pinned=None):
    root, app = build_app(snap=snap, errs=errs, ffts=ffts)
    app.lock.set(False)
    app.binary.set(True)
    app.do_arng.set(False)
    app.pinned_range = pinned
    _f, _a, _snap, notes = app.run_one()
    root.update()
    root.destroy()
    return notes


notes = capture(GOOD)
ok("a healthy capture is clean", notes["trace quality"] == "clean",
   notes["trace quality"])
ok("... and took the binary dump",
   notes["transfer"].startswith("binary"), notes["transfer"])
ok("... and its overload line is a verified no", notes["overload"] == "no")

notes = capture(dict(GOOD, AVGO="0"))
ok("averaging off gives one independent record",
   notes["independent records"] == "1.0", notes["independent records"])
ok("... and a 1 sigma of 1", notes["relative error (1 sigma)"] == "1")
ok("... and the metadata says why", "OFF" in notes.get("averaging", ""))

notes = capture({k: v for k, v in GOOD.items() if k not in ("UNIT0", "DISP0")})
ok("a trace labelled on a fallback is SUSPECT",
   notes["trace quality"].startswith("SUSPECT"), notes["trace quality"][:64])
ok("... and says which settings went unread", "read back" in
   notes["trace quality"])
ok("... and fell back to the slow readout",
   notes["transfer"].startswith("bin by bin"), notes["transfer"])

notes = capture(GOOD, errs=0, ffts=None)
ok("a half-read status is SUSPECT",
   notes["trace quality"].startswith("SUSPECT"), notes["trace quality"][:64])
ok("... naming the byte that went quiet", "FFTS" in notes["trace quality"])

notes = capture(GOOD, errs=None, ffts=None)
ok("neither byte gives a readable fault, not the bare word unread",
   "ERRS and FFTS did not answer" in notes["trace quality"],
   notes["trace quality"][:64])

notes = capture(GOOD, errs=128, ffts=0)
ok("a real overload is still reported as one",
   "overload flagged" in notes["trace quality"], notes["trace quality"][:56])

notes = capture(GOOD, pinned=-30.0)
ok("a range that held is verified against the pin",
   notes["trace quality"] == "clean: range verified against the pin",
   notes["trace quality"])

# Three faults at once, and no one of them may hide the others: the range
# slipped its pin, the front end overloaded, and the averaging was vector.
notes = capture(dict(GOOD, IRNG="-20", AVGT="1"), errs=128, pinned=-30.0)
quality = notes["trace quality"]
ok("every fault is listed, not just the first", quality.count("; ") == 2,
   quality[:100])
ok("... the slipped range is one of them", "-20 dBV" in quality)
ok("... the overload is another", "overload flagged" in quality)
ok("... and the vector averaging is the third", "vector" in quality)


# --------------------------------------------- 8. what a sweep will cost

print("\n--- capture_time ---")

T_REC = S.record_time(11)                       # 1.0256 s at the 390 Hz span
ok("the record length is bins/span", abs(T_REC - 400 / 390.0) < 1e-9,
   f"{T_REC:.4f} s")
# The figure the protocol was costed against, and the one plan_set computes:
# settle + NAVG * T_rec + transfer, exact because the preset sets OVLP 0.
plain = S.capture_time(11, navg=100, ovlp=0, settle_recs=5.0, transfer_s=1.5)
ok("a capture is settle + NAVG*T_rec + transfer",
   abs(plain - (5 * T_REC + 100 * T_REC + 1.5)) < 1e-6,
   f"{plain:.1f} s = {S.fmt_hms(plain)}")
ok("100 averages at the 390 Hz span is the ~102 s the protocol was costed at",
   abs(100 * T_REC - 102.6) < 0.5)

# Overlapping records arrive faster. capture_time can only put a floor under
# it - the analyzer still has to finish an FFT between them - so the sweep
# rescales against its own runs rather than trusting this.
half = S.capture_time(11, navg=100, ovlp=50, settle_recs=5.0, transfer_s=1.5)
deep = S.capture_time(11, navg=100, ovlp=90, settle_recs=5.0, transfer_s=1.5)
ok("overlap shortens the estimate", plain > half > deep,
   f"0% {plain:.0f}s, 50% {half:.0f}s, 90% {deep:.0f}s")
ok("90% overlap is about a tenth of the averaging",
   abs((deep - 5 * T_REC - 1.5) - (T_REC + 99 * T_REC * 0.1)) < 1e-6)
ok("an overlap of 100 does not divide by zero",
   np.isfinite(S.capture_time(11, navg=100, ovlp=100)))

# An average with no finish of its own runs for the exponential wait, which is
# exactly what run_one will sit out for it.
expo = S.capture_time(11, navg=100, averaged=False, exp_wait_s=30.0,
                      settle_recs=5.0, transfer_s=1.5)
ok("an exponential average is priced at the exponential wait",
   abs(expo - (5 * T_REC + 30.0 + 1.5)) < 1e-6, f"{expo:.1f} s")
ok("so is averaging switched off",
   S.capture_time(11, navg=None, exp_wait_s=30.0) == S.capture_time(
       11, navg=100, averaged=False, exp_wait_s=30.0))

# 100 averages at the 191 mHz span is 58 hours; the measurement timeout is what
# actually stops it, so the estimate has to stop there too.
capped = S.capture_time(0, navg=100, timeout_s=600.0, transfer_s=1.5)
ok("the measurement timeout caps the estimate", capped < 602.0,
   f"{S.fmt_hms(capped)}, uncapped it would be "
   f"{S.fmt_hms(S.capture_time(0, navg=100, transfer_s=1.5))}")
ok("an unknown span gives NaN, not a guess",
   not np.isfinite(S.capture_time(None, navg=100)))
ok("... and reads as a question mark", S.fmt_hms(float("nan")) == "?")

print("\n--- fmt_hms ---")

# The same three the run_protocol suite pins, because that copy and this one
# have to go on agreeing - the planner cannot import this module.
ok("seconds", S.fmt_hms(12.3) == "12.3s", S.fmt_hms(12.3))
ok("minutes", S.fmt_hms(102.6) == "1m43s", S.fmt_hms(102.6))
ok("hours", S.fmt_hms(7000) == "1h56m", S.fmt_hms(7000))
ok("a round minute keeps its seconds", S.fmt_hms(120) == "2m00s")

print("\n--- the estimate as a sweep runs ---")

plan = [100.0] * 10
line = sg.App.eta(plan, 0, 0.0)
ok("before the first run the whole plan is left", "16m40s" in line, line)
ok("... and it says it is an estimate", "(estimated)" in line)
ok("... and counts from one", line.startswith("run 1/10"))

# Runs coming in exactly on plan must leave the plan alone.
line = sg.App.eta(plan, 5, 500.0)
ok("runs landing on plan leave the estimate alone", "8m20s" in line, line)
ok("... and the run counter moves", line.startswith("run 6/10"))

# Was: scale = measured/planned applied whole, so one instant run against a
# 100 s plan reported "about 0.0s left" with fifteen minutes still to go.
line = sg.App.eta(plan, 1, 0.0)
ok("one freak-fast run does not collapse the estimate",
   "0.0s left" not in line, line)
ok("... it is pulled by a third, not all the way",
   abs(sum(plan[1:]) * (2 / 3.0) - 600.0) < 1e-6)

# But a genuinely slow sweep must still be believed once there is evidence. Two
# runs left is 3m20s on the plan; at eight runs of evidence that it goes at half
# speed the correction carries 8/10 of the answer, so 200 s * (0.8*2 + 0.2).
slow = sg.App.eta(plan, 8, 1600.0)          # twice as slow as planned
ok("a consistently slow sweep is believed by the eighth run",
   "6m00s" in slow, slow + "   (the raw plan says 3m20s)")
ok("... and by then it is most of the way to the full correction",
   abs(sum(plan[8:]) * (0.8 * 2 + 0.2) - 360.0) < 1e-6)
ok("a NaN plan degrades to just the counter",
   sg.App.eta([float("nan")] * 3, 0, 0.0) == "run 1/3",
   sg.App.eta([float("nan")] * 3, 0, 0.0))

print("\n--- sweep_plan ---")

root, app = build_app()
app.settle_recs.set("5")
app.settle_s.set("0")
app.binary.set(True)
app.do_arng.set(False)
plan = app.sweep_plan(["a", "b"], [0.0, 1.0, 2.0], [11, 12])
ok("one entry per run, in the order the loop walks them", len(plan) == 12,
   str(len(plan)))
ok("span is the innermost axis, as the loop has it",
   plan[0] == plan[2] == plan[4] and plan[0] != plan[1],
   f"{plan[0]:.1f}, {plan[1]:.1f}")
ok("a narrower span costs more", plan[0] > plan[1],
   f"span 11 {plan[0]:.1f}s vs span 12 {plan[1]:.1f}s")
ok("the settle is counted when the sweep writes a span",
   abs(plan[0] - S.capture_time(11, navg=100, ovlp=0, settle_recs=5.0,
                                timeout_s=600.0,
                                transfer_s=S.TRANSFER_BINARY_S)) < 1e-6)
# Nothing is written, so nothing has to settle.
flat = app.sweep_plan([""], [None], [None])
ok("no settle when the sweep leaves span and start alone",
   abs(flat[0] - S.capture_time(11, navg=100, ovlp=0, settle_recs=0.0,
                                timeout_s=600.0,
                                transfer_s=S.TRANSFER_BINARY_S)) < 1e-6,
   f"{flat[0]:.1f} s")
app.binary.set(False)
ok("the ASCII readout is priced as the slow one it is",
   app.sweep_plan([""], [None], [None])[0] - flat[0]
   == S.TRANSFER_ASCII_S - S.TRANSFER_BINARY_S,
   f"{app.sweep_plan([''], [None], [None])[0] - flat[0]:.1f} s more")
app.binary.set(True)
root.destroy()

# The span has to come from somewhere. When the sweep does not name one and the
# analyzer will not say, there is no estimate to give.
root, app = build_app(snap={k: v for k, v in GOOD.items() if k != "SPAN"})
ok("no span from either side means no estimate",
   app.sweep_plan([""], [None], [None]) == [])
ok("... but a sweep that names its spans needs no help",
   len(app.sweep_plan([""], [None], [11, 12])) == 2)
root.destroy()

print("\n--- the sweep logs it ---")

root, app = build_app()
app.save_csv.set(False), app.save_png.set(False), app.save_txt.set(False)
app.save_npy.set(False), app.combined.set(False)
app.lock.set(False), app.pause_cases.set(False)
clear_log(app)
app._grab_runs([""], [0.0, 1.0, 2.0], [11])
root.update()
log = logged(app)
ok("the sweep says what it will cost before it starts",
   "about 5m28s of measuring" in log,
   " ".join(log.split())[:88])
ok("... and when it expects to finish", "finishing around" in log)
ok("every run carries a countdown", log.count(" left, done ~") == 3)
ok("the first is flagged as an estimate", "(estimated)" in log)
root.destroy()

# ------------------------------------- 9. the combined plot as it builds

print("\n--- the sweep draws itself as it goes ---")


def sweep_into(outdir, starts, combined=True, save_png=True, spans=None,
               min_s=0.0):
    """Run a sweep and record every time the window was handed a plot."""
    root, app = build_app(outdir=outdir)
    shown = []
    app.show_building = lambda data, done, planned: shown.append(
        ("building", done, planned))
    app.show_preview = lambda path: shown.append(("saved", path, None))
    app.save_csv.set(False), app.save_txt.set(False), app.save_npy.set(False)
    app.save_png.set(save_png), app.combined.set(combined)
    app.lock.set(False), app.pause_cases.set(False)
    was, sg.PROGRESS_MIN_S = sg.PROGRESS_MIN_S, min_s
    try:
        app._grab_runs([""], starts, spans or [11])
        root.update()
    finally:
        sg.PROGRESS_MIN_S = was
    log = logged(app)
    plot = app.last_plot
    root.destroy()
    return shown, log, plot


out = os.path.join(TMP, "building")
os.makedirs(out, exist_ok=True)
shown, log, plot = sweep_into(out, [0.0, 1.0, 2.0, 3.0])

# Was: the window showed each segment on its own and the combined plot appeared
# once, at the end - a long time to wait to notice that segment three is on the
# wrong range or that the overlaps are not joining.
built = [s for s in shown if s[0] == "building"]
ok("the window is handed the sweep after every run", len(built) == 4,
   str([s[1] for s in built]))
ok("... and it grows a trace at a time", [s[1] for s in built] == [1, 2, 3, 4],
   str([s[1] for s in built]))
ok("... each knowing how many are coming", all(s[2] == 4 for s in built))
ok("no single-segment plot steals the window on the way",
   not any(s[0] == "saved" for s in shown[:-1]),
   str([s[0] for s in shown]))
ok("the finished combined plot is what is left on screen",
   shown[-1][0] == "saved" and shown[-1][1].endswith(".png"),
   str(shown[-1][:2]))
ok("the zoom window has every trace to redraw from", len(plot[0]) == 4,
   f"{len(plot[0])} traces")

# A sweep leaves one plot behind, not one per segment.
pngs = sorted(n for n in os.listdir(out) if n.endswith(".png"))
ok("a four-run sweep writes one plot, not four", len(pngs) == 1, str(pngs))
ok("... and it is the combined one", "_sweep_" in pngs[0], pngs[0])

print("\n--- and only when there is something to build ---")

out = os.path.join(TMP, "single")
os.makedirs(out, exist_ok=True)
shown, _, _ = sweep_into(out, [None])
ok("a single grab still shows its own plot",
   [s[0] for s in shown] == ["saved"], str([s[0] for s in shown]))

out = os.path.join(TMP, "nocombined")
os.makedirs(out, exist_ok=True)
shown, _, _ = sweep_into(out, [0.0, 1.0, 2.0], combined=False)
# The tick says whether to SAVE the combined plot. It used to decide the live
# view too, which was harmless while a sweep still drew every segment - now
# that it does not, that would have left the window blank for the whole run.
ok("unticking the combined plot still lets you watch it build",
   [s[0] for s in shown] == ["building"] * 3, str([s[0] for s in shown]))
ok("... it just does not save the picture",
   not any(n.endswith(".png") for n in os.listdir(out)), str(os.listdir(out)))

out = os.path.join(TMP, "nopng")
os.makedirs(out, exist_ok=True)
shown, _, _ = sweep_into(out, [0.0, 1.0, 2.0], save_png=False)
ok("the building plot does not need the per-run PNGs to be saved",
   [s[0] for s in shown[:3]] == ["building"] * 3,
   str([s[0] for s in shown]))
ok("... and nothing but the combined plot is written",
   all("_sweep_" in n for n in os.listdir(out) if n.endswith(".png")),
   str(os.listdir(out)))


# ------------------------------------- 9b. one set of files, not one per run

print("\n--- GRAB one and RUN SWEEP are different buttons ---")

out = os.path.join(TMP, "split")
os.makedirs(out, exist_ok=True)
root, app = build_app(outdir=out)
app.save_png.set(False), app.save_npy.set(False), app.combined.set(False)
app.lock.set(False), app.pause_cases.set(False)
started = []
app._grab_worker = lambda cases, starts, spans: started.append(
    (cases, starts, spans))


class RunNow:
    """A Thread that runs on the spot, so what a button did is settled by the
    time the next line looks."""

    def __init__(self, target=None, args=(), daemon=None, **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


real_thread, sg.threading.Thread = sg.threading.Thread, RunNow


def press(action):
    """The stub _grab_worker never reaches the real one's finally, so busy has
    to be let go by hand between presses."""
    app.busy = False
    started.clear()
    clear_log(app)
    action()
    return list(started)

# Was: one button, and what it did depended on three boxes elsewhere on the
# panel - a stitch left in them turned the next single capture into an
# eight-hour run.
app.spans_txt.set("11, 12")
app.starts_txt.set("0, 390, 780")
app.cases_txt.set("dark, light")
ok("GRAB one takes one capture whatever is in the sweep boxes",
   press(app.do_grab) == [([""], [None], [None])], str(started))
ok("RUN SWEEP reads them",
   press(app.do_sweep) == [(["dark", "light"], [0.0, 390.0, 780.0], [11, 12])],
   str(started))

app.spans_txt.set(""), app.starts_txt.set(""), app.cases_txt.set("")
ok("RUN SWEEP with nothing in the boxes refuses", press(app.do_sweep) == [])
ok("... and says which button takes a single capture",
   "GRAB one" in logged(app), " ".join(logged(app).split())[:78])
ok("GRAB one still works with the boxes empty",
   press(app.do_grab) == [([""], [None], [None])])

app.spans_txt.set("99")
ok("a span code out of range is still caught", press(app.do_sweep) == [])
ok("... and named", "not in 0-19" in logged(app),
   " ".join(logged(app).split())[:60])
app.spans_txt.set("")

# Auto-grab sits under GRAB one, so that is what it does. It used to sweep if
# the boxes happened to be filled.
app.starts_txt.set("0, 390")
app.busy = False
started.clear()
app.schedule_auto()
if app.auto_job is not None:
    root.after_cancel(app.auto_job)
    app.auto_job = None
ok("auto-grab takes single captures, not sweeps",
   started == [([""], [None], [None])], str(started))
sg.threading.Thread = real_thread
del app._grab_worker
root.destroy()


print("\n--- a sweep leaves one set of files ---")


def run_sweep(outdir, starts, cases=None, keep=False, spans=None):
    root, app = build_app(outdir=outdir)
    for var in (app.save_csv, app.save_png, app.save_txt, app.save_npy,
                app.combined):
        var.set(True)
    app.keep_segments.set(keep)
    app.lock.set(False), app.pause_cases.set(False)
    if cases:
        app.cases_txt.set(", ".join(cases))
    app._grab_runs(cases or [""], starts, spans or [11])
    root.update()
    log = logged(app)
    root.destroy()
    return sorted(os.listdir(outdir)), log


# The sweep stamps its own filenames with today's date, so the expected
# names have to be built the same way - hardcoding one passes only on the
# day it was written.
TODAY = datetime.datetime.now().strftime("%Y%m%d")

out = os.path.join(TMP, "onefileset")
os.makedirs(out, exist_ok=True)
files, log = run_sweep(out, [0.0, 1.0, 2.0, 3.0])
# Was: four segments left twelve files - a csv, a png and a txt each - and the
# combined set on top, all of it the same four traces written twice.
ok("four segments leave one set of files, not four", len(files) == 6,
   str(files))
ok("... the traces, as one CSV", files.count(f"sr760_sweep_{TODAY}.csv") == 1)
ok("... the matrices and their axes",
   sum(n.endswith((".npy", "_axes.json")) for n in files) == 3, str(files))
ok("... one plot", sum(n.endswith(".png") for n in files) == 1)
ok("... and one metadata file", sum(n.endswith(".txt") for n in files) == 1)
ok("no per-segment file survives",
   not any("_span" in n for n in files), str(files))

print("\n--- but the combined CSV is the whole sweep ---")

csv = os.path.join(out, f"sr760_sweep_{TODAY}.csv")
rows = np.loadtxt(csv, delimiter=",", skiprows=1)
ok("every point of every segment is in it", rows.shape == (4 * sg.N_BINS, 3),
   str(rows.shape))
ok("... sorted into one curve",
   list(rows[:, 0]) == sorted(rows[:, 0]))
ok("... with a column saying which segment each point came from",
   sorted(set(rows[:, 2])) == [1.0, 2.0, 3.0, 4.0], str(sorted(set(rows[:, 2]))))
header = open(csv, encoding="utf-8").readline().strip()
ok("... and a header naming the scale", header.endswith("dBV_sqrtHz,segment"),
   header)
# The third column must not stop Compare reading it back.
f, a, lab = S.read_csv(csv)
ok("the compare loader reads it", len(f) == 4 * sg.N_BINS
   and S.canonical_units(lab) == "dBV/sqrtHz", f"{len(f)}, {lab}")

print("\n--- and the metadata describes every segment ---")

meta = open(os.path.join(out, f"sr760_sweep_{TODAY}.txt"),
            encoding="utf-8").read()
ok("the sweep is described", "1 case(s) x 4 start freq(s)" in meta)
ok("the runs are counted", "runs completed       : 4" in meta, )
ok("the settings block is there once",
   meta.count("analyzer settings") == 1)
ok("there is a line per segment", meta.count("\n     1  ") == 1
   and meta.count("\n     4  ") == 1)
ok("each carries what varied",
   "N_indep" in meta and "1 sigma" in meta and "overload" in meta)
ok("the units are stated", "trace units" in meta)

# A flagged segment has to be quoted, since nothing on the trace shows it.
out2 = os.path.join(TMP, "suspect")
os.makedirs(out2, exist_ok=True)
root, app = build_app(snap=dict(GOOD, AVGT="1"), outdir=out2)
for var in (app.save_csv, app.save_png, app.save_txt, app.save_npy,
            app.combined):
    var.set(True)
app.keep_segments.set(False)
app.lock.set(False), app.pause_cases.set(False)
app._grab_runs([""], [0.0, 1.0], [11])
root.update()
root.destroy()
meta = open(os.path.join(out2, [n for n in os.listdir(out2)
                                if n.endswith(".txt")][0]),
            encoding="utf-8").read()
ok("a flagged segment is listed as suspect", "suspect segments" in meta)
ok("... and quoted in full", "vector averaging" in meta,
   [l for l in meta.splitlines() if "vector" in l][0][:70])

print("\n--- the old behaviour is one tick away ---")

out = os.path.join(TMP, "keepthem")
os.makedirs(out, exist_ok=True)
files, _ = run_sweep(out, [0.0, 1.0, 2.0], keep=True)
ok("keep per-segment files puts them back",
   sum("_span" in n for n in files) == 9, str(sum("_span" in n for n in files)))
ok("... and the combined set is still written alongside",
   sum("_sweep_" in n for n in files) == 6, str(sorted(files)))
ok("... which is 15 files for three segments, the old way",
   len(files) == 15, str(len(files)))

print("\n--- a case gets its own CSV ---")

out = os.path.join(TMP, "cases")
os.makedirs(out, exist_ok=True)
files, _ = run_sweep(out, [0.0, 1.0], cases=["dark", "light"])
csvs = sorted(n for n in files if n.endswith(".csv"))
ok("one CSV per case", len(csvs) == 2, str(csvs))
ok("... named for the case", all("dark" in n or "light" in n for n in csvs),
   str(csvs))
rows = np.loadtxt(os.path.join(out, csvs[0]), delimiter=",", skiprows=1)
ok("... holding only that case's segments", rows.shape == (2 * sg.N_BINS, 3),
   str(rows.shape))

print("\n--- the redraw is throttled ---")

out = os.path.join(TMP, "throttle")
os.makedirs(out, exist_ok=True)
# Every trace drawn again for every run is quadratic, so a long sweep of short
# runs must not spend its time drawing. These runs return instantly, so all but
# the first fall inside the window.
shown, _, _ = sweep_into(out, [float(i) for i in range(6)], min_s=30.0)
built = [s for s in shown if s[0] == "building"]
ok("a burst of fast runs does not redraw for every one", len(built) == 1,
   f"{len(built)} redraws for 6 runs")
ok("the finished plot is still drawn at the end",
   shown[-1][0] == "saved", str(shown[-1][0]))

root, app = build_app()
app.last_progress = 0.0
ok("force overrides the throttle",
   app.show_sweep_so_far([(np.zeros(3), np.ones(3), "a")], 2, "dBV", "linear")
   and not app.show_sweep_so_far([(np.zeros(3), np.ones(3), "a")], 2, "dBV",
                                 "linear")
   and app.show_sweep_so_far([(np.zeros(3), np.ones(3), "a")], 2, "dBV",
                             "linear", force=True))
ok("nothing to draw yet draws nothing",
   not app.show_sweep_so_far([], 2, "dBV", "linear", force=True))
root.destroy()

# ------------------------------------------ 10. comparing saved sequences

print("\n--- reading the units back off disk ---")

ok("the CSV spelling and the live one are one scale",
   S.unit_parts("dBVrms_sqrtHz") == S.unit_parts("dBVrms/sqrtHz")
   == ("dBVrms", True))
ok("a plain scale has no density flag", S.unit_parts("Vpk") == ("Vpk", False))
ok("the drawn spelling parses too", S.unit_parts("Vrms/√Hz") == ("Vrms", True))
# Phase is the one that matters: degrees do not convert into volts.
ok("phase is not an amplitude scale", S.unit_parts("deg") is None)
ok("nor is anything unrecognised", S.unit_parts("bananas") is None)
# Was: the header spelling never compared equal to trace_units(), so a loaded
# sequence was "converted" from a scale to itself and the axis kept the
# underscore.
ok("the header spelling canonicalises to the live one",
   S.canonical_units("dBVrms_sqrtHz") == "dBVrms/sqrtHz")
ok("... and an unknown label is handed back untouched",
   S.canonical_units("bananas") == "bananas")
ok("the axis label is drawn either way",
   S.pretty_units("Vpk_sqrtHz") == S.pretty_units("Vpk/sqrtHz") == "Vpk/√Hz")

print("\n--- converting between scales ---")

# 1 Vpk is 0 dBV, and is 1/sqrt(2) Vrms, which is -3.01 dBVrms.
ok("Vpk to dBV is 20 log10",
   abs(S.convert_amplitude([1.0], "Vpk", "dBV")[0]) < 1e-12)
ok("Vpk to Vrms divides by root two",
   abs(S.convert_amplitude([1.0], "Vpk", "Vrms")[0] - 1 / np.sqrt(2)) < 1e-12)
ok("Vpk to dBVrms is 3.01 dB down",
   abs(S.convert_amplitude([1.0], "Vpk", "dBVrms")[0] + 3.0103) < 1e-3,
   f"{S.convert_amplitude([1.0], 'Vpk', 'dBVrms')[0]:.4f} dB")
# The real case this was built for: the 20260826 floor is in dBVrms/sqrtHz and
# the 20260830 one in Vpk/sqrtHz, so one of them has to move to be compared.
# -137.7 dBVrms is 10**(-137.7/20) = 1.3033e-7 Vrms, and root two more in peak.
got = S.convert_amplitude([-137.7], "dBVrms/sqrtHz", "Vpk/sqrtHz")[0]
want = 10 ** (-137.7 / 20.0) * np.sqrt(2)
ok("a real dBVrms floor converts to volts peak", abs(got - want) < 1e-15,
   f"{got:.5g} Vpk/sqrtHz")
ok("... which is 1.843e-7", abs(got - 1.8430e-7) < 1e-11, f"{got:.5g}")
for a, b in (("Vpk", "dBV"), ("Vrms", "dBVrms"), ("dBV", "Vrms"),
             ("dBVrms", "Vpk")):
    there = S.convert_amplitude([0.5], a, b)
    back = S.convert_amplitude(there, b, a)
    ok(f"{a} -> {b} -> {a} round trips", abs(back[0] - 0.5) < 1e-12)
ok("the same scale is left alone",
   S.convert_amplitude([1.0, 2.0], "dBV", "dBV").tolist() == [1.0, 2.0])
# A linear trace that reaches zero has no dB equivalent; a gap beats -inf
# dragging the axis to the floor.
conv = S.convert_amplitude([1.0, 0.0, -1.0], "Vpk", "dBV")
ok("a non-positive volt reading becomes a gap, not minus infinity",
   np.isfinite(conv[0]) and np.isnan(conv[1]) and np.isnan(conv[2]),
   str(conv))

print("\n--- and refusing to ---")

for frm, to, why in (("Vpk/sqrtHz", "Vpk", "density against spectrum"),
                     ("Vpk", "Vrms/sqrtHz", "the other way round"),
                     ("deg", "dBV", "phase into volts"),
                     ("dBV", "rad", "volts into phase")):
    try:
        S.convert_amplitude([1.0], frm, to)
        ok(f"{why} is refused", False, "it converted instead")
    except ValueError as exc:
        ok(f"{why} is refused", True, str(exc)[:56])

print("\n--- loading captures back ---")

out = os.path.join(TMP, "seqs")
os.makedirs(out, exist_ok=True)
f1 = np.linspace(0, 390, 8)
f2 = np.linspace(390, 780, 8)
for name, freqs, label in (("floor_span11_390Hz_20260830", f1, "dBVrms/sqrtHz"),
                           ("floor_span11_strf390Hz_780Hz_20260830", f2,
                            "dBVrms/sqrtHz")):
    S.write_csv(os.path.join(out, name + ".csv"), freqs,
                np.full(8, -120.0), label)
S.write_csv(os.path.join(out, "other_span11_390Hz_20260830.csv"), f1,
            np.full(8, 1e-6), "Vpk/sqrtHz")

freqs, amps, ylabel = S.read_csv(os.path.join(out,
                                              "floor_span11_390Hz_20260830.csv"))
ok("a written capture reads back", len(freqs) == 8 and amps[0] == -120.0)
ok("... carrying the scale it was measured on", ylabel == "dBVrms_sqrtHz",
   ylabel)
# A file with no header must not lose its first data row to skiprows.
bare = os.path.join(out, "bare.csv")
np.savetxt(bare, np.column_stack([f1, np.full(8, -3.0)]), delimiter=",")
bf, ba, bl = S.read_csv(bare)
ok("a headerless file keeps every row", len(bf) == 8 and bl == "", f"{len(bf)}")

print("\n--- grouping into sequences ---")

ok("the name is what the segments share",
   sg.sequence_label(os.path.join(out, "floor_span11_strf390Hz_780Hz_2026.csv"))
   .startswith("floor"),
   sg.sequence_label(os.path.join(out, "floor_span11_390Hz_2026.csv")))
ok("the folder is part of it - one title on two days is two sequences",
   sg.sequence_label(r"C:\d\20260826\a_span11_390Hz_x.csv")
   != sg.sequence_label(r"C:\d\20260830\a_span11_390Hz_x.csv"))

paths = sorted(glob.glob(os.path.join(out, "*.csv")))
seqs = load_sequences(paths)
by = {s["name"].split()[0]: s for s in seqs}
ok("segments of one title become one sequence",
   by["floor"]["segments"] == 2, str(by["floor"]["segments"]))
ok("... concatenated and sorted into one curve",
   len(by["floor"]["freqs"]) == 16
   and list(by["floor"]["freqs"]) == sorted(by["floor"]["freqs"]),
   str(len(by["floor"]["freqs"])))
ok("... on the scale its header named",
   by["floor"]["ylabel"] == "dBVrms/sqrtHz", by["floor"]["ylabel"])
ok("a different title is a different sequence", "other" in by)
ok("a headerless file is still a sequence of its own", "bare" in by)

# A segment measured in something else is not part of the same measurement.
mixed = os.path.join(TMP, "mixed")
os.makedirs(mixed, exist_ok=True)
S.write_csv(os.path.join(mixed, "m_span11_390Hz_x.csv"), f1,
            np.full(8, -120.0), "dBVrms/sqrtHz")
S.write_csv(os.path.join(mixed, "m_span11_strf390Hz_780Hz_x.csv"), f2,
            np.full(8, 1e-6), "Vpk/sqrtHz")
said = []
got = load_sequences(sorted(glob.glob(os.path.join(mixed, "*.csv"))),
                     log=said.append)
ok("a segment in other units is dropped from its group",
   got[0]["segments"] == 1, str(got[0]["segments"]))
ok("... and said so", any("dropped" in m for m in said),
   " ".join(said)[:76])

print("\n--- drawing them underneath ---")

root, app = build_app()
app.compare = load_sequences(paths)
app.refresh_compare()
ok("the panel says what is loaded",
   "3 sequence" in app.compare_status.cget("text"),
   app.compare_status.cget("text")[:60])

refs = app.refs_for("dBVrms/sqrtHz")
ok("a sequence already on the scale is used as it is",
   any("floor" in r[2] and "was" not in r[2] for r in refs),
   str([r[2] for r in refs]))
ok("one on another scale is converted and the legend says so",
   any("other" in r[2] and "was Vpk/√Hz" in r[2] for r in refs),
   str([r[2] for r in refs]))
clear_log(app)
app.refs_for("dBVrms/sqrtHz")
ok("the conversion is not announced again on every redraw",
   logged(app).count("converted from") == 0, logged(app)[:60])

# Phase cannot go on an amplitude axis at all.
clear_log(app)
app.said.clear()
refs = app.refs_for("deg")
ok("nothing is drawn against an axis it cannot be put on", refs == [])
ok("... and each one is named once", logged(app).count("left out") == 3,
   str(logged(app).count("left out")))

print("\n--- the comparison plot ---")

cmp_out = os.path.join(TMP, "cmp")
os.makedirs(cmp_out, exist_ok=True)
app.outdir.set(cmp_out)
app.dated.set(False)
shown = []
app.show_preview = lambda p: shown.append(p)
app.do_compare_plot()
root.update()
made = [n for n in os.listdir(cmp_out) if n.endswith(".png")]
ok("a comparison is saved", len(made) == 1, str(made))
ok("... named for what it is", "_compare_" in made[0], made[0])
ok("... and shown", len(shown) == 1)
# Was going to be drawn twice: once in colour as the subject, once in grey
# underneath, because write_plot fills refs in when it is not told otherwise.
traces, _t, _s, ylabel, _y, plot_refs = app.last_plot
ok("the sequences are the traces, not also the references",
   plot_refs == (), f"{len(traces)} traces, {len(plot_refs)} refs")
# Was: the scale came from compare[0], so the headerless 'bare' sequence -
# first alphabetically, and on no scale at all - became the target that
# nothing could convert to, and the two real sequences were dropped instead.
ok("the scale comes from a sequence that has one",
   ylabel == "dBVrms/sqrtHz", ylabel)
ok("... so the sequences on a known scale are the ones that plot",
   len(traces) == 2, f"{len(traces)} traces")
ok("... and the one with no units is left out and named",
   "bare" in logged(app) and "left out" in logged(app),
   " ".join(logged(app).split())[-88:])

app.do_compare_clear()
ok("clearing empties it", app.compare == [] and app.refs_for("dBV") == [])
ok("... and the panel says so",
   "nothing loaded" in app.compare_status.cget("text"))
root.destroy()

print("\n--- and under a building sweep ---")

out = os.path.join(TMP, "sweepref")
os.makedirs(out, exist_ok=True)
root, app = build_app(outdir=out)
app.compare = load_sequences(paths)
app.save_csv.set(False), app.save_txt.set(False), app.save_npy.set(False)
app.save_png.set(False), app.combined.set(True)
app.lock.set(False), app.pause_cases.set(False)
was, sg.PROGRESS_MIN_S = sg.PROGRESS_MIN_S, 0.0
try:
    app._grab_runs([""], [0.0, 1.0, 2.0], [11])
    root.update()
finally:
    sg.PROGRESS_MIN_S = was
traces, _t, _s, ylabel, _y, plot_refs = app.last_plot
ok("the building sweep carries the references underneath",
   len(plot_refs) >= 1, f"{len(traces)} traces, {len(plot_refs)} refs")
ok("... converted onto the capture's own scale",
   ylabel == trace_units(GOOD), ylabel)
root.destroy()

# --------------------------------------- 11. the stitch stays in the band

print("\n--- filling the start frequencies ---")

ok("the top of the band is the widest span", S.MAX_FREQ == 100000.0,
   f"{S.MAX_FREQ:g} Hz")

root, app = build_app()


def stitch(spacing, stop, overlap=10):
    clear_log(app)
    app.fill_stitch(spacing, stop, overlap, "test")
    text = app.starts_txt.get()
    return [float(x) for x in text.split(",") if x.strip()], logged(app), text


# The two real cases. Span 13 is the 20260826 stitch and span 11 the 259
# segment one; both asked for start frequencies the analyzer could not honour.
for spacing, span_name in ((3.9058, "span 13"), (0.96680, "span 11")):
    span = sg.N_BINS * spacing
    starts, log, _ = stitch(spacing, 100000.0)
    over = [s for s in starts if s + span > S.MAX_FREQ + 1e-6]
    # Was: the last start ran past the top and the analyzer clamped it without
    # saying so - two starts past the limit would both land on the same place
    # and measure the same band twice.
    ok(f"{span_name}: no segment starts where its span runs off the top",
       not over, f"{len(over)} of {len(starts)} would")
    ok(f"{span_name}: the last start is the highest one that fits",
       abs(starts[-1] - (S.MAX_FREQ - span)) < 1e-6,
       f"{starts[-1]:.4f} vs {S.MAX_FREQ - span:.4f}")
    top = starts[-1] + (sg.N_BINS - 1) * spacing
    ok(f"{span_name}: the band is still measured to the top",
       top > S.MAX_FREQ - spacing - 1e-6, f"reaches {top:.2f} Hz")
    ok(f"{span_name}: and it says the last one was pulled down",
       "would have run past" in log and "instead of" in log)

# 98437.68 Hz is where the analyzer had been clamping the 20260826 stitch to,
# which is why its top measured frequency came out at 99996 Hz.
starts, _, _ = stitch(3.9058, 100000.0)
ok("the pulled-down start is where the analyzer was clamping to anyway",
   abs(starts[-1] - 98437.68) < 0.01, f"{starts[-1]:.2f} Hz")
ok("... and reaches the 99996 Hz the saved data tops out at",
   abs(starts[-1] + 399 * 3.9058 - 99996) < 1.0,
   f"{starts[-1] + 399 * 3.9058:.1f} Hz")

# The ordinary case must not change: a stitch that fits in the band steps
# evenly the whole way and shares exactly the overlap asked for.
starts, log, _ = stitch(0.96680, 20000.0)
steps = {round((starts[i + 1] - starts[i]) / 0.96680)
         for i in range(len(starts) - 1)}
ok("a stitch inside the band steps evenly all the way", steps == {390},
   str(sorted(steps)))
ok("... and nothing is said about pulling anything down",
   "would have run past" not in log)
ok("... and it still overshoots the stop, so the stop is covered",
   starts[-1] + 399 * 0.96680 > 20000.0)

starts, log, _ = stitch(3.9058, 150000.0)
ok("a stop above the band is capped", "100000 Hz is the top" in log,
   " ".join(log.split())[:74])
ok("... and no start goes past it either",
   all(s + 400 * 3.9058 <= S.MAX_FREQ + 1e-6 for s in starts))

# The overlap the last pair actually ends up sharing is reported, not left to
# be worked out from the numbers.
starts, log, _ = stitch(3.9058, 100000.0)
shared = sg.N_BINS - round((starts[-1] - starts[-2]) / 3.9058)
ok("the extra overlap is named", f"shares {shared} points" in log,
   f"shares {shared} points")
ok("... and it is more than was asked for", shared > 10, str(shared))

# A span wider than the band cannot be stitched at all.
_, log, text = stitch(300.0, 100000.0)          # 400 x 300 Hz = 120 kHz
ok("a span wider than the band is refused",
   "more than the analyzer" in log, " ".join(log.split())[:70])

# Whatever it writes has to survive being read back - fill_stitch and
# parse_list share MAX_LIST_ITEMS so the box it fills cannot be one the sweep
# then refuses.
starts, _, text = stitch(0.19073, 100000.0)     # 1345 segments, just inside
ok("a long stitch that fits is filled in full",
   len(starts) <= S.MAX_LIST_ITEMS, str(len(starts)))
ok("... and what it wrote parses back to what it meant",
   len(S.parse_list(text)) == len(starts), str(len(S.parse_list(text))))

# A span narrow enough to need more segments than the sweep will take. The
# last start must NOT be pulled to the top of the band here: the loop stopped
# because the list is full, not because the band ran out, so moving it would
# leap over everything in between and call the gap an overlap - and would
# leave MAX_LIST_ITEMS + 1 entries, which parse_list then refuses.
starts, log, text = stitch(0.05, 100000.0)      # 5128 needed, 2000 allowed
ok("a stitch too long for one sweep stops at the limit",
   len(starts) == S.MAX_LIST_ITEMS, str(len(starts)))
ok("... and says the top of the band is not covered",
   "nothing above" in log, " ".join(log.split())[-96:])
ok("... without pulling the last start to the top",
   starts[-1] < S.MAX_FREQ / 2, f"{starts[-1]:.1f} Hz")
ok("... and it still steps evenly the whole way",
   {round((starts[i + 1] - starts[i]) / 0.05)
    for i in range(len(starts) - 1)} == {390})
ok("... and the box it filled is one the sweep will accept",
   len(S.parse_list(text)) == S.MAX_LIST_ITEMS)
root.destroy()

# ---------------------------------------------------------------------------
# SPAN silently reinstalls the span's default OVLP (measured 31 Aug 2026 on
# s/n 41234). Every one of these used to pass a 98.44 % overlap off as the
# OVLP 0 that was asked for, and record_stats then quoted an error bar eight
# times better than the samples earned.
# ---------------------------------------------------------------------------

class OrderAn(FakeAn):
    """FakeAn holding a known OVLP, so what is under test is the shipped
    write_settings rather than a reimplementation of it. An empty snapshot is
    the analyzer that cannot answer OVLP? at all."""

    def __init__(self, held="98.44", readable=True):
        FakeAn.__init__(self, {"OVLP": held} if readable else {})


# SPANS stores the analyzer's own rounded display labels - 1560 Hz where the
# instrument is really 1562.5 - so codes 11-13 come out 0.16 % off the exact
# binary spans. That is immaterial everywhere record_time is used, but it means
# these cannot be asserted to the last digit.
MEASURED = {19: 0.0, 17: 0.0, 16: 50.0, 15: 75.0, 13: 93.75, 11: 98.44}
ok("default_overlap reproduces every measured span default",
   all(abs(S.default_overlap(c) - v) < 0.05 for c, v in MEASURED.items()),
   str({c: round(S.default_overlap(c), 2) for c in MEASURED}))

ok("... and clamps to zero once the record is already past 16 ms",
   S.default_overlap(19) == 0.0 and S.default_overlap(17) == 0.0
   and S.default_overlap(16) > 0,
   "span 19's default IS zero - which is why the 30 Aug set looked healthy")

an = OrderAn()
sent = an.write_settings({"OVLP": "0", "SPAN": "11"})
ok("OVLP asked for before SPAN is still sent after it",
   sent.index("SPAN 11") < sent.index("OVLP 0"), str(sent))
ok("... and OVLP is sent exactly once",
   sum(c.startswith("OVLP") for c in sent) == 1, str(sent))

an = OrderAn(held="98.44")
sent = an.write_settings({"SPAN": "11"})
ok("a SPAN-only write puts the held OVLP back after the span",
   sent == ["SPAN 11", "OVLP 98.44"], str(sent))

an = OrderAn(held="0.00")
sent = an.write_settings({"SPAN": "11", "NAVG": "100"})
ok("... and a zero it was holding is re-asserted too, not assumed",
   sent[0] == "SPAN 11" and sent[-1] == "OVLP 0.00", str(sent))

an = OrderAn()
sent = an.write_settings({"STRF": "10000"})
ok("STRF does not trigger a re-assert - the stitch is safe once span is set",
   sent == ["STRF 10000"], str(sent))

an = OrderAn(readable=False)
notes = []
sent = an.write_settings({"SPAN": "11"}, log=notes.append)
ok("an unreadable OVLP is skipped rather than guessed at",
   sent == ["SPAN 11"], str(sent))
ok("... and the log says the span default was left in place",
   any("could not be read back" in n for n in notes), " | ".join(notes))

ok("the protocol preset still carries OVLP with no SPAN of its own",
   "OVLP" in S.PRESETS["protocol"] and "SPAN" not in S.PRESETS["protocol"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nAll {checks} checks passed.")
