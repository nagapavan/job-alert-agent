"""Tests for the shared section-heading intent primitive (Phase 2)."""
import pytest

from backend import section_intent as si
from backend.preference_filter import PreferenceCriteria, PreferenceFilter


class TestJdHeadingIntent:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("Required Experience and Skills", "required"),
            ("Minimum Qualifications", "required"),
            ("What you'll bring", "required"),
            ("What we're looking for", "required"),
            ("Preferred Experience", "preferred"),
            ("Preferred Qualifications", "preferred"),
            ("What sets you apart", "preferred"),
            ("Nice to have", "preferred"),
            ("About Us", "intro"),
            ("Who we are", "intro"),
            ("What We Can Offer You", "boilerplate"),
            ("Health & Wellbeing", "boilerplate"),
            ("Benefits", "boilerplate"),
            ("Key Responsibilities", "responsibilities"),
        ],
    )
    def test_classify(self, line, expected):
        assert si.classify_heading(line, "jd") == expected

    def test_mid_sentence_a_plus_is_not_preferred(self):
        """Regression: the phrase 'a plus' mid-sentence must NOT open a preferred section."""
        line = (
            "Advanced degrees are a plus but not a substitute for a record of "
            "shipping reliable AI-native applications."
        )
        assert si.classify_heading(line, "jd") == "unknown"
        assert si.starts_with_preference_cue(line) is False

    def test_ideally_at_line_start_is_a_preference_cue(self):
        assert si.starts_with_preference_cue("Ideally, you'll also have Rust experience.") is True


class TestResumeHeadingIntent:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("Work Experience", "experience"),
            ("Professional Experience", "experience"),
            ("Technical Skills", "skills"),
            ("Education", "education"),
            ("Projects", "projects"),
            ("Certifications", "certifications"),
        ],
    )
    def test_classify(self, line, expected):
        assert si.classify_heading(line, "resume") == expected


class TestJdSegmentation:
    def test_required_then_preferred_split(self):
        jd = (
            "Required experience and skills\n"
            "Python and Kubernetes.\n"
            "Preferred Experience\n"
            "Payroll and benefits domain depth."
        )
        spans = si.jd_section_spans(jd)
        labels = [s.label for s in spans]
        assert labels == ["required", "preferred"]
        assert "Python and Kubernetes" in jd[spans[0].start:spans[0].end]
        assert "Payroll and benefits" in jd[spans[1].start:spans[1].end]

    def test_preamble_defaults_to_required(self):
        jd = "We need Python and Go engineers.\n\nAbout Us: we are great."
        spans = si.jd_section_spans(jd)
        assert spans[0].label == "required"
        assert spans[-1].label == "intro"

    def test_mid_sentence_a_plus_does_not_start_preferred(self):
        jd = (
            "Requirements: Python required.\n"
            "Advanced degrees are a plus but not a substitute.\n"
        )
        labels = [s.label for s in si.jd_section_spans(jd)]
        assert "preferred" not in labels

    def test_mask_spans_preserves_offsets(self):
        jd = "About Us: 40-year old C++ code.\nWhat You Bring: Python."
        spans = si.jd_section_spans(jd)
        masked = si.mask_spans(jd, [s for s in spans if s.label == "intro"])
        assert len(masked) == len(jd)
        assert "C++" not in masked
        assert "Python" in masked

    def test_label_at(self):
        jd = "Requirements: Python.\nPreferred: Rust."
        spans = si.jd_section_spans(jd)
        assert si.label_at(spans, 0) == "required"
        assert si.label_at(spans, jd.index("Rust")) == "preferred"


class TestMustHavePolicy:
    def test_must_have_heading(self):
        assert si.is_must_have_heading("Requirements") is True
        assert si.is_must_have_heading("Minimum Qualifications") is True
        assert si.is_must_have_heading("Preferred Qualifications") is False

    def test_preference_filter_rejects_skip_topic_in_must_have(self):
        pf = PreferenceFilter(PreferenceCriteria(skip_topics=["vue"]))
        reason = pf.rejection_reason(
            "Software Engineer", "", "Requirements: Vue and React experience."
        )
        assert reason == "skip_topic"

    def test_preference_filter_ignores_skip_topic_in_preferred(self):
        pf = PreferenceFilter(PreferenceCriteria(skip_topics=["vue"]))
        reason = pf.rejection_reason(
            "Software Engineer", "", "Preferred: Vue experience is a bonus."
        )
        assert reason is None


class TestTypographicNormalisation:
    """Headings as JDs actually write them (issue #61).

    An unfolded curly apostrophe defeats the whole heading vocabulary, so the requirements
    body stays inside the preceding intro span and is masked out of scoring entirely.
    """

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("What you’ll be doing", "responsibilities"),      # U+2019
            ("What you‘ll be doing", "responsibilities"),      # U+2018
            ("What you’ll bring", "required"),
            ("What you’ll do", "responsibilities"),
            ("What we’re looking for", "required"),
            ("Nice to have", "preferred"),
        ],
    )
    def test_curly_apostrophes_classify_like_ascii(self, line, expected):
        assert si.classify_heading(line, "jd") == expected
        assert si.normalize_heading(line) == si.normalize_heading(
            line.replace("\u2019", "'").replace("\u2018", "'")
        )

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("💻 WHAT YOU’LL BE DOING", "responsibilities"),
            ("✨ WHAT YOU’LL BRING TO THE TEAM", "required"),
            ("⚒️ Benefits", "boilerplate"),
        ],
    )
    def test_leading_emoji_is_ignored(self, line, expected):
        assert si.classify_heading(line, "jd") == expected

    @pytest.mark.parametrize(
        "line, expected",
        [
            (".NET Developer", ".net developer"),
            ("C++ Engineer", "c++ engineer"),
            ("Node.js", "node.js"),
        ],
    )
    def test_leading_punctuation_that_belongs_to_a_name_survives(self, line, expected):
        assert si.normalize_heading(line) == expected

    def test_curly_apostrophe_heading_splits_the_jd(self):
        """A real JD shape: intro, then a curly-apostrophe requirements heading."""
        jd = (
            "About the job\n\nWe are Wolt, we deliver things.\n\n"
            "What you’ll be doing\n\nBuild backend services in Go and Kotlin.\n"
            "Own Kubernetes deployments and Kafka pipelines.\n\n"
            "What we offer\n\nA great team.\n"
        )
        spans = si.jd_section_spans(jd)
        labels = [s.label for s in spans]
        assert "responsibilities" in labels
        resp = [s for s in spans if s.label == "responsibilities"][0]
        body = jd[resp.start:resp.end]
        assert "Kubernetes" in body and "Kafka" in body
        # The intro must not swallow the requirements body any more.
        intro = [s for s in spans if s.label == "intro"][0]
        assert "Kubernetes" not in jd[intro.start:intro.end]

    def test_ascii_headings_are_unchanged(self):
        jd = "About us\n\nWe build tools.\n\nRequirements\n\nPython and Go.\n"
        assert [s.label for s in si.jd_section_spans(jd)] == ["intro", "required"]


class TestSingleLineSegmentation:
    """A JD flattened onto one line still gets section labels (issue #53).

    Sentence boundaries are secondary heading anchors, so a posting whose newlines were lost
    in transit no longer scores every preferred item as a must-have.
    """

    FLAT = (
        "About us we build things. Responsibilities Design systems. "
        "Minimum Qualifications Python and SQL. "
        "Preferred Qualifications Kubernetes is a plus."
    )

    def test_headings_without_colons_are_detected_mid_line(self):
        labels = [s.label for s in si.jd_section_spans(self.FLAT)]
        assert labels == ["required", "responsibilities", "required", "preferred"]

    def test_the_preferred_items_land_in_the_preferred_span(self):
        spans = si.jd_section_spans(self.FLAT)
        preferred = [s for s in spans if s.label == "preferred"]
        assert len(preferred) == 1
        assert "Kubernetes" in self.FLAT[preferred[0].start:preferred[0].end]
        assert si.label_at(spans, self.FLAT.index("Kubernetes")) == "preferred"

    def test_colon_separated_headings_are_detected_mid_line(self):
        jd = (
            "About us we build things. Responsibilities: Design systems. "
            "Minimum Qualifications: Python. Preferred Qualifications: Kubernetes."
        )
        labels = [s.label for s in si.jd_section_spans(jd)]
        assert "preferred" in labels
        assert si.label_at(si.jd_section_spans(jd), jd.index("Kubernetes")) == "preferred"

    def test_flattened_spans_cover_the_whole_text_without_overlap(self):
        spans = si.jd_section_spans(self.FLAT)
        assert spans[0].start == 0
        assert spans[-1].end == len(self.FLAT)
        for left, right in zip(spans, spans[1:]):
            assert left.end == right.start

    def test_flattening_never_loses_a_label(self):
        """The loose rule applies mid-line only, so flattening can add labels, never drop one.

        At a line start the strict rule stays in force: loosening it there reclassified 416 of
        951 stored descriptions, which is far outside this issue's scope.
        """
        multiline = self.FLAT.replace(". ", ".\n")
        assert set(s.label for s in si.jd_section_spans(multiline)) <= set(
            s.label for s in si.jd_section_spans(self.FLAT)
        )

    def test_a_line_start_is_never_loosened(self):
        """A prose line that merely begins with a heading word must not open a section."""
        jd = (
            "Requirements: Python.\n"
            "Preferred Qualifications Kubernetes is a plus, but we will train.\n"
        )
        spans = si.jd_section_spans(jd)
        # The line starts with "Preferred Qualifications" but carries content, so the strict
        # rule rejects it and the section does not flip.
        assert [s.label for s in spans] == ["required"]

    def test_a_longest_heading_phrase_wins_over_its_prefix(self):
        """"Preferred Qualifications" must not be read as a bare "Preferred"."""
        jd = "Minimum Qualifications Python. Preferred Qualifications Kubernetes."
        spans = si.jd_section_spans(jd)
        assert spans[-1].label == "preferred"
        assert "Preferred Qualifications" in jd[spans[-1].start:spans[-1].end]

    def test_a_line_start_heading_is_unaffected(self):
        """The existing strict rule still governs headings that own their line."""
        jd = "Requirements: Python.\nPreferred Qualifications\nKubernetes is a plus."
        labels = [s.label for s in si.jd_section_spans(jd)]
        assert labels == ["required", "preferred"]

    def test_ordinary_prose_does_not_open_a_section(self):
        jd = (
            "About us we build things. Kubernetes is a plus for this role. "
            "Minimum Qualifications Python."
        )
        # "Kubernetes is a plus" starts with a noun, not a heading phrase, so no new section
        # opens, and the restated "Minimum Qualifications" coalesces with the current one.
        labels = [s.label for s in si.jd_section_spans(jd)]
        assert labels == ["required"]
        assert "preferred" not in labels

    def test_a_heading_free_blob_stays_a_single_required_span(self):
        """Documented limitation: with no heading text there is nothing to anchor on.

        A Schema.org/JSON-LD blob that carries no heading words at all cannot be segmented
        from its own text, however it is split.
        """
        blob = " ".join(["We build software that helps teams ship faster."] * 40)
        spans = si.jd_section_spans(blob)
        assert [s.label for s in spans] == ["required"]


class TestLineBreaksDisableSentenceSegmentation:
    """Sentence segmentation is a fallback for flattened JDs only (issue #53).

    Loosening the rule for descriptions that still have line breaks reclassified hundreds of
    stored postings, so the line-based behaviour must stay exactly as it was.
    """

    def test_a_multi_sentence_line_is_not_split(self):
        jd = "Preferred Qualifications: Kubernetes. Requirements: Python.\n"
        spans = si.jd_section_spans(jd)
        assert [s.label for s in spans] == ["preferred"]
        assert spans[0].end == len(jd)

    def test_multi_line_spans_are_unchanged_by_the_flattened_path(self):
        jd = "Requirements: Python.\nPreferred Qualifications: Kubernetes.\n"
        spans = si.jd_section_spans(jd)
        assert [s.label for s in spans] == ["required", "preferred"]
        assert spans[0].start == 0
        assert spans[-1].end == len(jd)

    def test_flattening_the_same_text_adds_the_preferred_section(self):
        flat = "Requirements: Python. Preferred Qualifications: Kubernetes."
        labels = [s.label for s in si.jd_section_spans(flat)]
        assert "preferred" in labels
