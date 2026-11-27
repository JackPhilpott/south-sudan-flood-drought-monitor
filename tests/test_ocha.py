"""Tests for reading OCHA's flood-affected counties. Invented spreadsheets only, no downloads."""

import pandas as pd
import pytest

from ssd_monitor import ocha

COUNTIES = pd.DataFrame(
    {
        "county_code": ["SS0306", "SS0307", "SS0303", "SS0704"],
        "county_name": ["Fangak", "Nyirol", "Bor South", "Luakpiny/Nasir"],
        "state_name": ["Jonglei", "Jonglei", "Jonglei", "Upper Nile"],
    }
)


def _sources(monkeypatch, tmp_path, frames: dict[ocha.Source, pd.DataFrame]):
    for source, frame in frames.items():
        frame.to_excel(tmp_path / source.file, index=False)
    monkeypatch.setattr(ocha, "SOURCES", tuple(frames))


def test_dtm_county_comes_from_the_payam_code(monkeypatch, tmp_path):
    dtm = ocha.Source(2019, "2019-10-29", "dtm.xlsx", "", "CC BY 4.0", "dtm_locations", partial=True)
    frame = pd.DataFrame(
        {
            "county_pcode": ["SS0306", "SS0307", "SS0306"],  # the first is miscoded: its payam is in Nyirol
            "Payam_Pcode": ["SS030705", "SS030706", "SS030601"],
            "idp_inds": [369, 870, 100],
        }
    )
    _sources(monkeypatch, tmp_path, {dtm: frame})
    table = ocha.reference_table(tmp_path, COUNTIES).set_index("county_code")
    assert table.loc["SS0307", "people"] == 369 + 870  # added up within the file
    assert table.loc["SS0306", "people"] == 100
    assert table["partial"].all()


def test_names_are_matched_and_snapshots_keep_the_larger_number(monkeypatch, tmp_path):
    first = ocha.Source(2021, "2021-10-01", "a.xlsx", "", "CC0", "names_2021")
    second = ocha.Source(2021, "2021-12-13", "b.xlsx", "", "CC0", "names_2021")

    def sheet(values):
        return pd.DataFrame(
            {
                "State": ["Jonglei", "Upper Nile", "Jonglei"],
                "County": ["Bor", "Nasir", "Fangak"],
                "Location of Flooding": None,
                "Month Flood Incident": None,
                "County Pop": None,
                "Affected People": values,
            }
        )

    _sources(monkeypatch, tmp_path, {first: sheet([100, 0, "n/a"]), second: sheet([80, 50, 0])})
    table = ocha.reference_table(tmp_path, COUNTIES).set_index("county_code")
    assert sorted(table.index) == ["SS0303", "SS0704"]  # Fangak never had people affected
    assert table.loc["SS0303", "people"] == 100  # the larger of the two snapshots
    assert "a.xlsx" in table.loc["SS0303", "sources"] and "b.xlsx" in table.loc["SS0303", "sources"]


def test_unknown_counties_are_an_error(monkeypatch, tmp_path):
    source = ocha.Source(2024, "2024-12-20", "c.xlsx", "", "CC0", "pcodes_2024")
    frame = pd.DataFrame({"Admin2_Pcode": ["SS9999"], "People_Affected": [10]})
    _sources(monkeypatch, tmp_path, {source: frame})
    with pytest.raises(ValueError, match="SS9999"):
        ocha.reference_table(tmp_path, COUNTIES)
