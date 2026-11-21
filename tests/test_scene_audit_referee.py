"""Class G: an independent referee over card state, not planner opinion.

The fixpoint gate (class F) asks `build_plan` whether any work remains. When
the 飲食 card's audio fill destroyed the expansion's trigger, the planner
honestly reported zero rows for a broken card. These tests exercise the
state-based check that does not import the planner at all.
"""

from __future__ import annotations

from test_golden_scene import (
    FOREIGN_BOLD_AUDIO,
    FOREIGN_BOLD_CHILD_ID,
    FOREIGN_BOLD_FRAGMENT,
    FOREIGN_BOLD_FULL_SENTENCE,
    FOREIGN_BOLD_PLAIN_SENTENCE,
    FOREIGN_BOLD_SIBLING_ID,
)

from scripts.scene_audit import _fragment_under_shared_audio

SCOPE = [FOREIGN_BOLD_SIBLING_ID, FOREIGN_BOLD_CHILD_ID]


def _fields(child_sentence: str, child_audio: str) -> dict[int, dict[str, str]]:
    return {
        FOREIGN_BOLD_SIBLING_ID: {
            "Expression": "無銭飲食",
            "Sentence": FOREIGN_BOLD_FULL_SENTENCE,
            "Sentence Audio": FOREIGN_BOLD_AUDIO,
        },
        FOREIGN_BOLD_CHILD_ID: {
            "Expression": "飲食",
            "Sentence": child_sentence,
            "Sentence Audio": child_audio,
        },
    }


def test_referee_flags_fragment_under_borrowed_audio():
    fields = _fields(FOREIGN_BOLD_FRAGMENT, FOREIGN_BOLD_AUDIO)
    assert _fragment_under_shared_audio(SCOPE, fields) == {
        FOREIGN_BOLD_CHILD_ID: FOREIGN_BOLD_SIBLING_ID
    }


def test_referee_is_quiet_once_the_expansion_landed():
    fields = _fields(FOREIGN_BOLD_PLAIN_SENTENCE, FOREIGN_BOLD_AUDIO)
    assert _fragment_under_shared_audio(SCOPE, fields) == {}


def test_referee_ignores_unrelated_audio():
    fields = _fields(FOREIGN_BOLD_FRAGMENT, " [sound:another-scene.webm] ")
    assert _fragment_under_shared_audio(SCOPE, fields) == {}
