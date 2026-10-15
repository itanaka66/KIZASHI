"""Tests for kizashi/models/logpattern.py (Drain tree)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kizashi.models.logpattern import DrainTree, LogTemplate, _WILDCARD


# ─────────────────────────────── helpers ───────────────────────────────────

BASE_TS = 1_700_000_000.0


def _add_many(tree: DrainTree, lines: list[str], ts: float = BASE_TS) -> list[LogTemplate]:
    return [tree.add(line, ts) for line in lines]


# ─────────────────────────────── unit tests ────────────────────────────────

class TestTokenize:
    """DrainTree._tokenize normalises variable tokens before grouping."""

    def test_ip_addresses_masked(self):
        tree = DrainTree()
        tokens = tree._tokenize("Connection from 192.168.1.1 to 10.0.0.1")
        assert "<IP>" in tokens
        assert "192.168.1.1" not in tokens

    def test_numbers_masked(self):
        tree = DrainTree()
        tokens = tree._tokenize("interface Gi0/0 errors 42")
        assert "<NUM>" in tokens
        assert "42" not in tokens

    def test_mac_masked(self):
        tree = DrainTree()
        tokens = tree._tokenize("MAC aa:bb:cc:dd:ee:ff learned on port Gi0/1")
        assert "<MAC>" in tokens

    def test_hex_masked(self):
        tree = DrainTree()
        tokens = tree._tokenize("Timer 0x1a2b expired")
        assert "<HEX>" in tokens

    def test_plain_words_unchanged(self):
        tree = DrainTree()
        tokens = tree._tokenize("BGP session established")
        assert tokens == ["BGP", "session", "established"]


class TestTemplateClustering:
    def test_identical_lines_same_template(self):
        tree = DrainTree()
        t1 = tree.add("BGP session established", BASE_TS)
        t2 = tree.add("BGP session established", BASE_TS)
        assert t1.id == t2.id

    def test_variable_token_wildcarded(self):
        # BGP peer state lines differ only in the last token (Established vs Idle).
        # After preprocessing both IPs become <IP>, so token 2 is identical.
        # Token 3 differs → gets wildcarded after merge.
        tree = DrainTree()
        t1 = tree.add("BGP peer 10.0.0.1 state Established", BASE_TS)
        t2 = tree.add("BGP peer 10.0.0.2 state Idle", BASE_TS)
        assert t1.id == t2.id
        assert _WILDCARD in t1.tokens  # "Established" vs "Idle" position wildcarded

    def test_different_prefix_different_template(self):
        tree = DrainTree()
        t1 = tree.add("BGP session established with 10.0.0.1", BASE_TS)
        t2 = tree.add("OSPF neighbor down on 10.0.0.2", BASE_TS)
        assert t1.id != t2.id

    def test_count_increments(self):
        tree = DrainTree()
        t = tree.add("BGP session established", BASE_TS)
        assert t.count == 1
        tree.add("BGP session established", BASE_TS)
        assert t.count == 2

    def test_different_lengths_different_templates(self):
        tree = DrainTree()
        t1 = tree.add("link up", BASE_TS)
        t2 = tree.add("link up Gi0/0", BASE_TS)
        assert t1.id != t2.id

    def test_all_wildcard_template_still_groups(self):
        """Lines with the same structure but all-variable tokens group together."""
        tree = DrainTree()
        t1 = tree.add("Xfrm 192.168.1.1 abc 100", BASE_TS)
        t2 = tree.add("Xfrm 10.0.0.1 xyz 200", BASE_TS)
        assert t1.id == t2.id

    def test_low_threshold_is_more_permissive(self):
        """With a very low threshold almost all lines of the same length cluster together."""
        tree = DrainTree(sim_threshold=0.1)
        lines = [
            "A B C D",
            "A X Y Z",
            "A P Q R",
        ]
        ids = {tree.add(line, BASE_TS).id for line in lines}
        assert len(ids) == 1  # all merged into one template

    def test_high_threshold_keeps_separate(self):
        """With threshold=1.0 lines that differ in a non-numeric token don't merge."""
        tree = DrainTree(sim_threshold=1.0)
        # After preprocessing: ["BGP", "state", "Established"] vs ["BGP", "state", "Idle"]
        # Similarity = 2/3 < 1.0 → separate templates
        t1 = tree.add("BGP state Established", BASE_TS)
        t2 = tree.add("BGP state Idle", BASE_TS)
        assert t1.id != t2.id


class TestNewTemplateDetection:
    def test_is_new_true_on_first_occurrence(self):
        tree = DrainTree()
        tmpl = tree.add("BGP session established", BASE_TS)
        assert tmpl.is_new is True

    def test_mark_known_clears_flag(self):
        tree = DrainTree()
        tmpl = tree.add("BGP session established", BASE_TS)
        tree.mark_known()
        assert tmpl.is_new is False

    def test_iter_new_after_snapshot(self):
        tree = DrainTree()
        tree.add("BGP session established", BASE_TS)
        tree.mark_known()

        # Add a new pattern after the snapshot
        new_tmpl = tree.add("OSPF neighbor down on 10.0.0.1", BASE_TS)
        # Revisit old pattern — not new
        tree.add("BGP session established", BASE_TS)

        new_ones = list(tree.iter_new())
        assert len(new_ones) == 1
        assert new_ones[0].id == new_tmpl.id

    def test_existing_pattern_after_snapshot_is_not_new(self):
        tree = DrainTree()
        tree.add("link flap detected on Gi0/0", BASE_TS)
        tree.mark_known()
        tmpl = tree.add("link flap detected on Gi0/1", BASE_TS)
        # Same template (wildcard merged) — not new
        assert tmpl.is_new is False

    def test_iter_new_empty_when_all_known(self):
        tree = DrainTree()
        tree.add("BGP session established", BASE_TS)
        tree.mark_known()
        tree.add("BGP session established", BASE_TS)
        assert list(tree.iter_new()) == []


class TestEviction:
    def test_max_clusters_respected(self):
        tree = DrainTree(max_clusters=5)
        for i in range(10):
            tree.add(f"UniqueWord{i} something happened", float(i))
        assert len(tree.templates) <= 5


class TestSerialization:
    def test_to_dict_from_dict_roundtrip(self):
        tree = DrainTree(sim_threshold=0.6)
        lines = [
            "BGP session established with 10.0.0.1",
            "BGP session established with 10.0.0.2",
            "OSPF neighbor down on Gi0/0",
        ]
        for line in lines:
            tree.add(line, BASE_TS)

        restored = DrainTree.from_dict(tree.to_dict())

        # Same number of templates
        assert len(restored.templates) == len(tree.templates)

        # Adding the same line should map to the same template id
        new_tmpl = restored.add("BGP session established with 192.168.1.1", BASE_TS + 1)
        orig_tmpl = tree.add("BGP session established with 172.16.0.1", BASE_TS + 1)
        assert new_tmpl.tokens == orig_tmpl.tokens

    def test_save_and_load(self, tmp_path: Path):
        tree = DrainTree()
        tree.add("BGP session established", BASE_TS)
        tree.add("interface Gi0/0 down", BASE_TS)
        tree.mark_known()

        save_path = tmp_path / "drain.json"
        tree.save(save_path)
        assert save_path.exists()

        loaded = DrainTree.load(save_path)
        assert len(loaded.templates) == len(tree.templates)

        # Loaded tree should not have new templates for lines it already saw
        t = loaded.add("BGP session established", BASE_TS + 1)
        assert t.is_new is False

    def test_save_is_atomic(self, tmp_path: Path):
        """Save should not leave a partial file if interrupted (tmp then replace)."""
        tree = DrainTree()
        tree.add("test line one", BASE_TS)
        path = tmp_path / "drain.json"
        tree.save(path)
        content_before = path.read_text()

        tree.add("test line two", BASE_TS)
        tree.save(path)

        # File should be valid JSON after second save
        import json
        json.loads(path.read_text())
        assert path.read_text() != content_before


class TestLogTemplateModel:
    def test_pattern_property(self):
        tmpl = LogTemplate(id=0, tokens=["BGP", _WILDCARD, "established"])
        assert tmpl.pattern == f"BGP {_WILDCARD} established"

    def test_to_dict_from_dict(self):
        tmpl = LogTemplate(
            id=5,
            tokens=["interface", _WILDCARD, "down"],
            count=3,
            first_seen=1.0,
            last_seen=2.0,
            is_new=False,
        )
        assert LogTemplate.from_dict(tmpl.to_dict()) == tmpl
