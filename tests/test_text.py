from job_search_assistant.text import content_hash, detect_language, html_to_text


def test_html_to_text_keeps_structure() -> None:
    assert html_to_text("<h2>Tasks</h2><ul><li>SQL</li><li>Python</li></ul>") == (
        "Tasks\n\n- SQL\n- Python"
    )


def test_html_to_text_handles_entity_escaped_html() -> None:
    escaped = (
        "&lt;p&gt;Tasks&lt;/p&gt;&lt;ul&gt;&lt;li&gt;SQL &amp;amp; Python&lt;/li&gt;&lt;/ul&gt;"
    )
    assert html_to_text(escaped) == "Tasks\n\n- SQL & Python"


def test_html_to_text_keeps_literal_angle_brackets_in_text() -> None:
    assert html_to_text("<p>salary &lt; 50k, x &gt; y</p>") == "salary < 50k, x > y"


def test_content_hash_ignores_gender_markers_case_and_punctuation() -> None:
    a = content_hash("Data Analyst (m/w/d)", "ACME GmbH", "Berlin")
    b = content_hash("data analyst (f/m/x)", "Acme GmbH.", " berlin ")
    c = content_hash("Data Analyst (w|m|d)", "ACME GmbH", "Berlin")
    assert a == b == c
    assert a != content_hash("Data Engineer (m/w/d)", "ACME GmbH", "Berlin")


def test_detect_language() -> None:
    assert detect_language("We are looking for a junior data analyst to join our team.") == "en"
    assert detect_language("Wir suchen einen Datenanalysten für unser Team in Berlin.") == "de"
