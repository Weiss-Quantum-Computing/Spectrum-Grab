"""Fold a folder of per-segment captures into one set of files per sweep.

Spectrum Grab used to write a CSV, a plot and a metadata file for every segment
of a sweep. A 65-segment stitch left 199 files behind, and all of the data was
in the combined .npy matrices as well. It now writes one set for the whole
sweep; this brings a folder taken before that up to the same shape.

What it does NOT do is throw the old files away, and the reason is that they are
not redundant. The .npy matrices hold frequencies and amplitudes and nothing
else: the units, the input range, the averaging, the overlap and the settings
each segment was taken under exist only in the per-segment .txt files, and the
scale the trace is on exists only in the CSV headers. So the combined .csv and
.txt are built and verified first, and the per-segment files are then MOVED into
a `segments/` subfolder. Deleting them is a decision for whoever can see the
bench notes, not for this script.

    python tools/consolidate_sweep.py <folder>            # say what it would do
    python tools/consolidate_sweep.py <folder> --apply    # do it
"""
import argparse
import collections
import datetime
import os
import re
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sr760 import canonical_units, read_csv, safe_name    # noqa: E402

SEGMENT = re.compile(r"^(?P<head>.+?)_span\d+.*_(?P<date>\d{8})(?:_\d+)?$")


def scan(folder):
    """The per-segment captures in `folder`, grouped by title. A file that does
    not look like one a run wrote is left alone."""
    groups = collections.defaultdict(lambda: {"segments": {}, "date": ""})
    for name in sorted(os.listdir(folder)):
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext not in (".csv", ".txt", ".png") or "_sweep_" in stem:
            continue
        m = SEGMENT.match(stem)
        if not m:
            continue
        g = groups[m.group("head")]
        seg = g["segments"].setdefault(stem, {"stem": stem})
        seg[ext[1:]] = os.path.join(folder, name)
        span = re.search(r"_span(\d+)", stem)
        seg["span"] = span.group(1) if span else "?"
        g["date"] = m.group("date")
    return groups


def split_runs(segments):
    """One title's segments split into the separate sweeps that wrote them.

    A title swept twice on the same day leaves both sets of files in the folder,
    and the _1 that unique_base adds does NOT separate them: the second sweep
    only collides on the start frequencies the first one reached, so on
    2026-08-30 the fourteen segments of an aborted run and the fifty-two of the
    one that replaced it came out as fifty-two unsuffixed files and fourteen
    suffixed ones, mixed.

    What does separate them is that a sweep visits each (span, start frequency)
    once. In capture order, a pair that has already been seen means a new sweep
    has begun. That holds whatever the span is, which a gap in the clock does
    not - at the 191 mHz span one segment takes thirty-five minutes.
    """
    ordered = sorted(segments, key=lambda s: (s["captured"], s["start"]))
    runs, seen = [], None
    for seg in ordered:
        key = (seg["span"], round(seg["start"], 6))
        if seen is None or key in seen:
            runs.append([])
            seen = set()
        runs[-1].append(seg)
        seen.add(key)
    return runs


def read_header(path):
    """The `key : value` block at the top of a per-segment .txt, and the
    settings block underneath it, kept verbatim."""
    head, settings = {}, []
    try:
        text = open(path, encoding="utf-8").read()
    except Exception:
        return head, ""
    body, _, block = text.partition("analyzer settings")
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            head[key.strip()] = value.strip()
    if block:
        settings = ["analyzer settings" + block.rstrip()]
    return head, "\n".join(settings)


def load(segments, log=print):
    """Read each segment's trace. Returns (segments, ylabel) or (None, None)
    when they are not all on one scale, since those are not one measurement."""
    units = set()
    for seg in segments:
        try:
            seg["freqs"], seg["amps"], ylabel = read_csv(seg["csv"])
        except Exception as exc:
            log(f"    ! {os.path.basename(seg['csv'])}: {exc}")
            return None, None
        seg["start"] = float(seg["freqs"][0])
        units.add(canonical_units(ylabel))
    if len(units) > 1:
        log(f"    ! segments are on {len(units)} different scales "
            f"({', '.join(sorted(units))}) - left alone")
        return None, None
    return segments, units.pop()


def combine(base, head, segments, ylabel, apply_it, log=print):
    """One sweep's segments into one .csv and one .txt. Returns the paths it
    wrote (or would write) and the segment files it would move."""
    loaded = sorted(segments, key=lambda s: s["start"])
    csv_path, txt_path = base + ".csv", base + ".txt"

    freqs = np.concatenate([s["freqs"] for s in loaded])
    amps = np.concatenate([s["amps"] for s in loaded])
    seg = np.concatenate([np.full(len(s["freqs"]), i, dtype=float)
                          for i, s in enumerate(loaded, 1)])
    order = np.argsort(freqs, kind="stable")
    metas = [read_header(s.get("txt", "")) for s in loaded]

    if apply_it:
        np.savetxt(csv_path, np.column_stack([freqs[order], amps[order],
                                              seg[order]]),
                   delimiter=",", comments="",
                   header=f"Frequency (Hz),{safe_name(ylabel)},segment")
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(metadata(head, loaded, metas, ylabel))
        # Read it straight back: the point of the whole exercise is that the
        # combined file holds what the per-segment ones did.
        got_f, got_a, got_l = read_csv(csv_path)
        if (len(got_f) != len(freqs) or canonical_units(got_l) != ylabel
                or not np.allclose(got_f, freqs[order], rtol=0, atol=0)
                or not np.allclose(got_a, amps[order], rtol=0, atol=0,
                                   equal_nan=True)):
            raise SystemExit(f"{csv_path} did not read back identically - "
                             f"nothing has been moved, look at it by hand")
    movable = [s[k] for s in loaded for k in ("csv", "txt", "png") if k in s]
    return [csv_path, txt_path], movable


def metadata(head, loaded, metas, ylabel):
    """The combined .txt: what every segment agreed on once, what varied in a
    table, and the settings block of the last segment verbatim."""
    common, varying = {}, []
    keys = [k for k in metas[0][0]] if metas else []
    for k in keys:
        values = {m[0].get(k, "") for m in metas}
        if len(values) == 1:
            common[k] = values.pop()
        else:
            varying.append(k)
    freqs = np.concatenate([s["freqs"] for s in loaded])

    lines = [f"consolidated         : "
             f"{datetime.datetime.now().isoformat(timespec='seconds')} by "
             f"tools/consolidate_sweep.py",
             f"from                 : {len(loaded)} per-segment captures, "
             f"moved to segments/",
             f"sweep                : {head}",
             f"segments             : {len(loaded)}",
             f"trace units          : {ylabel}",
             f"frequency range (Hz) : {np.min(freqs):g} to {np.max(freqs):g}",
             ""]
    if common:
        lines.append("the same for every segment")
        for k, v in common.items():
            if k not in ("captured",):
                lines.append(f"  {k:<21}: {v}")
        lines.append("")
    lines.append("segments")
    cols = ["start frequency (Hz)", "stop frequency (Hz)"] + \
           [k for k in varying if k not in ("start frequency (Hz)",
                                            "stop frequency (Hz)")]
    lines.append("  " + "  ".join(["   #"] + [c[:20].rjust(20) for c in cols]))
    for i, (m, _rest) in enumerate(metas, 1):
        lines.append("  " + "  ".join([f"{i:>4}"]
                                      + [m.get(c, "-")[:20].rjust(20)
                                         for c in cols]))
    if metas and metas[-1][1]:
        lines += ["", "settings of the last segment, as it was written",
                  metas[-1][1]]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder")
    ap.add_argument("--apply", action="store_true",
                    help="write the combined files and move the segments")
    args = ap.parse_args()

    groups = scan(args.folder)
    if not groups:
        print(f"No per-segment captures in {args.folder}")
        return
    moved_dir = os.path.join(args.folder, "segments")
    total_moved = 0
    for head in sorted(groups):
        g = groups[head]
        segs = [s for s in g["segments"].values() if "csv" in s]
        print(f"\n{head}  ({g['date']})")
        print(f"  {len(segs)} segment(s)")
        if not segs:
            continue
        for s in segs:
            header, _ = read_header(s.get("txt", ""))
            s["captured"] = header.get("captured") or datetime.datetime\
                .fromtimestamp(os.path.getmtime(s["csv"])).isoformat()
        segs, ylabel = load(segs)
        if segs is None:
            continue

        used = set()
        for run in split_runs(segs):
            when = f"{run[0]['captured'][11:]} to {run[-1]['captured'][11:]}"
            if len(run) < 2:
                # One capture is not a sweep, and folding it into a "combined"
                # file of one segment would only rename it.
                print(f"  {len(run)} segment at {when} - a single capture, "
                      f"left alone")
                continue
            stem, n = f"{head}_sweep_{g['date']}", 0
            while (stem in used
                   or os.path.exists(os.path.join(args.folder, stem + ".csv"))
                   or os.path.exists(os.path.join(args.folder, stem + ".txt"))):
                n += 1
                stem = f"{head}_sweep_{g['date']}_{n}"
            used.add(stem)
            print(f"  {len(run)} segments captured {when} -> {stem}")
            written, movable = combine(os.path.join(args.folder, stem), head,
                                       run, ylabel, args.apply)
            for p in written:
                print(f"    {'wrote' if args.apply else 'would write'} "
                      f"{os.path.basename(p)}")
            if args.apply:
                os.makedirs(moved_dir, exist_ok=True)
                for p in movable:
                    shutil.move(p, os.path.join(moved_dir,
                                                os.path.basename(p)))
            print(f"    {'moved' if args.apply else 'would move'} "
                  f"{len(movable)} per-segment files to segments/")
            total_moved += len(movable)

    print(f"\n{'Moved' if args.apply else 'Would move'} {total_moved} files "
          f"in total.")
    if not args.apply:
        print("Nothing has been changed. Run again with --apply to do it.")


if __name__ == "__main__":
    main()
