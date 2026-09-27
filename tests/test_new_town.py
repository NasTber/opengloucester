"""A second town builds from its own config alone: sections it doesn't list are
left out, even when data for them is on disk, and every link still resolves."""

import copy

import pytest
from conftest import BUILT_AT
from test_site import parse
from test_site import test_internal_links_resolve as check_links

from pipeline import build_site
from pipeline.config import configured, load_config
from pipeline.seeclickfix import street_address

OPTIONAL = ["seeclickfix", "finance", "labor", "schools", "housing", "permits", "summaries", "freshness",
            "analytics", "participation", "glossary"]


def meetings_only(config: dict) -> dict:
    town = copy.deepcopy(config)
    for table in OPTIONAL:
        town.pop(table, None)
    town["slug"] = "newtown"
    town["site"].update(name="OpenNewtown", name_suffix="Newtown", domain="opennewtown.example",
                        masthead="An independent guide to city government in Newtown",
                        repo_url="https://github.com/example/opennewtown")
    town["town"]["name"] = "Newtown"
    town["sections"] = [s for s in town["sections"] if s["slug"] in ("meetings", "about")]
    return town


@pytest.fixture(scope="module")
def new_town_site(tmp_path_factory, data_dir, monkeypatch_module):
    town = meetings_only(load_config("gloucester"))
    monkeypatch_module.setattr(build_site, "load_config", lambda slug: town)
    out = tmp_path_factory.mktemp("newtown") / "site"
    build_site.build("newtown", out, data_dir=data_dir, now=BUILT_AT)
    return out


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def test_only_listed_sections_are_built(new_town_site):
    for slug in ("311", "schools", "budget", "housing"):
        assert not (new_town_site / slug).exists(), slug
    assert (new_town_site / "meetings" / "index.html").exists()
    assert (new_town_site / "about" / "index.html").exists()
    assert (new_town_site / "streets" / "index.html").exists()
    assert (new_town_site / "CNAME").read_text().strip() == "opennewtown.example"


def test_pages_name_the_new_town_only(new_town_site):
    home = (new_town_site / "index.html").read_text()
    assert "Newtown" in home
    assert "/311/" not in home and "tax bill" not in home.lower()
    about = (new_town_site / "about" / "index.html").read_text()
    assert "SeeClickFix" not in about and "Division of Local Services" not in about
    assert "goatcounter" not in about.lower()
    # The street lookup offers only what the town has.
    assert "Agenda items on any street" in home
    streets = (new_town_site / "streets" / "index.html").read_text()
    assert 'data-sources="meetings"' in streets and "311" not in streets


def test_gloucester_street_lookup_names_every_source(site_dir):
    assert "Agenda items, building permits, and 311 requests on any street" in (site_dir / "index.html").read_text()
    assert 'data-sources="meetings permits requests"' in (site_dir / "streets" / "index.html").read_text()


def test_new_town_links_resolve(new_town_site):
    check_links(new_town_site, sorted(new_town_site.rglob("*.html")))
    for path in new_town_site.rglob("*.html"):
        assert parse(path).tags.count("h1") == 1, path


def test_fetchers_skip_sources_the_town_does_not_have(capsys):
    town = meetings_only(load_config("gloucester"))
    assert configured(town, "meetings")
    assert not configured(town, "seeclickfix")
    assert "No [seeclickfix] in config/newtown.toml; skipping." in capsys.readouterr().out


def test_town_name_is_removed_from_addresses():
    assert street_address("12 Essex St Salem, Massachusetts, 01970", "Salem") == "12 Essex St"
    assert street_address("12 Essex St Salem MA 01970", "Salem") == "12 Essex St"
    # Another town's name in a street name is kept.
    assert street_address("5 Gloucester St Salem, MA", "Salem") == "5 Gloucester St"

