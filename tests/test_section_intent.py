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

    def test_markdown_headings_split_sections(self):
        """JSON-LD descriptions ship headings as markdown; "#" must not defeat segmentation."""
        jd = (
            "## Position Overview\n\n"
            "We build things for people.\n\n"
            "## Minimum Qualifications\n\n"
            "  * Strong Python.\n\n"
            "## Nice to Have\n\n"
            "  * Familiarity with Rust.\n"
        )
        labels = [s.label for s in si.jd_section_spans(jd)]
        assert labels == ["intro", "required", "preferred"]

    @pytest.mark.parametrize("prefix", ["#", "##", "###", "######"])
    def test_markdown_heading_prefix_is_stripped(self, prefix):
        assert si.normalize_heading(f"{prefix} Minimum Qualifications") == "minimum qualifications"
        assert si.classify_heading(f"{prefix} Minimum Qualifications", "jd") == "required"
        assert si.classify_heading(f"{prefix} Nice to Have", "jd") == "preferred"

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


class TestAtsHeaderChrome:
    """Issue #98: a portal metadata block must not become scored requirements.

    ADP/Workday pages render title, department, location, worker category, job class,
    posted-date badge, requisition number and the Apply/Save CTAs above the JD. Everything
    before the first heading is `required`, so that block used to be mined as requirements.
    """

    HEADER_JD = (
        "Principal Software Engineer\n"
        "Development\n"
        "Remote India, IN\n"
        "Full Time\n"
        "Professional\n"
        "24 days ago\n"
        "Requisition ID: 1427\n"
        "Apply\n"
        "Save\n"
        "\n"
        "The Desired Attributes:\n"
        "\n"
        "Bachelor's degree in computer science or a related field.\n"
        "Strong expertise in Ruby on Rails.\n"
        "Experience with Kafka preferred\n"
    )

    def test_metadata_block_before_the_first_heading_is_intro(self):
        spans = si.jd_section_spans(self.HEADER_JD)
        assert spans[0].label == "intro"
        chrome = self.HEADER_JD[spans[0].start:spans[0].end]
        assert "Requisition ID" in chrome
        assert "Full Time" in chrome
        assert "Ruby on Rails" not in chrome

    def test_the_heading_after_the_block_keeps_its_own_label(self):
        labels = [s.label for s in si.jd_section_spans(self.HEADER_JD)]
        assert labels[0] == "intro"
        assert "required" in labels

    def test_flat_jd_opening_with_a_skills_list_is_untouched(self):
        """The guard needs two chrome signals; a bare skills list has none."""
        jd = "Kubernetes\nDocker\nTerraform\n"
        assert [s.label for s in si.jd_section_spans(jd)] == ["required"]

    def test_a_single_chrome_like_line_is_not_enough(self):
        jd = "Apply\n\nWe build tools for engineers.\n"
        assert si.jd_section_spans(jd)[0].label == "required"

    def test_a_heading_is_never_swallowed_by_the_block(self):
        jd = "Full Time\nProfessional\nPreferred Qualifications:\nRust experience.\n"
        spans = si.jd_section_spans(jd)
        assert "preferred" in [s.label for s in spans]


class TestInlinePreferenceCue:
    def test_trailing_preferred_marks_only_that_requirement(self):
        """Issue #98: '... preferred' mid-list is nice-to-have, not must-have."""
        jd = (
            "Requirements:\n\n"
            "Strong expertise in Ruby on Rails.\n"
            "Experience with Kafka preferred\n"
            "We also value curiosity.\n"
        )
        assert [s.label for s in si.jd_section_spans(jd)] == [
            "required", "preferred", "required",
        ]

    def test_trailing_a_plus_is_detected(self):
        jd = "Requirements:\n\nRust experience a plus\n"
        assert "preferred" in [s.label for s in si.jd_section_spans(jd)]

    def test_mid_sentence_a_plus_still_does_not_switch_sections(self):
        jd = (
            "Requirements:\n\n"
            "Advanced degrees are a plus but not a substitute for shipping.\n"
        )
        assert [s.label for s in si.jd_section_spans(jd)] == ["required"]


class TestHeadingEmbeddingMemo:
    """The embedding fallback used to re-embed all 13 JD exemplars per unknown heading line.

    Scoring profiles showed ~5 s of a 6.8 s five-posting run inside the ONNX session, because
    every non-lexical line paid for the whole exemplar set. Headings repeat across a corpus, so
    both the exemplar vectors and the per-text label are memoised per embedding backend.
    """

    @pytest.fixture(autouse=True)
    def _clear_memo(self):
        """The memo is thread-local, so a leftover label could leak into a later test."""
        si.reset_heading_cache()
        yield
        si.reset_heading_cache()

    @staticmethod
    def _patch(monkeypatch, stamp=("test", "fake-model")):
        from backend import embeddings

        calls = {"n": 0}

        def fake_embed(text, settings=None):
            calls["n"] += 1
            return [1.0, 0.0, 0.0]

        monkeypatch.setattr(embeddings, "embed_text", fake_embed)
        monkeypatch.setattr(embeddings, "last_embed_stamp", lambda: stamp)
        return calls

    @staticmethod
    def _exemplar_count(domain="jd"):
        return sum(len(samples) for samples in si._EMBED_EXEMPLARS[domain].values())

    def test_exemplars_are_embedded_once_not_per_line(self, monkeypatch):
        calls = self._patch(monkeypatch)
        si.classify_heading("totally novel heading one", "jd")
        assert calls["n"] == self._exemplar_count() + 1

        si.classify_heading("totally novel heading two", "jd")
        # The second unknown line costs exactly one query vector, not the whole exemplar set.
        assert calls["n"] == self._exemplar_count() + 2

    def test_repeated_heading_costs_no_embedding(self, monkeypatch):
        calls = self._patch(monkeypatch)
        si.classify_heading("a heading nobody listed", "jd")
        before = calls["n"]
        si.classify_heading("a heading nobody listed", "jd")
        assert calls["n"] == before

    def test_memo_is_invalidated_by_a_backend_change(self, monkeypatch):
        calls = self._patch(monkeypatch, stamp=("standalone", "model-a"))
        si.classify_heading("a heading nobody listed", "jd")
        before = calls["n"]

        monkeypatch.setattr(
            "backend.embeddings.last_embed_stamp", lambda: ("standalone", "model-b")
        )
        si.classify_heading("a heading nobody listed", "jd")
        assert calls["n"] > before, "a different backend must not reuse the previous labels"

    def test_hash_backend_is_unknown_and_memoised(self, monkeypatch):
        calls = self._patch(monkeypatch, stamp=("hash", "hash"))
        assert si.classify_heading("a heading nobody listed", "jd") == "unknown"
        before = calls["n"]
        assert si.classify_heading("a heading nobody listed", "jd") == "unknown"
        assert calls["n"] == before

    def test_lexical_headings_never_reach_the_embedding_tier(self, monkeypatch):
        calls = self._patch(monkeypatch)
        assert si.classify_heading("Minimum Qualifications", "jd") == "required"
        assert calls["n"] == 0
