"""Planning the Earth Engine exports, and how the flood layers pack their flags."""

import dataclasses

from ssd_monitor.flood import FLAG_BITS, layer_band, unpack_flags
from ssd_monitor.tasks import (
    cost_test_jobs,
    estimate_full_run,
    fetch_years,
    job_months,
    months_per_kind,
    next_steps,
    plan_jobs,
    select_flood_years,
)


def test_fetch_years_cover_baselines_and_period(cfg):
    assert fetch_years(cfg) == {"rainfall": (1991, 2025), "ndvi": (2001, 2025), "flood": (2019, 2025)}


def test_plan_has_one_job_per_chunk(cfg):
    jobs = plan_jobs(cfg)
    names = [j.name for j in jobs]
    assert len(names) == len(set(names)) == 20
    kinds = [j.kind for j in jobs]
    assert kinds.count("rainfall") == 1 and names[0] == "rainfall_1991_2025"
    assert kinds.count("ndvi") == 5 and "ndvi_2001_2005" in names
    assert kinds.count("flood_layers") == 7 and "flood_layers_2019" in names
    assert kinds.count("flood_table") == 7 and "flood_table_2025" in names


def test_each_flood_table_waits_for_its_own_layers(cfg):
    for job in plan_jobs(cfg):
        if job.kind == "flood_table":
            assert job.needs == (f"flood_layers_{job.first_year}",)
        else:
            assert job.needs == ()


def test_flood_job_months_stop_at_the_period(cfg):
    jobs = {j.name: j for j in plan_jobs(cfg)}
    short = dataclasses.replace(cfg, start_month="2019-06")
    assert job_months(jobs["flood_layers_2019"], short)[0] == "2019-06"
    assert len(job_months(jobs["flood_table_2019"], cfg)) == 12
    # Rainfall always fetches whole years, because the normal needs them.
    assert len(job_months(jobs["rainfall_1991_2025"], short)) == 420


def test_select_flood_years_keeps_rainfall_and_ndvi(cfg):
    jobs = select_flood_years(plan_jobs(cfg), 2019, 2022)
    flood_years = {j.first_year for j in jobs if j.kind.startswith("flood")}
    assert flood_years == {2019, 2020, 2021, 2022}
    assert sum(j.kind in ("rainfall", "ndvi") for j in jobs) == 6
    assert len(jobs) == 14


def test_next_steps_from_scratch(cfg):
    plan = next_steps(plan_jobs(cfg), done=set(), active=set())
    assert {j.kind for j in plan.start} == {"rainfall", "ndvi", "flood_layers"}
    assert {j.kind for j in plan.waiting} == {"flood_table"}
    assert not plan.running and not plan.done


def test_next_steps_starts_a_table_once_its_layers_exist(cfg):
    plan = next_steps(plan_jobs(cfg), done={"flood_layers_2019"}, active={"flood_layers_2020"})
    assert [j.name for j in plan.running] == ["flood_layers_2020"]
    started = {j.name for j in plan.start}
    assert "flood_table_2019" in started and "flood_layers_2019" not in started
    assert "flood_table_2020" in {j.name for j in plan.waiting}


# --- flag packing ------------------------------------------------------------------------


def test_layer_band_names():
    assert layer_band("2020-09") == "m2020_09"


def test_flags_pack_into_distinct_bits():
    assert sorted(FLAG_BITS.values()) == [1, 2, 4, 8]
    packed = FLAG_BITS["flooded"] + FLAG_BITS["flooded_optical"] + FLAG_BITS["observed"]
    assert unpack_flags(packed) == {
        "flooded": True,
        "flooded_sar": False,
        "flooded_optical": True,
        "observed": True,
    }
    assert not any(unpack_flags(0).values())


# --- cost test ---------------------------------------------------------------------------


def test_cost_test_covers_each_kind_for_one_month(cfg):
    jobs = cost_test_jobs("2020-09")
    assert [j.kind for j in jobs] == ["rainfall", "ndvi", "flood_layers", "flood_table"]
    for job in jobs:
        assert job.name.startswith("cost_test_") and job.name.endswith("_2020_09")
        assert job_months(job, cfg) == ["2020-09"]
    assert jobs[3].needs == ("cost_test_flood_layers_2020_09",)


def test_months_per_kind(cfg):
    assert months_per_kind(cfg) == {"rainfall": 420, "ndvi": 300, "flood_layers": 84, "flood_table": 84}


def test_estimate_scales_one_month_to_the_full_run(cfg):
    measured = {"rainfall": 36.0, "ndvi": 60.0, "flood_layers": 360.0}
    estimate = estimate_full_run(measured, cfg)
    assert estimate == {"rainfall": 4.2, "ndvi": 5.0, "flood_layers": 8.4}
