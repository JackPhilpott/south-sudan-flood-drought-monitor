"""Planning the Earth Engine exports."""

import dataclasses

from ssd_monitor.tasks import fetch_years, job_months, next_steps, plan_jobs


def test_fetch_years_cover_baselines_and_period(cfg):
    assert fetch_years(cfg) == {"rainfall": (1991, 2025), "ndvi": (2001, 2025), "flood": (2019, 2025)}


def test_plan_has_one_job_per_chunk(cfg):
    jobs = plan_jobs(cfg)
    names = [j.name for j in jobs]
    assert len(names) == len(set(names))
    kinds = [j.kind for j in jobs]
    assert kinds.count("rainfall") == 1 and names[0] == "rainfall_1991_2025"
    assert kinds.count("ndvi") == 5 and "ndvi_2001_2005" in names
    assert kinds.count("s1_reference") == 1
    assert kinds.count("flood_table") == 7 and "flood_table_2019" in names
    assert kinds.count("flood_frequency") == 7 and "flood_frequency_2025" in names


def test_only_flood_jobs_wait_for_the_reference(cfg):
    for job in plan_jobs(cfg):
        assert job.needs == (("s1_reference",) if job.kind.startswith("flood") else ())


def test_flood_job_months_stop_at_the_period(cfg):
    jobs = {j.name: j for j in plan_jobs(cfg)}
    short = dataclasses.replace(cfg, start_month="2019-06")
    assert job_months(jobs["flood_table_2019"], short)[0] == "2019-06"
    assert len(job_months(jobs["flood_table_2019"], cfg)) == 12
    # Rainfall always fetches whole years, because the normal needs them.
    assert len(job_months(jobs["rainfall_1991_2025"], short)) == 420


def test_next_steps_from_scratch(cfg):
    plan = next_steps(plan_jobs(cfg), done=set(), active=set())
    assert {j.kind for j in plan.start} == {"rainfall", "ndvi", "s1_reference"}
    assert {j.kind for j in plan.waiting} == {"flood_table", "flood_frequency"}
    assert not plan.running and not plan.done


def test_next_steps_after_the_reference_and_with_running_jobs(cfg):
    plan = next_steps(
        plan_jobs(cfg), done={"s1_reference", "rainfall_1991_2025"}, active={"flood_table_2019"}
    )
    assert [j.name for j in plan.running] == ["flood_table_2019"]
    assert {j.name for j in plan.done} == {"s1_reference", "rainfall_1991_2025"}
    started = {j.name for j in plan.start}
    assert "flood_frequency_2019" in started and "flood_table_2019" not in started
    assert not plan.waiting
