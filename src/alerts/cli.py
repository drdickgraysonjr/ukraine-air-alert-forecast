"""Command line: alerts {download,tune,backtest,forecast,report,all}."""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from . import config


def _data():
    from . import datasets, stitch

    return datasets.build(stitch.load_all(), config.DATA_END_UTC)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alerts", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download", help="fetch the pinned sources and write data_manifest.json")
    sub.add_parser("tune", help="choose hyper-parameters on the validation window")
    v = sub.add_parser("validate", help="validation-window trials for one decision -> reports/validation.json")
    v.add_argument("step", choices=["zones", "history", "text", "edited"])
    sub.add_parser("backtest", help="rolling-origin backtest of both phases -> reports/")
    f = sub.add_parser("forecast", help="forecast as of a moment, using only data known then")
    f.add_argument("--as-of", default=None, help="ISO time with offset, e.g. 2026-10-10T21:00+00:00 (default: data end)")
    f.add_argument("--json", default=None, help="also write the result as JSON to this path")
    sub.add_parser("report", help="figures and README tables from reports/*.json")
    sub.add_parser("all", help="download, backtest, report")
    a = ap.parse_args(argv)

    if a.cmd in ("download", "all"):
        from . import data

        data.download()
        print("data downloaded and verified against the manifest")
    if a.cmd == "validate":
        from . import validate

        getattr(validate, f"step_{a.step}")()
    if a.cmd == "tune":
        from . import tune

        tune.run(_data())
    if a.cmd in ("backtest", "all"):
        from . import run_backtest

        run_backtest.run(_data())
    if a.cmd in ("report", "all"):
        from . import report

        report.run()
    if a.cmd == "forecast":
        from . import forecast, stitch

        as_of = pd.Timestamp(a.as_of) if a.as_of else config.DATA_END_UTC
        if as_of.tzinfo is None:
            ap.error("--as-of needs a UTC offset")
        res = forecast.run(stitch.load_stitched(), as_of)
        print(forecast.render(res))
        if a.json:
            with open(a.json, "w") as fh:
                json.dump(res, fh, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
