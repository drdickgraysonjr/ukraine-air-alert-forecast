# ukraine-air-alert-forecast

Forecasting air raid alerts in Ukraine by region, with a strict no-look-ahead
evaluation on the most recent data and comparison against naive forecasts.

Two questions are answered for each of 24 regions (23 oblasts and Kyiv City):

- **Phase 1, the coming day.** At 00:00 Kyiv time: how many alerts will start in
  the region during this calendar day, and how many hours will it spend under
  alert (clipped to 00:00-24:00)?
- **Phase 2, the next hours.** At any hour: if no alert is in progress, what is
  the probability that one starts within 1, 2, ... 6 hours? If an alert is
  already in progress, the answer is "alert in progress", not a probability.

Status: MVP on stitched historical data (see [Data](#data)). The live API feeds
(Ukraine Alert API, alerts.in.ua) are not wired in yet.

## Results

Rolling-origin backtest: the models are refit every 7 days on everything known
at the refit moment and forecast the following week. Two test windows are
reported separately, because the data changes meaning between them (see
[the break](#the-break-in-the-series)). Hyper-parameters were chosen on an
earlier validation window (2026-03-02 .. 2026-05-31) and not touched afterwards.

<!-- RESULTS:START -->
<!-- RESULTS:END -->

![Average alert hours per region and day](reports/fig_daily_hours.png)

![Phase 2 calibration](reports/fig_reliability.png)

## Data

| Source | Used for | Pinned commit | Licence |
|---|---|---|---|
| [Vadimkin/ukrainian-air-raid-sirens-dataset](https://github.com/Vadimkin/ukrainian-air-raid-sirens-dataset), `official_data_en.csv` (mirror of the official @air_alert_ua channel) | alerts that started before 2026-09-06 13:00 UTC | `bf0c323c78e3e4508721458797dcd38c95df13f3` | MIT |
| [JustJimer/ukraine-air-alert-stats](https://github.com/JustJimer/ukraine-air-alert-stats), `archive/alerts_in_ua.csv` (alerts.in.ua archive with red/yellow levels) | alerts that started at or after 2026-09-06 13:00 UTC | `7abc556ba5a0ff47a9e33c6abc136b8da3a9feb8` | none stated |

Both files are downloaded at the pinned commit by `alerts download` and checked
against `data_manifest.json` (sha256). Neither is committed: the alerts.in.ua
archive carries no licence, so this repository only redistributes code and
aggregate metrics.

### The break in the series

Three things happened to the alert history in late August and early September
2026, right inside the most recent period that the evaluation must cover:

1. The widely used volunteer dataset stopped tracking most alerts. In an
   exploratory check (outside this pipeline) it covered only about 21-22% of
   Kyiv City alert minutes after late August, measured against two independent
   references (Kyiv city open data and the alerts.in.ua archive). It is not used.
2. The official feed went silent: its last alert starts at 2026-09-07 05:28 UTC,
   and it has no alert starts at all on 2026-08-30 .. 09-01 (Kyiv time).
3. Two alert levels were introduced (yellow: drones, red: missiles). The first
   yellow alert in alerts.in.ua is at 2026-09-07 05:36 UTC, eight minutes after
   the official feed's last start.

What this project does about it:

- **Stitching.** Every alert is taken from exactly one source, chosen by its start
  time: the official feed before 2026-09-06 13:00 UTC, alerts.in.ua from then on.
  alerts.in.ua is complete only from about 2026-09-06 12:20 UTC (before that it
  holds just the alerts that were still active when collection started). On the
  16 hours where both sources are complete, alert minutes agree closely: the
  median Jaccard similarity over the 13 regions with alerts is 0.967, and 11 of
  13 are at 0.93 or above (`scripts/verify_stitch.py` recomputes this from the
  raw files). The exceptions are Zaporizhzhia (0.70) and Kherson (0.27):
  alerts.in.ua also issues alerts for occupied raions of Kherson oblast, which
  the official feed did not do in that window.
- **Excluded days.** 2026-08-29 .. 09-02 (the hole in the official feed and its
  partial edge days) and 2026-09-06 .. 09-07 (the seam) are neither trained on
  nor scored. Rolling features treat them as missing and are normalised by the
  number of valid hours in each window.
- **One alert definition across the break.** Yellow and red alerts are counted
  together, so that "an alert" keeps covering both drones and missiles, as the
  single-level system did. Even so, the series is not level across the break:
  summed over the 24 regions, the median day has 72 region alerts and 167 alert
  hours in June 1 - August 22, against 118 alerts and 240 hours in September 8 -
  October 10. These data cannot separate how much of the jump comes from the new
  yellow level, how much from the change of source, and how much from a real
  escalation.
- **Separate scores before and after.** Metrics are reported for a window before
  the break and for one after it, so a data change cannot hide inside an average.
  A `two_level_era` feature lets the model tell the periods apart once post-break
  weeks are in its training data.

### Region and alert definitions

- **An alert in a raion is an alert in its oblast.** Oblast- and raion-level
  alerts define the region's state: *none*, *part of the region* (some raions)
  or *whole region* (an oblast-level alert, or every raion at once). Raion names
  are matched across sources by the official 2010 transliteration standard, with
  three 2024 renames handled explicitly; all 118 raions match.
- **Hromada- and city-level alerts are left out.** In frontline communities they
  last for months (one hromada alert in Kharkivskyi raion started 2024-05-12 and
  lasted about 14,500 hours) and would mark the whole oblast as permanently under
  alert.
- **Crimea and Luhansk oblast are not modelled**: each has had one uninterrupted
  alert since 2022.
- **Counting alerts.** Overlapping alerts within a region are merged into one;
  alerts separated by at most 2 minutes count as one (raion hand-overs and level
  switches). An alert is counted on the day it starts, so a night alert crossing
  midnight counts once.

## No look-ahead

The task requires that nothing the model is trained on or uses as an input comes
from after the forecast moment. The main trap is in the data itself: an alert
appears in these files only after it has ended, with its finish time already
filled in.

- **Inputs.** Every feature at moment `t` is computed from alert starts at or
  before `t` and from the part of each alert that lies before `t`. An alert in
  progress at `t` is known only as "started at s, not over yet"; its duration is
  never used. The current state is a legitimate input because an alert is public
  the moment it is declared.
- **Labels.** Each training row carries the moment its label became known (the
  end of the day for phase 1, `t + h` for phase 2). A refit at moment `T` trains
  only on rows known by `T`.
- **Test.** `scripts/verify_leakage.py` recomputes all 44 features of all 24
  regions at 48 moments (random ones plus the seam, DST switches and the start of
  two-level alerts) from (a) the alert table as it was known at that moment and
  (b) a table with a rewritten future, and requires identical values. A
  deliberately leaky control feature (hours until the current alert ends) must
  be caught by the same comparison, and is.

## Models and baselines

- Phase 1: two gradient-boosted regressors with Poisson loss (scikit-learn
  `HistGradientBoostingRegressor`), one for the alert count and one for alert
  hours; predicted hours are clipped to the length of the day (23-25 h on DST days).
- Phase 2: one gradient-boosted classifier per horizon; probabilities are made
  non-decreasing in the horizon.
- Features: the region's state and history over 1 h .. 28 days, time since the
  last alert, the same clock time on previous days, neighbouring regions, the
  whole country, Kyiv calendar time, the two-level flag.
- Naive forecasts for phase 1: yesterday, the same weekday last week, means of
  the last 7 and 28 days. For phase 2: the frequency of alert starts in the same
  region and hour of day over the last 28 days (climatology), and a Poisson rate
  from the last 24 hours.

## How to run

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by uv).

```bash
uv sync
uv run alerts download     # pinned data -> data/raw/, checked against data_manifest.json
uv run alerts backtest     # both phases, both windows -> reports/metrics_phase{1,2}.json
uv run alerts report       # figures and the results tables in this README
uv run alerts forecast --as-of 2026-10-10T18:00+00:00
```

`uv run alerts tune` re-runs the hyper-parameter search on the validation window
(writes `reports/tuning.json`, which the backtest reads). The full backtest takes
about 20-30 minutes on a laptop.

Checks:

```bash
uv run pytest
uv run python scripts/verify_data.py       # pinned data matches the manifest
uv run python scripts/verify_stitch.py     # stitched series recomputed from raw files
uv run python scripts/verify_leakage.py    # no look-ahead, with a positive control
uv run python scripts/verify_backtest.py phase1
uv run python scripts/verify_backtest.py phase2
uv run python scripts/verify_forecast.py   # CLI state agrees with the raw data
uv run python scripts/verify_repro.py      # a fresh run reproduces reports/ byte for byte
uv run python scripts/verify_readme.py     # tables here equal reports/*.json
```

## Limitations

- **Stitched, not live.** Training and scoring use two archives joined at a seam.
  Neither archive is versioned as "what was published when": if a source corrected
  an alert later, the correction is in the data. The no-look-ahead guarantee is
  about alert timestamps, not about dataset revisions.
- **The post-break window is short** (about four weeks) and the model has seen
  only a few post-break weeks at each refit. Its intervals are wide.
- **Kherson oblast changes meaning at the seam** (occupied raions appear in
  alerts.in.ua), and Zaporizhzhia agrees only partly.
- **No external signals yet.** Air Force posts, monitoring channels and news are
  out of this MVP; adding them needs the same as-of discipline.
- **Not an early-warning system.** These are statistical forecasts for planning
  and analysis. Follow official alerts.

## Repository layout

```
src/alerts/     config, data loading, stitching, interval algebra, features,
                datasets, models, backtest, evaluation, forecast, report, CLI
scripts/        verification scripts used as acceptance gates
tests/          unit tests (intervals, three states, day bounds and DST, counting)
reports/        tuning and metric files, figures (generated)
```
