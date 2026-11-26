"""The Earth Engine exports this project needs, and which ones to start next. No Earth Engine here.

The heavy work runs as Earth Engine batch exports to assets, split into chunks of years so that each
task stays small and a failed one can be rerun on its own:

    rainfall_<years>      county-month rainfall table
    ndvi_<years>          county-month NDVI table
    flood_layers_<year>   one packed flood band per month, plus months flooded (the expensive part)
    flood_table_<year>    county-month flooded and observed areas, read from that year's layers

`submit` is safe to run again: it skips exports that are done or running, and starts each
flood_table once its layers asset exists.

`cost-test` runs one month of each export first (names starting with cost_test_), so the Earth
Engine cost of the full run can be estimated from measured EECU-seconds before committing to it.
"""

from collections import Counter
from dataclasses import dataclass, field

from ssd_monitor.config import Config
from ssd_monitor.dates import months_between, months_in_years, year_chunks

TABLE_KINDS = ("rainfall", "ndvi", "flood_table")
COST_TEST_PREFIX = "cost_test_"


@dataclass(frozen=True)
class Job:
    name: str
    kind: str
    first_year: int
    last_year: int
    needs: tuple[str, ...] = ()  # assets that must exist before this job can start
    months: tuple[str, ...] = ()  # set only for cost-test jobs, which cover a single month


@dataclass
class Plan:
    start: list[Job] = field(default_factory=list)
    waiting: list[Job] = field(default_factory=list)
    running: list[Job] = field(default_factory=list)
    done: list[Job] = field(default_factory=list)


def _years(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}_{last}"


def fetch_years(cfg: Config) -> dict[str, tuple[int, int]]:
    """The years each indicator needs: its baseline plus the monitoring period."""
    return {
        "rainfall": (
            min(cfg.rainfall.normal_years[0], cfg.start_year),
            max(cfg.rainfall.normal_years[1], cfg.end_year),
        ),
        "ndvi": (
            min(cfg.vegetation.baseline_years[0], cfg.start_year),
            max(cfg.vegetation.baseline_years[1], cfg.end_year),
        ),
        "flood": (cfg.start_year, cfg.end_year),
    }


def plan_jobs(cfg: Config) -> list[Job]:
    """Every export the configuration needs, in the order they should start."""
    years = fetch_years(cfg)
    jobs = [
        Job(f"rainfall_{_years(a, b)}", "rainfall", a, b)
        for a, b in year_chunks(*years["rainfall"], cfg.rainfall.years_per_task)
    ]
    jobs += [
        Job(f"ndvi_{_years(a, b)}", "ndvi", a, b)
        for a, b in year_chunks(*years["ndvi"], cfg.vegetation.years_per_task)
    ]
    for a, b in year_chunks(*years["flood"], cfg.flood.years_per_task):
        layers = f"flood_layers_{_years(a, b)}"
        jobs.append(Job(layers, "flood_layers", a, b))
        jobs.append(Job(f"flood_table_{_years(a, b)}", "flood_table", a, b, needs=(layers,)))
    return jobs


def select_flood_years(jobs: list[Job], first: int, last: int) -> list[Job]:
    """Keep every non-flood job, and the flood jobs that fall within first..last."""
    return [
        j for j in jobs if not j.kind.startswith("flood") or (j.first_year >= first and j.last_year <= last)
    ]


def job_months(job: Job, cfg: Config) -> list[str]:
    """The months a job covers. Flood jobs stop at the edges of the monitoring period."""
    if job.months:
        return list(job.months)
    months = months_in_years(job.first_year, job.last_year)
    if job.kind in ("flood_layers", "flood_table"):
        period = set(months_between(cfg.start_month, cfg.end_month))
        months = [m for m in months if m in period]
    return months


def next_steps(jobs: list[Job], done: set[str], active: set[str]) -> Plan:
    """Sort jobs into done, running, ready to start, and waiting for another job to finish."""
    plan = Plan()
    for job in jobs:
        if job.name in done:
            plan.done.append(job)
        elif job.name in active:
            plan.running.append(job)
        elif all(need in done for need in job.needs):
            plan.start.append(job)
        else:
            plan.waiting.append(job)
    return plan


# --- Cost test ---------------------------------------------------------------------------


def cost_test_jobs(month: str) -> list[Job]:
    """One-month versions of every kind of export."""
    tag, year = month.replace("-", "_"), int(month[:4])
    layers = f"{COST_TEST_PREFIX}flood_layers_{tag}"
    return [
        Job(f"{COST_TEST_PREFIX}rainfall_{tag}", "rainfall", year, year, months=(month,)),
        Job(f"{COST_TEST_PREFIX}ndvi_{tag}", "ndvi", year, year, months=(month,)),
        Job(layers, "flood_layers", year, year, months=(month,)),
        Job(
            f"{COST_TEST_PREFIX}flood_table_{tag}",
            "flood_table",
            year,
            year,
            needs=(layers,),
            months=(month,),
        ),
    ]


def months_per_kind(cfg: Config) -> dict[str, int]:
    """How many months each kind of export covers in the full run."""
    counts: Counter[str] = Counter()
    for job in plan_jobs(cfg):
        counts[job.kind] += len(job_months(job, cfg))
    return dict(counts)


def estimate_full_run(measured_s: dict[str, float], cfg: Config) -> dict[str, float]:
    """Scale the measured EECU-seconds of each one-month export kind to the full run, in EECU-hours."""
    counts = months_per_kind(cfg)
    return {kind: seconds * counts[kind] / 3600 for kind, seconds in measured_s.items()}
