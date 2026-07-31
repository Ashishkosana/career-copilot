"""Tests for pure job scoring + ranking."""
from copilot.domain import scoring
from copilot.domain.scoring import SWE_PROFILE


def test_score_rewards_profile_keywords_and_caps() -> None:
    strong = scoring.score("Software Engineer, Full Stack",
                           "Flutter mobile app on AWS Lambda + DynamoDB, Python backend")
    weak = scoring.score("Barista", "make coffee and greet customers")
    assert strong > weak
    assert scoring.score(" ".join(SWE_PROFILE.keywords), " ".join(SWE_PROFILE.keywords)) == 100


def test_title_gating() -> None:
    assert SWE_PROFILE.matches_title("Full Stack Engineer")
    assert not SWE_PROFILE.matches_title("Senior Software Engineer")  # blocklist
    assert not SWE_PROFILE.matches_title("Marketing Manager")         # must-include fails


def test_job_id_stable_and_short() -> None:
    a = scoring.job_id("https://x.com/1")
    assert a == scoring.job_id("https://x.com/1")
    assert a != scoring.job_id("https://x.com/2")
    assert len(a) == 16


def test_rank_filters_scores_dedups_and_orders() -> None:
    postings = [
        {"title": "Full Stack Engineer", "company": "Acme", "url": "https://j/1",
         "description": "Flutter, AWS Lambda, DynamoDB, Python, full stack"},
        {"title": "Backend Developer", "company": "Beta", "url": "https://j/2",
         "description": "Python REST API microservices"},
        {"title": "Staff Engineer", "company": "Gamma", "url": "https://j/3",
         "description": "Flutter AWS"},                        # senior blocklist
        {"title": "Marketing Manager", "company": "Delta", "url": "https://j/4",
         "description": "brand"},                              # title must-include fails
        {"title": "Junior Dev", "company": "Eps", "url": "https://j/5",
         "description": "make coffee"},                        # score too low
    ]
    ranked = scoring.rank(postings, min_score=30, limit=8)
    urls = [j.url for j in ranked]
    assert urls == ["https://j/1", "https://j/2"]
    assert ranked[0].score >= ranked[1].score  # ordered desc


def test_rank_respects_seen_and_limit() -> None:
    postings = [
        {"title": "Software Engineer", "company": "A", "url": "https://j/1"},
        {"title": "Software Engineer", "company": "B", "url": "https://j/2"},
    ]
    seen = frozenset({scoring.job_id("https://j/1")})
    ranked = scoring.rank(postings, min_score=10, seen=seen)
    assert [j.url for j in ranked] == ["https://j/2"]
    assert len(scoring.rank(postings, min_score=10, limit=1)) == 1


def test_rank_skips_incomplete_postings() -> None:
    postings = [
        {"title": "", "url": "https://j/1", "description": "engineer"},
        {"title": "Engineer", "url": "", "description": "engineer"},
    ]
    assert scoring.rank(postings, min_score=0) == []
