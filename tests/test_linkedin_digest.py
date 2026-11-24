"""LinkedIn job-alert digest parsing (card-per-posting plain-text bodies).

Body shape copied from real digests; employers/roles are invented.
"""

from backend.gmail_client import ALERT_DIGEST_MAX_JOBS, is_linkedin_alert_sender
from backend.scraper import parse_linkedin_alert_cards

DIGEST = """Your job alert for Principal software Engineer in Bengaluru South
30+ new jobs match your preferences.

Senior Staff Software Engineer
Initech
Bengaluru
Top applicant
View job: https://www.linkedin.com/comm/jobs/view/111111111/

---------------------------------------------------------

Principal Engineer
Globex
Bengaluru
Easy Apply
View job: https://www.linkedin.com/comm/jobs/view/222222222/

---------------------------------------------------------

Staff Backend Engineer
Acme
Remote
View job: https://www.linkedin.com/comm/jobs/view/333333333/
"""


def test_digest_parses_every_card():
    jobs = parse_linkedin_alert_cards(DIGEST, "Initech is hiring: Senior Staff Software Engineer")
    assert [(j["company"], j["title"]) for j in jobs] == [
        ("Initech", "Senior Staff Software Engineer"),
        ("Globex", "Principal Engineer"),
        ("Acme", "Staff Backend Engineer"),
    ]
    assert jobs[0]["url"].startswith("https://www.linkedin.com/comm/jobs/view/111111111")
    assert jobs[2]["location"] == "Remote"
    assert all(j["source"] == "LinkedIn Job Alert" for j in jobs)


def test_digest_caps_the_postings_it_contributes():
    cards = "\n".join(
        f"Staff Engineer\nAcme{i}\nBengaluru\nView job: https://www.linkedin.com/comm/jobs/view/{i}\n"
        "\n---------------------------------------------------------\n"
        for i in range(7)
    )
    assert len(parse_linkedin_alert_cards(cards)) == ALERT_DIGEST_MAX_JOBS
    assert len(parse_linkedin_alert_cards(cards, max_jobs=2)) == 2


def test_digest_ignores_a_card_without_an_employer():
    body = """Principal Engineer
Bengaluru
Remote
View job: https://www.linkedin.com/comm/jobs/view/444444444/
"""
    assert parse_linkedin_alert_cards(body) == []


def test_digest_is_not_confused_with_a_message_notification():
    assert is_linkedin_alert_sender("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>")
    assert is_linkedin_alert_sender("LinkedIn <jobs-noreply@linkedin.com>")
    assert not is_linkedin_alert_sender("Recruiter <hit-reply@linkedin.com>")
    assert not is_linkedin_alert_sender("Recruiter <inmail-hit-reply@linkedin.com>")
