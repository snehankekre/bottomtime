"""Plot one canonical dive: both computers' depth profiles overlaid on a
shared clock (using the matcher's backfilled UTC start times), plus deco and
ppO2 panels when those channels are present.

matplotlib is an optional dependency: pip install 'bottomtime[plot]'.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .api import load_dive


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def _iter_source_entries(dive: dict):
    for source, value in dive["sources"].items():
        for entry in value if isinstance(value, list) else [value]:
            yield source, entry


def _nan(values: list) -> list[float]:
    return [float("nan") if v is None else float(v) for v in values]


def _has(entry: dict, channel: str) -> bool:
    return any(v is not None for v in entry["samples"].get(channel, []))


def aligned_series(dive: dict) -> list[dict]:
    """One dict per source log: label plus x (seconds since the canonical
    dive's start). Alignment uses each log's backfilled UTC start plus, for
    Shearwater logs, the matcher's residual clock skew (the wall clock's
    error beyond the whole-hour zone offset)."""
    t0 = _parse(dive["start_time_utc"])
    skew = {
        m["shearwater_source_dive_id"]: m["residual_skew_s"] or 0.0
        for m in dive.get("matches", [])
    }
    out = []
    for source, entry in _iter_source_entries(dive):
        start = entry["start_time_utc"] or dive["start_time_utc"]
        base = (_parse(start) - t0).total_seconds()
        if source == "shearwater":
            base += skew.get(entry["source_dive_id"], 0.0)
        model = entry["computer"].get("model") or source
        out.append(
            {
                "source": source,
                "label": model,
                "x": [base + t for t in entry["samples"]["t_s"]],
                "entry": entry,
            }
        )
    return out


def plot_dive(db_path: str | Path, dive_number: int, out: Path | None = None):
    dive = load_dive(db_path, dive_number)
    series = aligned_series(dive)

    try:
        import matplotlib

        if out:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise SystemExit(
            "bottomtime plot requires matplotlib: pip install 'bottomtime[plot]'"
        )

    panels = ["depth"]
    if any(_has(s["entry"], "gf99") for s in series):
        panels.append("gf99")
    ppo2_channels = ("sensor1_ppo2", "sensor2_ppo2", "sensor3_ppo2",
                     "avg_ppo2", "po2", "setpoint")
    if any(_has(s["entry"], c) for s in series for c in ppo2_channels):
        panels.append("ppo2")

    fig, axes = plt.subplots(
        len(panels), 1, sharex=True, figsize=(11, 2.8 * len(panels)), squeeze=False
    )
    axes = [ax for (ax,) in axes]
    ax_by_panel = dict(zip(panels, axes))

    ax = ax_by_panel["depth"]
    for s in series:
        samples = s["entry"]["samples"]
        x_min = [x / 60.0 for x in s["x"]]
        ax.plot(x_min, _nan(samples["depth_m"]), label=s["label"], linewidth=1.2)
        if _has(s["entry"], "ceiling_m"):
            ax.plot(
                x_min,
                _nan(samples["ceiling_m"]),
                linestyle="--",
                linewidth=0.9,
                label=f"{s['label']} ceiling",
            )
    ax.invert_yaxis()
    ax.set_ylabel("depth (m)")
    ax.legend(loc="lower right", fontsize=8)
    ax.grid(True, alpha=0.3)

    if "gf99" in ax_by_panel:
        ax = ax_by_panel["gf99"]
        for s in series:
            if _has(s["entry"], "gf99"):
                ax.plot(
                    [x / 60.0 for x in s["x"]],
                    _nan(s["entry"]["samples"]["gf99"]),
                    linewidth=1.0,
                    label=f"{s['label']} GF99",
                )
        ax.set_ylabel("GF99 (%)")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3)

    if "ppo2" in ax_by_panel:
        ax = ax_by_panel["ppo2"]
        for s in series:
            samples = s["entry"]["samples"]
            x_min = [x / 60.0 for x in s["x"]]
            for channel, style in (
                ("sensor1_ppo2", "-"),
                ("sensor2_ppo2", "-"),
                ("sensor3_ppo2", "-"),
                ("po2", "-"),
                ("setpoint", ":"),
            ):
                if _has(s["entry"], channel):
                    ax.plot(
                        x_min,
                        _nan(samples[channel]),
                        style,
                        linewidth=0.9,
                        label=f"{s['label']} {channel}",
                    )
        ax.set_ylabel("ppO2 (bar)")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("runtime (min)")
    depth = (
        f"max {dive['max_depth_m']:.1f} m"
        if dive["max_depth_m"] is not None
        else ""
    )
    fig.suptitle(
        f"dive {dive['dive_number']}  {dive['start_time_utc']} UTC  {depth}".rstrip()
    )
    fig.tight_layout()

    if out:
        fig.savefig(out, dpi=150)
        print(f"wrote {out}")
    else:
        plt.show()
    return fig
