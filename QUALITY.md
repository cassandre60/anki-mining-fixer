# Mining Fixer — Quality Rules (Invariants)

Answers: *what properties must remain true?*
`TEST_STRATEGY.md` answers how each one is mechanically verified.
An invariant may not be relaxed inside a coding session; change this file
first, with the reason.

## Data safety

- **Q-D1 — Fills only touch empty fields.** A field with any non-whitespace
  content is never written by a `fill` (`old_value == ""`); markup-only values
  like `<hr>` count as occupied. When `reading` is empty and no sibling fills it,
  the note's own `furigana` field serves as a self-contained oracle
  (`reading_from_furigana`, badge `reading-derived-from-furigana`), filling
  `reading` with the derived kana reading without needing sibling cards.
  `cloze-*` and `Definition` / `Extended
  definition` belong to `FIELD_NEVER_COPY` and are never copied from sibling
  cards; empty container markup in definition fields (`<ol></ol>`) is cleared
  solely by `strip_empty_glossary` (Q-D13).
- **Q-D2 — Eight approved non-empty writes, each information-preserving.**
  `replace_source` (blob URL is logged), `expand_sentence` (the card's mined
  fragment survives: the scene matcher proved it is contained in the new
  sentence — `cloze-*` holds it too on a sentence-mined card — Q-D16),
  `bold_cloze_word` (rendered text unchanged),
  `repair_sentence` (only configured noise classes change, Q-D10),
  `regenerate_furigana` (the ruby layer must still carry the sentence's
  characters, Q-D11), `repair_expression` (the new spelling is the note's own
  furigana base, cross-checked against its reading field, Q-D12; the old
  spelling survives in the run log, Q-D7), `strip_empty_glossary` (only a
  definition that renders as nothing, Q-D13), `add_tags` (superset only).
  `OPS_TOUCHING_NONEMPTY` is the single source of
  truth and a test asserts no other op writes a non-empty value.
- **Q-D3 — Bold is markup-only, word-like and unique.** A bold write is
  emitted only when `strip_renderable(new) == strip_renderable(old)`;
  verified per write. When an existing bold span wraps the tested word plus
  surrounding non-word punctuation or delimiters (e.g. `<b>とんでもない></b>`
  or `<b>芸者&gt;</b>`), the outer punctuation is trimmed outside the bold tags
  (`<b>とんでもない</b>>`, `<b>芸者</b>&gt;`), preserving the exact occurrence
  the existing bold already sits on even when the word repeats elsewhere in the
  sentence (badge `bold-trimmed-punctuation`). The word is found in five
  tiers, each requiring exactly one occurrence: the `cloze-body` (the mined
  word), the `Expression` verbatim, a reading-derived mixed-kana surface form
  (鬱病 → うつ病), furigana per-kanji spellings (類い希 → 類いまれ), or an
  unambiguous inflected / alternate-kanji form identified via morphological
  analysis (淀む → <b>よどん</b>, 日差し → <b>日射し</b>, 読む → <b>読め</b>;
  badge `bold-added-to-inflected-form`, config key `batch_bold_inflected`).
  Expression-derived tiers require the Expression to be
  *word-like* — no whitespace, no sentence punctuation, clause patterns
  rejected, and character count capped (`MAX_EXPRESSION_CHARS`, 25) while
  accepting legitimate long compound words like ベーコンチーズバーガー; a clause-like
  value (漢字は読めるようになりたいかな) is reported (`expression-not-word-like`),
  never bolded. A MeCab token-count oracle was tried on 2026-11-21 and
  rejected on deck evidence: it refused legitimate compound verbs and
  dictionary expressions (響き渡る, いろいろあって) while the deck's
  full-sentence Expressions are listening placeholders (B11), so the
  heuristic stays. Cards whose
  template front is audio-only are skipped entirely — the sentence is not
  displayed: either a `listening` tag with usable audio, or no
  `Definition`/`Extended definition`/`Frequency` with `Sentence Audio`
  present (`plan._is_listening`, verified against the template). Ambiguity
  — several occurrences or several candidates — is never resolved by
  guessing. The word is located under **width folding**: a subtitle dump can
  carry half-width katakana (`ｽﾍﾟｸﾄﾗﾑ`) where the mined word and its furigana
  are full-width (`スペクトラム`), so the match is made on the NFKC-folded
  text while the bold wraps the sentence's own characters (author's
  自閉症すぺくとらむ: the bold had stayed on another card's このたび).
  The ruby pairing (`normalize.furigana_base_matches`) deliberately does
  **not** fold: the ruby's base characters must render exactly what the
  `Sentence` shows.
- **Q-D4 — Sentence and Sentence (furigana) are atomic.** Both come from the
  same parent; a furigana fill paired with a guarded expansion ships
  unchecked (`stale-fragment-furigana-risk`). This *text unit* is also the
  batch's atomic unit: a batch either rewrites the sentence together with its
  ruby or leaves both alone. Audio, picture, translation and source do not
  depend on the sentence text, so they remain independent fills even when the
  sentence rewrite is not enabled.
- **Q-D5 — Tags only grow.** The tag write is a union with the live tag set:
  `new ⊇ live ⊇ old`. Reserved tags never propagate: `marked`, `leech`,
  `optout_tag` (`no-autofill`), `applied_tag` (`auto-filled`).
- **Q-D6 — No bookkeeping tags.** Audit/working state lives in deck/row
  structure and run logs, not in tag names the user did not ask for.
- **Q-D7 — Every write carries its old value in a run log.** Undo depends on
  it; the audit builder emits the same for dry runs.
- **Q-D8 — Junk is never a source.** Doubled-reading pitch-accent artefacts,
  attribution-only values, `<hr>`-only fields and placeholder values are
  excluded across all scene and word fills (including word-anchored scene
  recovery). Definitions are never copied at all (`FIELD_NEVER_COPY`).
- **Q-D9 — Media is verified on disk** before a media fill is offered; a
  missing file drops the row with a warning instead of writing a dead
  reference.
- **Q-D10 — Sentence repair touches only configured noise.** The
  `repair_sentence` op rewrites a note's own `Sentence` and nothing else,
  and only through the rules configured in `sentence_repair_rules`. The
  shipped rule set is `flatten-div-blocks` (a styled `.vtt`/`.srt` nests the
  sentence in empty `<div>` wrappers that render as the line breaks they
  stand for — 淀む: 39 wrappers for four visible lines; a block boundary
  becomes `<br>`, existing `<br>` are kept, whitespace between block tags is
  dropped as layout, and a final check refuses the rewrite if it would lose
  a visible character; owner addition 2026-11-17), `strip-arrows`
  (continuation arrows at line tails;
  mid-line glyphs such as `f:X→Y` are real notation and stay) and
  `join-speaker-lines` (a short speaker/stage tag that **opens** its line —
  ``(荒木)``, ``(葉子)`` — loses the line break that follows it; a short
  parenthetical at the end of a text line (``…知ってた?(test)<br>``) is
  not a speaker tag, so its break is real and is kept (owner report
  2026-11-26); longer parenthesised text keeps its break too, so a real
  aside is never joined) and
  `strip-dash` (the stray horizontal bar — U+2015, the "―" — the Mining
  cards' Sentences carried; owner report 2026-11-20). The strip is
  **Sentence-field-only**: `plan._ruby_repair_rules` excludes `strip-dash`
  from every ruby ("Sentence (furigana)") call site, so the ruby and every
  other field keep the bar — "only inside the sentence field, not
  everywhere". Added also: `strip-nbsp` (non-breaking spaces — `&nbsp;` and
  `\u00a0` — dumped during web/subtitle mining; converts to a single space
  between Latin/digit tokens, strips when adjacent to CJK or Japanese
  punctuation; unlike `strip-dash`, applies to both `Sentence` and its ruby
  `Sentence (furigana)`; owner addition 2026-11-21).
   Added also: `normalize-parens` (speaker/stage tags — short parenthesised
   names, the repo's speaker-paren notion — are rewritten to the
   Japanese-style full-width `（name）`; pairs that are not speaker tags
   (longer than 16 chars, nested or unbalanced) stay as mined; owner
   addition 2026-11-25, issue #16).
  `<br>` line breaks and spacing are otherwise deliberately NOT rewritten —
  they are the sentence's pause structure (owner decision 2026-11-15;
  `flatten-div-blocks` only converts block *tags*, never an existing `<br>`;
  the recorded future direction is deriving breaks from the audio,
  BULK_FIX.md). Unknown
  rule names are warned about and ignored. A repair write is never issued
  when another `Sentence` write (expansion or bold) is queued for the same
  note, and it is dropped when it would change how often the tested word
  (`cloze-body`) occurs. The transformation is idempotent. By owner
  decision (2026-11-16) the dimension runs by default: the tool fixes on
  its own (`batch_repair_sentence: true`, rules `flatten-div-blocks`,
  `strip-arrows`, `join-speaker-lines`, `strip-dash`, `strip-nbsp`,
   `normalize-parens`); the
  config key remains the escape hatch to switch it off, and every guard
  above still applies.
- **Q-D11 — Furigana regeneration uses Japanese Support as the only
  oracle.** The `regenerate_furigana` op rewrites `Sentence (furigana)`
  from the note's own Sentence through the installed Japanese Support
  add-on's reading engine; the planner never generates ruby itself and the
  add-on's public entry (which refuses non-empty destinations) is bypassed
  only to reach its MeCab/Kakasi reading, which is the same engine that
  entry uses. Guards: the generator input is the Sentence the plan will
  actually leave behind — the **checked** Sentence write (a repair/expansion/
  bold row feeds its `new_value`; a guarded, unchecked expansion never
  applies and must not feed the generator, or the ruby is built for a
  sentence the card does not end up with); no other `Sentence (furigana)`
  write may be queued for the note (a scene fill owns the field first); an
  empty engine result or a raised engine error becomes a reported
  `furigana-generation-failed` reason, never a write; and the generated
  ruby's base text must carry exactly the sentence's characters
  (`normalize.furigana_base_matches`), else `furigana-base-mismatch` and no
  write. Unreachable engine (headless, add-on missing) is reported once per
  run. The engine is the controller at `reading.mecab` inside the add-on
  package (its `__init__` imports the `reading` submodule; the package has no
  `mecab` attribute — looking only at the package silently disabled the whole
  dimension until 2026-11-17). If a sentence completion shipped expecting a
  rebuilt ruby (`ruby-will-be-regenerated`) and the engine produced nothing,
  the completion is demoted (`stale-fragment-furigana-risk`) rather than
  leaving the old ruby under the new text.
  **Protection for user-authored ruby context (owner report 2026-11-21):**
  On unedited sentences (`pending is None`), a non-empty `Sentence (furigana)`
  is never overwritten as stale when it carries user-authored context —
  speaker tags on the back omitted from the front (`ruby_speaker and not sent_speaker`,
  e.g. `（炭治郎）`) or enclosing context (`norm_sentence_match(sentence) in norm_sentence_match(base)`).
  The user intentionally omits speaker tags from the front so as not to
  reveal the answer, while keeping them on the back for character voice;
  blind regeneration must never clobber that hand-crafted context.
  By owner decision (2026-11-16) it
  runs by default (`batch_regenerate_furigana: true`); the config key remains
  the escape hatch, and every guard above still applies.
- **Q-D12 — Expression repair is self-oracled, completion and lemma normalization,
  on by default.** The `repair_expression` op rewrites a note's own `Expression`
  to the base text of its own `furigana` field: Yomitan records the
  dictionary form the lookup resolved to (ひと握り mined under furigana
  `一握[ひとにぎ]り` → 一握り; 倍にして under `倍[ばい]にする` → 倍にする). No dictionary
  seam and no hand-written kana→kanji table — the note itself is the oracle.
  Guards, each a reported reason instead of a guess: `reading_from_furigana(furigana)`
  must equal the note's normalized `reading` field (the furigana must describe *this*
  word); the base must differ from the Expression, contain kanji and be
  word-like (the Q-D3 gate); and the rewrite must be either a **kanji
  completion** (the base's kanji set strictly extends the Expression's:
  ひと握り → 一握り, とっておき → 取って置き) or an **inflection normalization to dictionary
  lemma** (identical kanji set in identical order sharing the initial stem:
  倍にして → 倍にする, 決まってる → 決まる). A kanji-dropping kana preference
  (気付き → 気づき) or different-kanji substitution (美味い → 上手い) remains refused
  and reported (`expression-repair-not-a-kanji-completion`), never written.
  `furigana_base` also treats HTML `<ruby>`/`<rt>` markup as an unusable oracle
  (its `<rt>` text would corrupt the base into `温あたためる`). The write is deferred
  when a word-level write is queued for the note — those values come from the old
  spelling's word group and must not land in the same run as the rewrite (F2 risk 4).
  By owner decision (2026-11-16, "I want things to be easy", confirmed 2026-11-18)
  it runs by default like every other dimension — automatic mode, no options to tick;
  `batch_repair_expression` remains the escape hatch to switch it off, and every
  rewrite is previewed old → new and undone from the run log like every other write.

- **Q-D14 — A mis-mined sentence is recovered by the word, never by the
  episode's mood.** When a card is still missing its `Sentence Audio` (Q-D16)
  and its own `Sentence` contains none of the word's spellings (the author's
  類い希: the wrong subtitle block was mined), the scene is recovered by
  anchoring on the **word**, with every guard from the author's own cards:
  - the spelling candidates come from the note's own furigana — every mix of
    "kanji kept / kanji written in kana" (`類い希` → 類いまれ, たぐい希, …);
  - the `Source` must name an episode, or — for a blob link — every matching
    candidate card must agree on **one** identifiable episode;
  - each candidate must occur **exactly once** in a same-episode,
    audio-bearing card's sentence;
  - at least **two** same-episode cards must match, and their sentences must
    be byte-identical or **nested** (the same block at different clips) — the
    tighter clip wins; a genuinely different scene is refused;
  - the write is checked (`recovered-sentence`, `word-in-sentence` badges),
    cleans both the recovered sentence and its paired ruby with the configured
    repair rules, checks `eligible_source` on every scene fill (Q-D8), and
    fills the card's empty audio/picture/source in the same run.
- **Q-D13 — Empty definition shells are cleared, never copied over.**
  `Definition` and `Extended definition` belong to `FIELD_NEVER_COPY`: the
  add-on never copies definitions from sibling cards. The
  `strip_empty_glossary` op clears a definition field only when it holds
  nothing but empty container markup (the Yomitan `<ol></ol>` shell) — the
  rendered text is empty before and after, so nothing is lost, and a second
  run is a no-op. Any media (`[sound:...]`, `<img>`, `[anki:...]`) or any
  visible character means no write: a real definition is never touched
  (`normalize.is_empty_glossary_shell`, pure).
- **Q-D15 — A ruby always describes the sentence the card ends up with.** The
  ruby is *derived* from the sentence, so a `Sentence (furigana)` value that
  does not carry the final Sentence's characters
  (`normalize.furigana_base_matches`) is never written **checked** — whether it
  came from a parent's payload, a moment group, a recovered scene or the
  reading engine. The scene payload fill is the path this guards: it copies a
  parent's ruby into an empty child ruby, and when the child's own sentence is
  a different block the parent's ruby annotates text the card does not show
  (author's どなた, 日取り). The invariant runs per target *before* the
  regeneration tier decides, so a dropped ruby is regenerated correctly
  instead of being blocked by a bad one (a guarded row, already demoted to
  unchecked, is left for the reason it carries). Verified by
  `test_a_foreign_ruby_is_never_copied_onto_a_different_sentence` and the
  scene audit (C class 0 with the reading engine).
- **Q-D16 — A card that receives the scene's audio also receives the scene's
  text.** A card missing its `Sentence Audio` is completed from the scene
  source: its `Sentence` becomes the parent's whole block (the card's word
  bolded, `expand_sentence`), so the text and the clip describe the same
  speech. This is the general form of the word-mined fragment: a card mined
  as one word keeps only its fragment in `cloze-*` (the tested word), so the
  old "fragment must be recoverable from `cloze-*`" guard refused every
  completion it should have made (author's 正道: a two-line fragment playing
  the parent 出来's 14.8 s nine-line scene clip). The fragment is information-
  preserved because the scene matcher requires the card's sentence to be
  **contained** in the parent's, so the completion can never lose it
  (`fragment-kept-in-scene`; `fragment-kept-in-cloze` when `cloze-*`
  reconstructs it). Containment is judged on matching keys only: a space
  beside CJK is typographic and ignored even when the line carries a Latin
  token (`ＯＫ 少年`), while a space between two Latin/digit tokens is
  lexical and kept (`a b` never matches `ab`). Without that symmetry a
  pure-CJK fragment failed to match an `ＯＫ`-bearing scene (author's
  引き受ける card, S03E10) and was wrongly reported `no-parent-that-fills`;
  the captured pair is in the real-card fixture.
  Two pairing rules keep the card whole:
  - a completion that would strand a fragment ruby ships checked only when
    the parent carries a matching ruby **or** the furigana tier will rebuild
    it in the same plan (`ruby-will-be-regenerated`, Q-D11/Q-D15); otherwise
    it is `stale-fragment-furigana-risk` and ships unchecked;
  - **convergence:** the completion runs only while the card is still
    missing its scene audio. The run fills that audio, so the next run sees
    a card that already owns its scene and leaves its sentence alone. A card
    that already carries its own `Sentence Audio` is a scene unit in its own
    right and is never expanded onto a longer neighbour's block (its audio and
    picture are never overwritten); the cloze anchor must still be placeable
    (once, or inside the card's own fragment). It still receives all other
    applicable repairs: subtitle noise cleaning (arrows, div blocks, speaker lines,
    Q-D10), furigana regeneration (Q-D11), bold repair (Q-D3), word-level fills,
    and expression spelling repair (Q-D12).
    The cross-run cascade (a card completed in one pass becoming a scene parent
    for a neighbour) is resolved in the **same Apply** by the fixpoint controller
    (`collection_io.apply_plan_to_fixpoint`, QUALITY Q-A2).
  Verified by `test_a_jidoujisho_clip_is_scene_audio_and_completes_the_card`,
  `test_a_stale_fragment_ruby_is_regenerated_when_the_engine_is_available`,
  `test_a_completed_sentence_is_never_expanded_again`, and the 正道 replay.

## Correctness of decisions

- **Q-C1 — Never guess.** Ambiguity becomes a visible `reason` or `warning`:
  `no-parent-that-fills`, `cloze-word-ambiguous`, `cloze-word-not-in-sentence`,
  divergent parents. No tie is resolved by choosing one of several plausible
  values.
- **Q-C2 — Scope is a hard cap.** Scene parents are searched only inside the
  temporal window (default ±3 days, `MAX_WINDOW_MS` ceiling). A miss is
  reported; the window is never widened silently.
- **Q-C3 — Determinism.** The same records and config produce the same writes,
  in the same order (`(note_id, field, op)`); `PLAN_VERSION` is bumped when
  plan meaning changes.
- **Q-C4 — The preview is the truth.** What the dialog shows is exactly what
  the applier writes: `build_plan` output is the only input to both.
- **Q-C5 — Opt-out is respected.** A note tagged `optout_tag` is never a
  target; it may still be a *source*, and is badged when used.
- **Q-C6 — The Browser selection is the target set, never the source set.**
  Only selected notes are written; every note of the configured deck remains
  eligible as a parent/word source. Duplicates are dropped, the user's order
  is preserved, foreign note types and ids that no longer resolve are
  reported rather than silently dropped.
- **Q-C7 — A scene source is only trusted when it is unambiguous and close.**
  If several parents would write *different* payloads, no parent is auto-chosen:
  the rows ship unchecked with a `divergent-parents` badge and a warning. A
  containment match further away than `scene_auto_gap_hours` (default 6) from
  the child ships unchecked with `far-from-source`; a byte-identical sentence
  is self-verifying and is trusted regardless of distance. Batch runs send
  every such note to review instead of writing it. Wrong-source data would be
  wrong in a way no later check could detect, so it is never guessed.
- **Q-C8 — Nothing is written without explicit acceptance.** Rows are
  `default_checked` only when every guard passed; the user still has to press
  Apply.
- **Q-C9 — Batch runs ask for no decisions.** A bulk run resolves its own
  guard policy and writes only what is provably safe:
  - **Source confidence** (`divergent-parents`, `far-from-source`) — a
    source that other parents disagree with is not written: the rows it
    proposed are dropped (the note is *left alone* with the reason when
    they were its only rows). Two corroborations lift the demotion, both
    taken from the author's own cards: **two or more parents writing
    byte-identical payloads** (自閉症 ← 自負 plus a second agreeing sibling),
    and **nested sentences** — competing parents whose sentences are the
    same subtitle block at different clips (one contains the other), where
    the tighter pick wins. Genuinely different scenes still demote.
    Because the guard is about the *source*, it does not freeze the card's
    own work — a card whose scene siblings disagree still gets its bold,
    its arrows stripped and its furigana rebuilt.
  - **Text safety** (`cloze-anchor-not-unique`, `stale-fragment-furigana-risk`,
    `paired-with-expansion`) — exactly the text unit is dropped; the note's
    independent scene fields (audio, picture, translation) are still
    filled.
  - A note with nothing safe to write is reported as *left alone* with a
    reason, never as a question. The preview report lists them all.
  - **The batch default includes both information-preserving replaces**
    (sentence completion and blob-source replacement) so the tool completes
    cards instead of merely filling blanks; `batch_expand_sentence` and
    `batch_replace_source` turn each off. Both keep their information: the
    mined fragment survives (in `cloze-*`, and always inside the new scene
    sentence — Q-D16), the blob URL is written to the run
    log before the write. The subtitle-noise repair runs by default too
    (`batch_repair_sentence`, default true); rows of a disabled tier
    make the note count as excluded by policy. The Expression repair
    likewise runs by default (owner decision 2026-11-16;
    `batch_repair_expression` is the escape hatch, Q-D12).

## Apply behaviour

- **Q-A1 — Per-note atomicity.** If any accepted row of a note is stale at
  apply time, *no* row of that note is written; the note is reported as
  `changed since preview`.
- **Q-A2 — One run is the whole fix; a second run writes nothing.** The first
  Apply must leave the collection at a fixpoint: a second Apply finds
  **zero rows** — not "the same rows are already applied", but *no new work of
  any kind*. Two consequences, both release gates:
  - **no cross-run cascade:** if fixing card A in this run would make card B
    fixable, B is fixed in the **same** run;
  - **complete ordering inside the plan:** a step that another depends on
    (arrow/line cleanup before bold, a sentence before its ruby, a
    completion's audio and its text) happens in this run, never the next.

  A row already at its target value is reported as `already applied`, never
  rewritten. **How the fixpoint is reached:** after each pass the applier
  re-plans on the *real* post-write state and stops when a pass writes
  nothing (`collection_io.apply_plan_to_fixpoint`); the per-pass undo logs are
  merged into one (`applier.merge_run_logs`), so one Apply — and one Undo —
  cover the whole fixpoint. A cross-run cascade (a card completed here
  becoming a source for a neighbour) is therefore resolved **inside the same
  Apply**, never deferred. Measured by `scripts/scene_audit.py` (class **F**),
  which mirrors that loop: apply the selection to a fixpoint, then re-plan it;
  **the residual must be 0**. The 2026-11-17 measurement on the newest 300
  cards is **0 rows** (it was 33 before the bold `old_value` fix and 21 before
  the applier fixpoint).
- **Q-A3 — Undo is exact and conservative.** It restores only fields still
  holding the applied value, removes only the tags the run added, and reports
  `changed since the run` instead of clobbering a later edit. Undo is itself
  idempotent.
- **Q-A4 — One collection update per note**, so Anki's own undo entry stays
  meaningful.
- **Q-A5 — Writes happen only when the host is quiescent.** The accepted rows
  are written *after* the preview dialog has closed (never inside its nested
  event loop) and, when a Browser is open, only after that Browser's editor
  has saved pending edits (`applier.run_after_editor_saved`). A lost editor
  callback is covered by an idempotent safety net, so an apply is never
  silently dropped and never written twice. This is what keeps a pending
  editor save from landing on top of the run — and away from Anki's own
  legacy-editor teardown race.
- **Q-A6 — A run is recoverable before it starts via surgical inverse logs.**
  Every run writes its undo log **before the first write**, applies in chunks,
  and then re-reads every written field and tag set to verify the values landed
  (`collection_io.apply_batch`, `applier.verify_writes`). Because the JSON
  inverse log records the exact prior values and is written before SQLite is
  touched, an interrupted or crashed run is immediately reversible via Tools →
  Mining Fixer: undo last run without multi-second UI freezing or review history
  loss. A partial write is reported rather than silently accepted.
- **Q-A7 — The configured fields must exist on the note type.** Field names
  are checked against the live note type before any write
  (`collection_io.preflight_fields`); drift aborts the run instead of raising
  halfway through. Notes deleted mid-run are skipped and reported by
  verification, never allowed to abort the rest.
- **Q-A8 — Tags compare case-insensitively**, exactly like Anki's own tag
  matching, so a case difference can neither duplicate a tag nor produce a
  false verification problem.
- **Q-A9 — Host UI synchronization after collection writes.** Every write to
  the collection (single-preview Apply, batch-fix, undo) must immediately
  synchronize Anki's host UI and open editors
  (`collection_io.notify_collection_changed`). This:
  1. invalidates cached reviewer cards (`mw.reviewer.card.load()`) so opening
     the browser from review never loads stale card note references;
  2. triggers Anki's global reset (`mw.reset()`), synthesizing `OpChanges` to
     fire `gui_hooks.operation_did_execute` and `gui_hooks.state_did_reset`,
     alerting all webviews (legacy and Svelte) and Anki components;
  3. reloads any open Browser or EditCurrent note editor from SQLite
     (`card.note(reload=True)` → `editor.set_note(note)`) and redraws browser
     table cells (`table.redraw_cells()`).
  Without this post-write synchronization, any open or subsequently closed
  Browser flushes its stale editor contents on `closeEvent` (`saveNow()`),
  silently overwriting and reverting the applied fix to its pre-fix values
  (the author's ばあちゃん defect).
- **Q-A10 — A skipped guarded expansion never becomes unreachable.** A scene
  fill may supply a sibling's `Sentence Audio` while the paired `Sentence`
  expansion is guarded and left unchecked (the 飲食 card, 2026-11-21). The
  card then owns scene audio but still carries a fragment sentence, and the
  guard at `_scene_writes_for` would keep every later run from proposing the
  expansion. Such a card is detected by `_shared_audio_parent`: its audio
  file is byte-identical to the sibling's while its sentence is a strict
  fragment of the sibling's full block. It stays expandable, the expansion
  writes the full block with the bold retargeted to *this* card's word, and
  the following run is a no-op. Related: `_fragment_anchored_sentence`
  ignores the parent's bold markup when matching the cloze fragment, so a
  sibling bolding a different word cannot force the expansion into the
  guarded state in the first place. Measured independently of the planner by
  `scripts/scene_audit.py` class **G**, which reads post-run field values only
  and fails on a card holding a strict fragment under a sibling's audio;
  class F (residual rows == 0) cannot see this state.
- **Q-A11 — No silent abandonment under partial apply.** A guarded row is an
  explicit statement "this card needs this write, but a human must confirm
  it". If a run writes only the checked rows of the same plan, the skipped
  work is still owed: the next run must still propose every guarded field
  (or the field must now hold the intended value). Eligibility must never be
  destroyed by a write another op in the same bundle made — the 飲食 bug was
  exactly that: the audio fill removed the expansion's only trigger. Pinned
  by a metamorphic test that applies only checked writes, re-plans, and
  asserts each previously guarded field is still proposed (Q-A2's fixpoint
  gate alone is insufficient because it asks the planner to referee itself).

## Performance & Latency

- **Q-P1 — Sub-second single-card fix latency (the 1-second wall).**
  Fixing a single card selection must complete in under 1 second end-to-end
  (interactive UI response):
  1. **No synchronous full-collection backups (`_take_backup`):** Anki's native
     `create_backup` must never be called on the interactive path; safety and
     exact reversibility are guaranteed by pre-write JSON inverse logs (Q-A6).
  2. **In-window candidate parent indexing:** `SceneParentIndex.from_parents`
     must never index 3-grams of the entire historical deck when planning a
     bounded selection; only candidate parents within
     `[t.note_id - window_ms, t.note_id + window_ms]` of targets with scene
     gaps may be indexed.
  3. **In-memory fixpoint re-planning:** Passes after the first pass in
     `apply_plan_to_fixpoint` must reuse in-memory records and update only the
     touched notes in-place, never issuing a redundant full-deck SQLite query.
  4. **Cached media existence lookups:** Media checks must query the media
     directory path once and cache file existence in memory, avoiding redundant
     Rust FFI and filesystem `stat` bursts.

## System

- **Q-S1 — The pure core never imports `anki`, `aqt` or PyQt** (AST-enforced).
  This is what makes the headless builder, the suite and CI possible.
- **Q-S2 — The add-on survives import.** Anything in `__init__.py` that
  raises is what users see as "add-on failed to load": the `aqt` import is
  optional, handlers import lazily, and failures print instead of throwing.
- **Q-S3 — Fully offline.** No network access, ever.
- **Q-S4 — Development tooling never writes to a live profile.** Copies and
  scratch bases only; the only writer of a real collection is the add-on,
  after Apply.
- **Q-S5 — Deterministic audit identities.** Audit notes use
  `uuid5(mb-audit:<note id>)`, so re-importing a rebuilt package updates in
  place instead of duplicating.
- **Q-S6 — No secrets** in code, config, fixtures or logs.

## Acceptance

- **Q-T1 — The golden fixture stays byte-exact.** `tests/test_golden_scene.py`
  reproduces the author's manual 診察 fix on all five fields, plus the tag
  union (`asbplayer yomitan`).
- **Q-T2 — Gates are clean.** `ruff check`, `ruff format --check` and the
  suite pass locally and on CI for the released commit.
- **Q-T3 — A new test must be able to fail.** Tests for bug fixes are verified
  to fail against the unfixed code (or the observation that made the bug
  visible is recorded in the test).
