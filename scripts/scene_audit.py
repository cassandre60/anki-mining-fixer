#!/usr/bin/env python3
"""Audit a whole selection for the defects the author keeps finding by hand.

Copy-free for the collection (it works on a temporary copy), read-only for
the profile, and general: it does not test named cards, it *measures* six
invariants over every card in scope, after applying a run to the copy.

    python3 scripts/scene_audit.py --profile-dir "<Anki profile dir>" --last 300

Invariants (a card fails if any is broken):
  A  every ``[sound:...]`` / ``<img src=...>`` file referenced exists;
  C  a non-empty ruby describes the sentence (base text matches);
  D  exactly one bold span, wrapping one of the card's own spellings
     (compared under width folding — a subtitle dump's half-width katakana
     still counts as the word);
  E  no block-level markup survives in the sentence fields (the .vtt/.srt
     ``<div>`` nesting is rewritten to ``<br>`` by the repair);
  F  **one run is the whole fix** (QUALITY Q-A2): the apply runs to a
     fixpoint exactly as the add-on does (re-plan on the real post-write
     state until a pass writes nothing), then the same scope is re-planned —
     the residual must be **0 rows**. Any row is work the fixpoint should
     have absorbed (a cross-run cascade or a misordered step). Release gate.
  G  **no fragment under borrowed audio** (QUALITY Q-A10): an independent
     referee over the post-run field values — no `build_plan` involved. A
     card whose `Sentence Audio` is byte-identical to a sibling's but whose
     `Sentence` is a strict fragment of that sibling's block was abandoned
     mid-fix. Class F cannot see it: once the audio is present the planner
     considers the card complete and reports zero residual rows, so the gate
     would referee itself. Ground truth, not planner opinion.

Output is a count per class with examples. Zero on every line means the
corpus is coherent; the author's reported cards would appear under B, C, D.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_audit_package import prepare_work_dir, records_from_collection  # noqa: E402

from mining_fixer.applier import NoteState, decide  # noqa: E402
from mining_fixer.batch import default_ops, select  # noqa: E402
from mining_fixer.matching import NoteRecord  # noqa: E402
from mining_fixer.normalize import furigana_base_matches, strip_renderable  # noqa: E402
from mining_fixer.plan import build_plan  # noqa: E402

SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
IMG_RE = re.compile(r"<img[^>]+src=[\"']?([^\"'>]+)", re.IGNORECASE)


def _is_kanji(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _shares_word_material(span: str, spelling: str) -> bool:
    """True when a bold span could be the word in another spelling.

    Sharing a kanji (日差し -> 日射し), a common prefix of two or more
    characters (淀む -> よどん keeps the よど stem), or containing one another
    (とんでもない> vs とんでもない) is evidence; a single shared kana is not
    (このたび vs 今回...). Pure.
    """
    if not span or not spelling:
        return False
    if span in spelling or spelling in span:
        return True
    if any(_is_kanji(char) and char in spelling for char in span):
        return True
    prefix = 0
    while prefix < min(len(span), len(spelling)) and span[prefix] == spelling[prefix]:
        prefix += 1
    return prefix >= 2


BOLD_RE = re.compile(r"<b>(.*?)</b>", re.DOTALL)


def _fragment_under_shared_audio(
    scope: list[int], fields: dict[int, dict[str, str]]
) -> dict[int, int]:
    """Independent referee for Q-A10: abandoned cards, keyed to their sibling.

    Reads only post-run field values — it never calls ``build_plan``, so it
    cannot inherit the planner's blind spot. A card that borrowed a sibling's
    scene audio but still carries a strict fragment of that sibling's
    sentence was left half-fixed: the audio fill landed while the guarded
    sentence expansion did not. Pure and cheap (one group-by, one substring
    check per shared-audio pair).
    """
    audio_to_notes: dict[str, list[int]] = collections.defaultdict(list)
    for note_id in scope:
        for ref in SOUND_RE.findall(fields[note_id].get("Sentence Audio") or ""):
            audio_to_notes[ref].append(note_id)

    abandoned: dict[int, int] = {}
    for note_id in scope:
        own = strip_renderable(fields[note_id].get("Sentence") or "")
        if not own:
            continue
        for ref in SOUND_RE.findall(fields[note_id].get("Sentence Audio") or ""):
            for sibling_id in audio_to_notes[ref]:
                if sibling_id == note_id or note_id in abandoned:
                    continue
                sibling = strip_renderable(fields[sibling_id].get("Sentence") or "")
                if sibling and sibling != own and own in sibling:
                    abandoned[note_id] = sibling_id
    return abandoned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/opencode/mb_audit"))
    parser.add_argument("--last", type=int, default=300)
    parser.add_argument(
        "--max-passes",
        type=int,
        default=4,
        help="passes one Apply may take to reach a fixpoint (Q-A2); a residual "
        "after this many is a failure, not a silent extra run",
    )
    parser.add_argument(
        "--engine",
        choices=("none", "fake"),
        default="none",
        help="simulate the reading engine (inside Anki only) so the ruby "
        "class can be measured; 'fake' annotates kanji runs, base text intact",
    )
    parser.add_argument(
        "--reuse-copy",
        action="store_true",
        help="audit the existing work dir instead of re-copying the profile "
        "(a frozen snapshot is reproducible; the live collection moves)",
    )
    args = parser.parse_args()

    if not args.reuse_copy:
        if args.work_dir.exists():
            shutil.rmtree(args.work_dir)
        prepare_work_dir(args.profile_dir, args.work_dir)
    from anki.collection import Collection
    from anki.notes import NoteId

    col = Collection(str(args.work_dir / "collection.anki2"))
    cfg = json.loads((REPO_ROOT / "mining_fixer" / "config.json").read_text(encoding="utf-8"))
    media_dir = args.work_dir / "collection.media"

    def media_exists(name: str) -> bool:
        return (media_dir / name).exists()

    try:
        records = records_from_collection(col, cfg["deck_name"], cfg["note_type_name"])
        scope = [r.note_id for r in sorted(records, key=lambda r: r.note_id, reverse=True)]
        scope = scope[: args.last]

        def fake_engine(sentence: str) -> str | None:
            """Stand-in for Japanese Support.

            Annotates the first kanji run; a kana-only sentence comes back
            unchanged, exactly as MeCab does (base text intact either way), so
            a kana-only card is not reported as an engine failure.
            """
            match = re.search(r"[\u4e00-\u9fff]+", sentence or "")
            if match is None:
                return sentence or None
            return (
                sentence[: match.start()]
                + match.group(0)
                + "["
                + match.group(0)
                + "]"
                + sentence[match.end() :]
            )

        def state(note_id: int) -> NoteState:
            note = col.get_note(NoteId(note_id))
            return NoteState(fields=dict(note.items()), tags=frozenset(note.tags))

        # One Apply is the whole fix (QUALITY Q-A2): the add-on re-plans on the
        # real post-write state until a pass writes nothing. The audit mirrors
        # that loop here, so class F measures the same thing the user sees.
        generator = fake_engine if args.engine == "fake" else None
        from mining_fixer.collection_io import japanese_support_morph_tokenizer

        morph_tokenizer = japanese_support_morph_tokenizer(col)
        for _pass in range(args.max_passes):
            current = records_from_collection(col, cfg["deck_name"], cfg["note_type_name"])
            plan = build_plan(
                current,
                cfg,
                scope_note_ids=scope,
                media_exists=media_exists,
                furigana_generator=generator,
                morph_tokenizer=morph_tokenizer,
            )
            selection = select(plan, default_ops(cfg))
            decision = decide(
                plan,
                [(w.note_id, w.field) for w in selection.rows],
                state,
                applied_tag=cfg.get("applied_tag") or None,
            )
            if not decision.field_writes and not decision.tags_writes:
                break
            for write in decision.field_writes:
                note = col.get_note(NoteId(write.note_id))
                note[write.field] = write.new_value
                col.update_note(note)
            for write in decision.tags_writes:
                note = col.get_note(NoteId(write.note_id))
                note.tags = list(write.new_tags)
                col.update_note(note)

        fields = {note_id: dict(col.get_note(NoteId(note_id)).items()) for note_id in scope}
        by_sentence: dict[str, list[int]] = collections.defaultdict(list)
        for note_id in scope:
            by_sentence[strip_renderable(fields[note_id].get("Sentence") or "")].append(note_id)

        # Class G: referee the *state*, not the planner's opinion of it. Class
        # F above can pass with zero rows while a card is still half-fixed,
        # because the borrowed audio hides the owed expansion from planning.
        abandoned = _fragment_under_shared_audio(scope, fields)

        violations: collections.Counter[str] = collections.Counter()
        examples: dict[str, list[str]] = collections.defaultdict(list)
        # Convergence gate (QUALITY Q-A2, "one run is the whole fix"): re-plan
        # the same scope on the updated copy — the second run must find zero
        # rows. Anything here is follow-up work the first run should have done
        # in the same pass (a cross-run cascade, or a step ordered after one it
        # depends on). This is a release gate, not a note.
        updated = records_from_collection(col, cfg["deck_name"], cfg["note_type_name"])
        plan2 = build_plan(
            updated,
            cfg,
            scope_note_ids=scope,
            media_exists=media_exists,
            furigana_generator=fake_engine if args.engine == "fake" else None,
            morph_tokenizer=morph_tokenizer,
        )
        second = select(plan2, default_ops(cfg))
        if second.rows:
            violations["F second run still has work (Q-A2)"] = len(second.rows)
            by_field = collections.Counter(w.field for w in second.rows)
            examples["F second run still has work (Q-A2)"].append(
                f"{len(second.rows)} rows on {len({w.note_id for w in second.rows})} "
                f"cards: {dict(by_field)}"
            )
        if abandoned:
            name = "G fragment sentence under borrowed audio (Q-A10)"
            violations[name] = len(abandoned)
            for note_id, sibling_id in list(abandoned.items())[:4]:
                word = (fields[note_id].get("Expression") or "")[:14]
                examples[name].append(f"{word}: audio of note {sibling_id}, fragment sentence")

        for note_id in scope:
            card = fields[note_id]
            sentence = card.get("Sentence") or ""
            ruby = card.get("Sentence (furigana)") or ""
            word = (card.get("Expression") or card.get("cloze-body") or "")[:14]

            refs = SOUND_RE.findall(sentence) + IMG_RE.findall(sentence)
            refs += [ref for value in card.values() for ref in SOUND_RE.findall(value)]
            if any(not media_exists(ref) for ref in refs):
                violations["A media file missing"] += 1
                examples["A media file missing"].append(word)

            if ruby.strip() and sentence.strip() and not furigana_base_matches(ruby, sentence):
                violations["C ruby does not match its sentence"] += 1
                base = re.sub(r"\[[^\[\]]*\]", "", re.sub(r"<[^>]+>", "", ruby))
                plain = re.sub(r"<[^>]+>", "", sentence)
                squeeze = lambda text: re.sub(r"\s+", "", text)  # noqa: E731
                if squeeze(base) != squeeze(plain):
                    violations["C base text differs from the sentence"] += 1
                else:
                    violations["C ruby present but unusable"] += 1
                examples["C ruby does not match its sentence"].append(
                    f"{word}: {squeeze(base)[:26]!r} vs {squeeze(plain)[:26]!r}"
                )

            from mining_fixer.plan import _bold_candidates, _fold, _is_morph_match

            spellings = {
                _fold(strip_renderable(s).strip())
                for s in _bold_candidates(
                    NoteRecord(note_id=note_id, notetype_id=0, fields=dict(card), tags=frozenset())
                )
            }
            spans = BOLD_RE.findall(sentence)
            folded_spans = [_fold(strip_renderable(span).strip()) for span in spans]
            if len(spans) > 1:
                violations["D bold doubled"] += 1
                examples["D bold doubled"].append(f"{word}: {spans[:2]}")
            elif folded_spans and not any(span in spellings for span in folded_spans):
                # A bold that shares a character with the word is that word in
                # a spelling the candidate set cannot enumerate without the
                # dictionary (淀む -> よどん, 日差し -> 日射し); one that shares
                # nothing is another card's word (このたび on 自負).
                is_morph = morph_tokenizer is not None and any(
                    _is_morph_match(
                        span,
                        card.get("Expression") or "",
                        card.get("reading") or "",
                        morph_tokenizer,
                        card.get("furigana"),
                    )
                    for span in spans
                )
                if not is_morph:
                    partial = any(
                        _shares_word_material(span, spelling)
                        for span in folded_spans
                        for spelling in spellings
                    )
                    name = (
                        "D bold on another spelling of the word"
                        if partial
                        else "D bold on a different word"
                    )
                    violations[name] += 1
                    examples[name].append(f"{word}: {spans[:2]}")

            # E: no block-level markup survives in the sentence fields — the
            # styled .vtt/.srt <div> nesting is rewritten to <br> by the
            # flatten-div-blocks repair (author's 淀む: 39 wrappers, four
            # visible lines).
            for field_name in ("Sentence", "Sentence (furigana)"):
                if "<div" in (card.get(field_name) or "").lower():
                    violations["E div markup in a sentence field"] += 1
                    examples["E div markup in a sentence field"].append(word)
                    break

        print(f"audited {len(scope)} cards after a run on a copy")
        if not violations:
            print("  no violations")
        for name, count in violations.most_common():
            print(f"  {count:4}  {name}  e.g. {examples[name][:4]}")
        return 1 if violations else 0
    finally:
        col.close()


if __name__ == "__main__":
    raise SystemExit(main())
