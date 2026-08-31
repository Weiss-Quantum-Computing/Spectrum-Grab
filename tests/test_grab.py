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

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nAll {checks} checks passed.")
