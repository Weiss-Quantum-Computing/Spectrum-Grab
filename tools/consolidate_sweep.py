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
    """The per-segment captures in `folder`, grouped by the sweep they belong
    to. A file that does not look like one a run wrote is left alone."""
    groups = collections.defaultdict(lambda: {"csv": [], "txt": [], "png": [],
                                              "date": ""})
    for name in sorted(os.listdir(folder)):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".csv", ".txt", ".png") or "_sweep_" in stem:
            continue
        m = SEGMENT.match(stem)
        if not m:
            continue
        g = groups[m.group("head")]
        g[ext.lower()[1:]].append(os.path.join(folder, name))
        g["date"] = m.group("date")
    return groups


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


def combine(folder, head, group, apply_it, log=print):
    """One sweep's segments into one .csv and one .txt. Returns the paths it
    wrote (or would write) and the segment files it would move."""
    csvs = group["csv"]
    if not csvs:
        return [], []
    loaded, units = [], set()
    for path in csvs:
        try:
            freqs, amps, ylabel = read_csv(path)
        except Exception as exc:
            log(f"    ! {os.path.basename(path)}: {exc}")
            return [], []
        loaded.append((path, freqs, amps))
        units.add(canonical_units(ylabel))
    if len(units) > 1:
        log(f"    ! {head}: segments are on {len(units)} different scales "
            f"({', '.join(sorted(units))}) - left alone")
        return [], []
    ylabel = units.pop()
    loaded.sort(key=lambda t: float(t[1][0]))          # by start frequency

    base = os.path.join(folder, f"{head}_sweep_{group['date']}")
    csv_path, txt_path = base + ".csv", base + ".txt"

    freqs = np.concatenate([f for _p, f, _a in loaded])
    amps = np.concatenate([a for _p, _f, a in loaded])
    seg = np.concatenate([np.full(len(f), i, dtype=float)
                          for i, (_p, f, _a) in enumerate(loaded, 1)])
    order = np.argsort(freqs, kind="stable")

    # Every per-segment .txt, so the combined one can say what varied.
    by_stem = {os.path.splitext(os.path.basename(p))[0]: p
               for p in group["txt"]}
    metas = [read_header(by_stem.get(
        os.path.splitext(os.path.basename(p))[0], "")) for p, _f, _a in loaded]

    if apply_it:
        np.savetxt(csv_path, np.column_stack([freqs[order], amps[order],
                                              seg[order]]),
                   delimiter=",", comments="",
                   header=f"Frequency (Hz),{safe_name(ylabel)},segment")
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(metadata(head, group, loaded, metas, ylabel))
        # Read it straight back: the point of the whole exercise is that the
        # combined file holds what the per-segment ones did.
        got_f, got_a, got_l = read_csv(csv_path)
        if (len(got_f) != len(freqs) or canonical_units(got_l) != ylabel
                or not np.allclose(got_f, freqs[order], rtol=0, atol=0)
                or not np.allclose(got_a, amps[order], rtol=0, atol=0,
                                   equal_nan=True)):
            raise SystemExit(f"{csv_path} did not read back identically - "
                             f"nothing has been moved, look at it by hand")
    return [csv_path, txt_path], group["csv"] + group["txt"] + group["png"]


def metadata(head, group, loaded, metas, ylabel):
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
    freqs = np.concatenate([f for _p, f, _a in loaded])

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
        print(f"\n{head}  ({g['date']})")
        print(f"  {len(g['csv'])} csv, {len(g['txt'])} txt, {len(g['png'])} png")
        written, movable = combine(args.folder, head, g, args.apply)
        if not written:
            continue
        for p in written:
            print(f"  {'wrote' if args.apply else 'would write'} "
                  f"{os.path.basename(p)}")
        if args.apply:
            os.makedirs(moved_dir, exist_ok=True)
            for p in movable:
                shutil.move(p, os.path.join(moved_dir, os.path.basename(p)))
        print(f"  {'moved' if args.apply else 'would move'} {len(movable)} "
              f"per-segment files to segments/")
        total_moved += len(movable)

    print(f"\n{'Moved' if args.apply else 'Would move'} {total_moved} files "
          f"in total.")
    if not args.apply:
        print("Nothing has been changed. Run again with --apply to do it.")


if __name__ == "__main__":
    main()
