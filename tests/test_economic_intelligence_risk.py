from radar.economic_intelligence import extract_relationship, match_relationship
from radar.research_risk import expectancy, risk_gate
import pytest


def test_direct_investment_has_both_parties_and_no_trade():
    r = extract_relationship("NVIDIA to invest $2 billion in Lumentum for optical infrastructure",
                             "https://example.com/source", "2026-11-13T10:00:00Z")
    assert r is not None
    assert (r.source_company, r.target_company) == ("NVIDIA", "Lumentum")
    assert r.amount_usd == 2_000_000_000
    assert r.decision == "RESEARCH_ONLY_NO_TRADE"


def test_acquisition_announcement_not_completion():
    r = extract_relationship("Marvell to acquire Lumentum for $10 billion",
                             "https://example.com/a", "2026-11-13T10:00:00+00:00")
    assert r.relation == "ACQUISITION_ANNOUNCED"


def test_no_inferred_beneficiaries():
    assert extract_relationship("Optical stocks may benefit from AI spending",
                                "https://example.com/a", "2026-11-13T10:00:00Z") is None


def test_ambiguous_ticker_does_not_pass():
    r = extract_relationship("NVIDIA invests $2 billion in Lumentum",
                             "https://example.com/a", "2026-11-13T10:00:00Z")
    row = match_relationship(r, lambda name: ("NVDA", "exact") if name == "NVIDIA" else ("LITE", "fuzzy"))
    assert row["source_ticker"] == "NVDA"
    assert row["target_ticker"] is None
    assert not row["validated_alpha"]


def test_timestamp_must_have_zone():
    with pytest.raises(ValueError):
        extract_relationship("NVIDIA invests $2 billion in Lumentum",
                             "https://example.com/a", "2026-11-13T10:00:00")


def test_net_expectancy_not_win_rate_alone():
    m = expectancy([2, 2, -1, -1, -1], roundtrip_cost_r=0.1)
    assert m.win_rate == 0.4
    assert m.net_expectancy_r == pytest.approx(0.1)
    assert m.status == "DESCRIPTIVE_ONLY_NOT_VALIDATED"


def test_unvalidated_alpha_blocks():
    r = risk_gate(entry=100, stop=95, target=110, quantity=5,
                  equity=10000, cash=10000)
    assert r["decision"] == "BLOCK"
    assert "UNVALIDATED_ALPHA" in r["reasons"]


def test_risk_and_exposure_limits():
    r = risk_gate(entry=100, stop=90, target=120, quantity=50,
                  equity=10000, cash=10000, validated_alpha=True)
    assert r["decision"] == "BLOCK"
    assert "POSITION_CAP" in r["reasons"]
    assert "RISK_CAP" in r["reasons"]
