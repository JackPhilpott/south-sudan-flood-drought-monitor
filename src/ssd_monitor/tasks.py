"""The Earth Engine exports this project needs, and which ones to start next. No Earth Engine here.

The heavy work runs as Earth Engine batch exports to assets, split into chunks of years so that each
task stays small and a failed one can be rerun on its own:

    rainfall_<years>         county-month rainfall table
    ndvi_<years>             county-month NDVI table
    s1_reference             dry-season radar reference image (the flood jobs read it)
    flood_table_<years>      county-month flooded and observed areas
    flood_frequency_<years>  per-pixel count of flooded months

`submit` is safe to run again: it skips exports that are done or running, and starts the flood
jobs once the reference exists.
"""

from dataclasses import dataclass, field

from ssd_monitor.config import Config
from ssd_monitor.dates import months_between, months_in_years, year_chunks

TABLE_KINDS = ("rainfall", "ndvi", "flood_table")


@dataclass(frozen=True)
class Job:
    name: str
    kind: str
    first_year: int | None = None
    last_year: int | None = None
    needs: tuple[str, ...] = ()


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
    jobs.append(Job("s1_reference", "s1_reference"))
    for a, b in year_chunks(*years["flood"], cfg.flood.years_per_task):
        jobs.append(Job(f"flood_table_{_years(a, b)}", "flood_table", a, b, needs=("s1_reference",)))
        jobs.append(Job(f"flood_frequency_{_years(a, b)}", "flood_frequency", a, b, needs=("s1_reference",)))
    return jobs


def job_months(job: Job, cfg: Config) -> list[str]:
    """The months a job covers. Flood jobs stop at the edges of the monitoring period."""
    months = months_in_years(job.first_year, job.last_year)
    if job.kind in ("flood_table", "flood_frequency"):
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
