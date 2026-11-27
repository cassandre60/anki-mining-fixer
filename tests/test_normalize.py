"""Tests for normalize.py"""

from __future__ import annotations

from mining_fixer.normalize import (
    REPAIR_RULES,
    detect_inline_readings,
    furigana_base,
    furigana_base_matches,
    is_empty_glossary_shell,
    kana_fold,
    norm_reading,
    norm_sentence,
    norm_sentence_match,
    override_furigana_reading,
    reading_from_furigana,
    repair_sentence,
    strip_furigana_annotation_space,
    word_key,
)


def test_sound_tags_and_images_removed():
    assert norm_sentence("[sound:a.webm] はい <img src=x.jpg> そうです") == "はいそうです"


def test_html_and_entities():
    assert norm_sentence("<br>ことば&nbsp;のみ</div>") == "ことばのみ"


def test_whitespace_removed_for_cjk():
    assert norm_sentence("先生は今 診察中ですので") == "先生は今診察中ですので"


def test_latin_whitespace_collapsed_not_removed():
    assert norm_sentence("a b") == "a b"
    assert norm_sentence("a  b") == "a b"


def test_cjk_spaces_dropped_even_when_the_line_has_latin():
    # A scene line with a Latin token (OK, SNS, …) used to keep every space,
    # while a pure-CJK fragment dropped them; the two keys then disagreed on
    # the same Japanese text and scene matching failed (author's 引き受ける
    # card, S03E10). Spaces beside CJK are typographic, not lexical.
    assert norm_sentence("ＯＫ 少年 君のお願い しかと引き受けた") == (
        "OK少年君のお願いしかと引き受けた"
    )
    assert norm_sentence("ａ ｂ") == "a b"
    assert norm_sentence("OK a") == "OK a"


def test_nfkc_halfwidth_katakana():
    assert norm_sentence("[ｲﾔﾎﾝ] たとえ") == "[イヤホン]たとえ"


def test_nfkc_fullwidth_round_brackets_decompose():
    assert norm_sentence("（受付）") == "(受付)"


def test_cjk_brackets_kept():
    assert norm_sentence("【怒り】") == "【怒り】"
    assert norm_sentence("⦅夢⦆") == "⦅夢⦆"


def test_punctuation_folded():
    assert norm_sentence("はい｡そうです。") == "はい.そうです."


def test_invisible_characters_removed():
    assert norm_sentence("こと\u200bば\ufeffです") == "ことばです"
    assert norm_sentence("なに\u27a1\ufe0f") == "なに➡"


def test_trailing_continuation_markers_stripped_for_matching():
    assert norm_sentence_match("先生は今 診察中ですので➡") == "先生は今診察中ですので"
    assert norm_sentence_match("まだ おかあさんのオッパイが➡") == "まだおかあさんのオッパイが"
    # interior markers survive: only the tail is stripped
    assert norm_sentence_match("➡はい") == "➡はい"


def test_kana_fold():
    assert kana_fold("シンサツ") == "しんさつ"
    assert kana_fold("ヴィルス") == "ゔぃるす"
    assert kana_fold("ジョーカー") == "じょーかー"  # long vowel kept


def test_norm_reading():
    assert norm_reading("シンサツ") == "しんさつ"
    assert norm_reading("しきゅう") == "しきゅう"


def test_reading_from_furigana_interleaved():
    # the naive "concatenate payloads" bug would give しいた
    assert reading_from_furigana("死[し]に 至[いた]る") == "しにいたる"
    assert reading_from_furigana("先[ま]ず") == "まず"
    assert reading_from_furigana("耳[みみ]を 傾[かたむ]ける") == "みみをかたむける"
    assert reading_from_furigana("大人[おとな]") == "おとな"
    assert reading_from_furigana("人々[ひとびと]") == "ひとびと"
    assert reading_from_furigana("診察[しんさつ]") == "しんさつ"


def test_reading_from_furigana_malformed():
    assert reading_from_furigana("[かんじ") is None
    assert reading_from_furigana("漢字[かん") is None
    assert reading_from_furigana("") is None


def test_word_key():
    assert word_key("診察", "しんさつ") == ("診察", "しんさつ")
    assert word_key("診察", "", "診察[しんさつ]") == ("診察", "しんさつ")
    assert word_key("", "しんさつ") is None


# --- repair_sentence (bulk-fix dimension 2) ---------------------------------
#
# Fixtures are real Sentence values from the configured deck (census
# 2026-11-15): multi-line subtitle dumps joined with <br>, line-tail arrows,
# ideographic beat spaces, bold markup in the middle of it all.

ALL_RULES = REPAIR_RULES

# Real Sentence value (note 1793400721969, 淀む): a styled subtitle source
# nested the sentence in dozens of empty <div> wrappers that render as the
# four visible lines. Owner report 2026-11-17.
_DIV_SENTENCE = (
    "奥の方に泉を発見しました!<br>残念ながら出口はまだ見つかりません!<br>"
    "<div><div><div><br><div><div><div><div><div></div></div><div><div></div>"
    "</div><div><div></div></div><div><div></div></div><div><div></div></div>"
    "<div><div></div></div><div><div></div></div></div><div><div><div></div>"
    "</div><div><div></div></div><div><div></div></div><div><div></div></div>"
    "<div><div>その泉の水...なら泣きいろ紫色なんです</div></div></div></div></div>"
    "</div></div></div><div><div><div><div><div><div><div><div>それもひどく<b>よどん</b>"
    "だ</div></div></div></div></div></div></div></div>"
)
_DIV_EXPECTED = (
    "奥の方に泉を発見しました!<br>残念ながら出口はまだ見つかりません!<br><br>"
    "その泉の水...なら泣きいろ紫色なんです<br>それもひどく<b>よどん</b>だ"
)


def test_repair_flattens_nested_div_blocks():
    # 39 <div> wrappers, four visible lines: the block structure becomes the
    # <br> line breaks it stood for; the bold passes through untouched.
    new, applied = repair_sentence(_DIV_SENTENCE, ALL_RULES)
    assert applied == ("flatten-div-blocks",)
    assert new == _DIV_EXPECTED
    assert "<div" not in new
    # Second pass is a no-op (one run fixes a card).
    assert repair_sentence(new, ALL_RULES) == (new, ())


def test_repair_flatten_keeps_inline_parts_and_bold():
    text = "<div><div>一つ目<b>語</b> 二つ目</div></div>"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("flatten-div-blocks",)
    assert new == "一つ目<b>語</b> 二つ目"


def test_repair_flatten_drops_layout_whitespace_between_blocks():
    text = "<div>\n  <div>前</div>\n  <div>後</div>\n</div>"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("flatten-div-blocks",)
    assert new == "前<br>後"


def test_repair_strips_line_tail_arrows():
    text = "あしたの朝刊の見出しは<br>こうニャ！➡"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("strip-arrows",)
    assert new == "あしたの朝刊の見出しは<br>こうニャ！"


def test_repair_keeps_the_line_breaks_and_spacing_it_does_not_ship():
    # Owner decision 2026-11-15: <br> line breaks and spacing are the
    # sentence's pause structure and stay exactly as mined — see BULK_FIX.md.
    text = "俺は弱くて 誰も助けらんなかった<br>それどころか 伏黒を殺し<b>かけた</b>"
    assert repair_sentence(text, ALL_RULES) == (text, ())


def test_repair_keeps_midline_arrows():
    # f:X→Y is mathematical notation, not a continuation marker.
    text = "任意の<br>xεXに対してただひとつのyεYが対応しているとき，関数f:X→Yが定まったといい，"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ()
    assert new == text


def test_repair_strips_stray_horizontal_bars():
    # Owner report 2026-11-20: the Mining cards' Sentences carried the stray
    # horizontal bar (U+2015) — `strip-dash` removes it; idempotent; other
    # rule classes do not fire on the bar alone.
    text = "あ\u2015い\u2015<br>う"
    new, applied = repair_sentence(text, ("strip-dash",))
    assert applied == ("strip-dash",)
    assert new == "あい<br>う"
    assert repair_sentence(new, ("strip-dash",)) == (new, ())
    # A run of bars and a bar mid-line are the same character to the rule.
    assert repair_sentence("a\u2015b\u2015\u2015c", ("strip-dash",)) == ("abc", ("strip-dash",))
    # With the rule off or unlisted, the bar stays.
    assert repair_sentence("あ\u2015い", ()) == ("あ\u2015い", ())
    assert repair_sentence("あ\u2015い", ("strip-arrows", "join-speaker-lines")) == (
        "あ\u2015い",
        (),
    )
    # A bar in other text classes the rule does not run on is not touched.
    assert repair_sentence("あ\u2015い", ("flatten-div-blocks",)) == ("あ\u2015い", ())


def test_repair_strips_nbsp_entities_and_unicode_nbsp():
    # Non-breaking spaces (&nbsp; and \u00a0) dumped during web/subtitle mining
    # are noise: strip adjacent to Japanese/punctuation, convert to single space
    # between Latin/digits. Idempotent.
    text = "彼女は&nbsp;毎朝<b>いつも</b><br>食べながら<b>遊ぶ&nbsp;</b>"
    new, applied = repair_sentence(text, ("strip-nbsp",))
    assert applied == ("strip-nbsp",)
    assert new == "彼女は毎朝<b>いつも</b><br>食べながら<b>遊ぶ</b>"
    assert repair_sentence(new, ("strip-nbsp",)) == (new, ())

    # Between Latin/digits: converts to single space
    assert repair_sentence("Episode&nbsp;3", ("strip-nbsp",)) == ("Episode 3", ("strip-nbsp",))
    assert repair_sentence("Part\u00a0\u00a01", ("strip-nbsp",)) == ("Part 1", ("strip-nbsp",))

    # At line boundaries and with unicode \u00a0
    assert repair_sentence("&nbsp;おいしい\u00a0", ("strip-nbsp",)) == ("おいしい", ("strip-nbsp",))

    # With the rule unlisted, nbsp stays untouched
    assert repair_sentence("あ&nbsp;い", ("strip-dash", "strip-arrows")) == ("あ&nbsp;い", ())


def test_repair_keeps_bold_markup_around_a_tail_arrow():
    text = "俺は弱くて 誰も助けらんなかった<br>それどころか 伏黒を殺し<b>かけた</b>➡"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("strip-arrows",)
    assert new == "俺は弱くて 誰も助けらんなかった<br>それどころか 伏黒を殺し<b>かけた</b>"


def test_repair_br_variants_and_vs16_arrow():
    new, applied = repair_sentence("一つ目<br/>二つ目<BR />三つ目➡", ALL_RULES)
    assert applied == ("strip-arrows",)
    assert new == "一つ目<br/>二つ目<BR />三つ目"
    new, applied = repair_sentence("一つ目➡️<br>二つ目", ALL_RULES)  # arrow + VS16
    assert applied == ("strip-arrows",)
    assert new == "一つ目<br>二つ目"


def test_repair_is_idempotent():
    texts = [
        "あしたの朝刊の見出しは<br>こうニャ！➡",
        "「絶」\u3000念オーラを完全に絶つ技<br>自身の気配を消したり<br><b>疲労回復</b>に効果がある➡",
        _DIV_SENTENCE,
    ]
    for text in texts:
        once, _ = repair_sentence(text, ALL_RULES)
        twice, applied_twice = repair_sentence(once, ALL_RULES)
        assert twice == once, text
        assert applied_twice == (), text


def test_repair_noop_on_clean_or_empty_text():
    assert repair_sentence("きれいな文。", ALL_RULES) == ("きれいな文。", ())
    assert repair_sentence("", ALL_RULES) == ("", ())
    # Whitespace-only values are left exactly as they are (is_empty gate).
    assert repair_sentence("   ", ALL_RULES) == ("   ", ())


def test_repair_joins_a_line_break_after_a_speaker_tag():
    # Owner report: "(荒木)<br>相手が あなたなら…" — the break is the
    # subtitle's wrapping, not the speech. join merges the tag into the next
    # line, so normalize-parens then converts the now-inline pair (issue #16).
    text = "日野君も ここに来てるんだ?<br>(荒木)<br>相手が あなたなら守秘義務も<br>もありませんよ。"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("normalize-parens", "join-speaker-lines")
    assert "（荒木）相手が" in new
    # A long parenthesised aside is not a speaker tag: its break survives the
    # join, and the pair is converted to Japanese style.
    long_line = "これは(とても長い補足の文章でここはタグではない)<br>続き。"
    new2, applied2 = repair_sentence(long_line, ALL_RULES)
    assert applied2 == ()
    assert new2 == long_line


def test_repair_join_does_not_cross_a_blank_line():
    # A tag followed by a blank line (<br><br>) is its own stanza, not a
    # wrapped line. Joining only one of the two breaks left the second, so a
    # second run joined again and never converged (owner requirement: one run
    # fixes a card; a second is a no-op). Real value, note 1744221774736.
    # The speaker tag stays as mined (issue #16).
    text = "微かな望みさえ見えない<br>(So dark)<br><br>手を伸ばしても届かない"
    new, applied = repair_sentence(text, ALL_RULES)
    # normalize-parens converts the speaker tag (issue #16).
    assert applied == ("normalize-parens",)
    assert new == "微かな望みさえ見えない<br>（So dark）<br><br>手を伸ばしても届かない"
    # A single break after the tag still joins.
    new, applied = repair_sentence("微かな<br>(So dark)<br>手を", ALL_RULES)
    assert applied == ("normalize-parens", "join-speaker-lines")
    assert new == "微かな<br>（So dark）手を"


def test_repair_join_keeps_the_break_after_a_mid_line_parenthetical():
    # A short paren at the END of a text line — (test)<br> — is not a
    # speaker/stage tag alone on a line: its break is real and must survive
    # the join (owner report 2026-11-26; the 浮く card with (test) added).
    text = (
        "(ヒロミ)最近 ヤナギハラさんが一人なの 知ってた?(test)<br>"
        "(悟)えっ 美里が?<br>"
        "(ヒロミ)あの給食費のことがあってから<br>"
        "みんなから<b>浮いちゃってる</b>みたい。"
    )
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("normalize-parens",)
    assert new == (
        "（ヒロミ）最近 ヤナギハラさんが一人なの 知ってた?(test)<br>"
        "（悟）えっ 美里が?<br>"
        "（ヒロミ）あの給食費のことがあってから<br>"
        "みんなから<b>浮いちゃってる</b>みたい。"
    )


def test_repair_unknown_rules_are_ignored():
    new, applied = repair_sentence("前➡<br>後", ("strip-arrows", "nonsense"))
    assert applied == ("strip-arrows",)
    assert new == "前<br>後"


# --- normalize-parens (speaker parens -> Japanese style, issue #16) -----------
#
# Subtitle dumps write Sentences with generic (half-width) speaker parens where
# the Japanese style — full-width `（）` — is wanted (owner report: the 浮く
# card, speaker (ヒロミ)). The rule converts speaker/stage tags (short
# parenthesised names, the repo's speaker-paren notion) to `（name）`; pairs
# that are not speaker tags (longer than 16 chars, nested or unbalanced, or
# used inline for other purposes) stay as mined. Unbalanced or nested pairs
# are left as-is.


def test_normalize_parens_converts_speaker_tag_to_japanese_style():
    text = "(葉子)"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("normalize-parens",)
    assert new == "（葉子）"
    # Idempotent: the converted value is the fixpoint.
    assert repair_sentence(new, ALL_RULES) == (new, ())


def test_normalize_parens_leaves_inline_parens_as_is():
    # A parenthesised pair inside the sentence (not a speaker tag alone on a
    # line) is left untouched.
    text = "あの歌(葉子)を聞いた"
    assert repair_sentence(text, ALL_RULES) == (text, ())


def test_normalize_parens_converts_speaker_tag_among_other_lines():
    text = "あの歌を聞いた<br>(ヒロミ)"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("normalize-parens",)
    assert new == "あの歌を聞いた<br>（ヒロミ）"


def test_normalize_parens_converts_speaker_tag_prefixing_a_line():
    # The real defect (note 1738961231466, the 浮く card): the speaker tag
    # opens the line and the dialogue follows on the same line —
    # ``(ヒロミ)最近...`` — so it must convert, not only a tag that fills the
    # whole line.
    text = "(ヒロミ)最近 ヤナギハラさんが一人なの 知ってた?"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("normalize-parens",)
    assert new == "（ヒロミ）最近 ヤナギハラさんが一人なの 知ってた?"


def test_normalize_parens_leaves_long_speaker_as_is():
    # A parenthesised name longer than the speaker-paren limit (16 chars) is
    # not a speaker tag and stays as mined.
    text = "(とても長い補足の文章でここはタグではない)"
    assert repair_sentence(text, ALL_RULES) == (text, ())


def test_normalize_parens_leaves_unbalanced_as_is():
    text = "あの(浮くを思い出した"
    assert repair_sentence(text, ALL_RULES) == (text, ())


def test_normalize_parens_converts_fullwidth_open_with_western_close():
    # Mixed pair: full-width open + old-western close. The early-out must not
    # require ASCII "(" — `_SPEAKER_TAG_RE` already accepts `[)）]`.
    text = "（葉子)"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("normalize-parens",)
    assert new == "（葉子）"
    # Idempotent: the converted value is the fixpoint.
    assert repair_sentence(new, ALL_RULES) == (new, ())


def test_normalize_parens_converts_speaker_after_square_source_tag():
    # Real defect (bug 2, case b): a western speaker paren after a square
    # source tag. ``[TV](好美)`` -> ``[TV]（好美）``; square tag kept.
    text = "[TV](好美) 毎日 殺されるんじゃ"
    new, applied = repair_sentence(text, ALL_RULES)
    assert applied == ("normalize-parens",)
    assert new == "[TV]（好美） 毎日 殺されるんじゃ"
    assert repair_sentence(new, ALL_RULES) == (new, ())
    # Mixed paren styles after a square source tag convert too.
    assert repair_sentence("[TEL](受付) 患者", ALL_RULES)[0] == "[TEL]（受付） 患者"
    assert repair_sentence("[TV]（好美) ずっと", ALL_RULES)[0] == "[TV]（好美） ずっと"
    assert repair_sentence("[TV](好美） ずっと", ALL_RULES)[0] == "[TV]（好美） ずっと"


def test_normalize_parens_with_rule_off():
    text = "(葉子)"
    assert repair_sentence(text, ()) == (text, ())


# --- furigana_base_matches (bulk-fix dimension 3) ----------------------------


def test_furigana_base_matches_the_sentence_it_annotates():
    # Japanese Support's own format: per-kanji bracket ruby, <br> kept.
    # mecab trims the shared tail, so 見出し[みだし] comes out 見出[みだ]し.
    generated = "あしたの朝刊の 見出[みだ]しは<br>こうニャ！"
    assert furigana_base_matches(generated, "あしたの朝刊の見出しは<br>こうニャ！")


def test_furigana_base_ignores_whitespace_and_the_engines_spacing_quirks():
    # mecab prefixes annotated groups with a space; the guard squeezes it.
    assert furigana_base_matches(" 表面[ひょうめん]が  白い", "表面が白い")


def test_furigana_base_masks_speaker_brackets_on_the_sentence_side():
    # [TEL] is a speaker tag, not a reading: stripped on both sides.
    generated = "[TEL]もしもし[もしもし]"
    assert furigana_base_matches(generated, "[TEL]もしもし")


def test_furigana_base_rejects_a_mangled_generation():
    assert not furigana_base_matches("見[", "見出し")
    assert not furigana_base_matches("全然違うテキスト", "見出しはこうニャ！")
    assert not furigana_base_matches("", "見出し")


# --- is_empty_glossary_shell (empty-definition cleanup) ----------------------

JUNK_SHELL = '<div style="text-align: left;" class="yomitan-glossary"><ol></ol></div>'


def test_empty_glossary_shell_detects_the_yomitan_shell():
    assert is_empty_glossary_shell(JUNK_SHELL)
    assert is_empty_glossary_shell("<div class='g'><ol><li>&nbsp;</li></ol></div>")


def test_empty_glossary_shell_never_matches_real_content():
    # A real definition, however wrapped, is never touched.
    assert not is_empty_glossary_shell(
        '<div class="yomitan-glossary"><ol><li>しんさつ【診察】医者が調べること</li></ol></div>'
    )
    assert not is_empty_glossary_shell("しんさつ【診察】")
    # Media inside the shell is information: never cleared.
    assert not is_empty_glossary_shell("[sound:def.webm]")
    assert not is_empty_glossary_shell('<div class="g"><img src="x.jpg"></div>')
    # An already-empty value is nothing to clean.
    assert not is_empty_glossary_shell("")
    assert not is_empty_glossary_shell("   ")


# --- furigana_base (bulk-fix dimension 4: the Expression-repair oracle) -------


def test_furigana_base_extracts_the_dictionary_form():
    # The real ひと握り card: the mined spelling is the surface form, the
    # furigana records the dictionary form the Yomitan lookup resolved to.
    assert furigana_base("一握[ひとにぎ]り") == "一握り"


def test_furigana_base_strips_markup_and_squeezes_whitespace():
    assert furigana_base(" 表面[ひょうめん]が  白い ") == "表面が白い"


def test_furigana_base_is_empty_for_unusable_values():
    assert furigana_base("") == ""
    assert furigana_base("見[") == ""  # unbalanced: the writer mangled it
    assert furigana_base("[ひとにぎり]") == ""  # nothing outside the brackets


def test_furigana_base_refuses_html_ruby_markup():
    # HTML ruby is not the deck's bracket convention: its <rt> payload is a
    # reading, so leaving it in would corrupt the base (温あたためる).
    assert furigana_base("<ruby>温<rt>あたた</rt></ruby>める") == ""
    assert furigana_base("温<rt>あたた</rt>める") == ""


def test_kana_fold_matches_codepoint_spec_across_katakana_boundaries():
    from hypothesis import given
    from hypothesis import strategies as st

    alphabet = st.sampled_from(
        [
            *(chr(c) for c in range(0x30A0, 0x30FB)),  # katakana block including ゠, ァ..ヶ, ヷ..ー
            *(chr(c) for c in range(0x3041, 0x3097)),  # hiragana
            "漢",
            "A",
            " ",
        ]
    )

    @given(st.text(alphabet=alphabet, max_size=60))
    def check(text: str) -> None:
        expected = "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in text)
        assert kana_fold(text) == expected

    check()


# --- inline subtitle readings (issue #9) -------------------------------------


def test_detect_inline_readings_strips_annotation_and_reports_reading():
    sentence = "あそこの本町(ほんちゅう)通り…"
    cleaned, anns = detect_inline_readings(sentence)
    assert cleaned == "あそこの本町通り…"
    assert anns == [{"base": "本町", "reading": "ほんちゅう", "occurrence": 1}]


def test_detect_inline_readings_leaves_non_reading_parens():
    sentence = "あの歌(笑)を聞いた"
    cleaned, anns = detect_inline_readings(sentence)
    assert cleaned == sentence
    assert anns == []


def test_detect_inline_readings_multiple_annotations():
    sentence = "本町(ほんちゅう)と本町(ほんちょう)通り"
    cleaned, anns = detect_inline_readings(sentence)
    assert cleaned == "本町と本町通り"
    assert [a["reading"] for a in anns] == ["ほんちゅう", "ほんちょう"]
    assert [a["occurrence"] for a in anns] == [1, 2]


def test_detect_inline_readings_katakana_payload_normalized():
    sentence = "本町(ホンチュウ)通り"
    cleaned, anns = detect_inline_readings(sentence)
    assert cleaned == "本町通り"
    assert anns[0]["reading"] == "ほんちゅう"


def test_override_furigana_reading_merges_spans():
    generated = "あそこの 本[ほん]町[ちょう]通り…"
    cleaned = "あそこの 本町通り…"
    anns = [{"base": "本町", "reading": "ほんちゅう", "occurrence": 1}]
    result = override_furigana_reading(generated, cleaned, anns)
    assert result is not None
    assert "本町[ほんちゅう]" in result
    assert "通り" in result


def test_override_furigana_reading_single_span():
    generated = "あそこの 本町[ほんちょう]通り…"
    cleaned = "あそこの 本町通り…"
    anns = [{"base": "本町", "reading": "ほんちゅう", "occurrence": 1}]
    result = override_furigana_reading(generated, cleaned, anns)
    assert result is not None
    assert "本町[ほんちゅう]" in result


def test_override_furigana_reading_returns_none_on_misalignment():
    generated = "あそこの 本町通り[ほんちょうどおり]…"
    cleaned = "あそこの 本町通り…"
    anns = [{"base": "本町", "reading": "ほんちゅう", "occurrence": 1}]
    result = override_furigana_reading(generated, cleaned, anns)
    assert result is None


def test_override_furigana_reading_returns_none_when_base_missing():
    generated = "あそこの 通り…"
    cleaned = "あそこの 通り…"
    anns = [{"base": "本町", "reading": "ほんちゅう", "occurrence": 1}]
    result = override_furigana_reading(generated, cleaned, anns)
    assert result is None


def test_override_furigana_reading_validates_base_match():
    generated = "あそこの 本[ほん]町[ちょう]通り…"
    cleaned = "あそこの 本町通り…"
    anns = [{"base": "本町", "reading": "ほんちゅう", "occurrence": 1}]
    result = override_furigana_reading(generated, cleaned, anns)
    assert result is not None
    from mining_fixer.normalize import furigana_base_matches

    assert furigana_base_matches(result, cleaned)


def test_override_furigana_reading_merges_a_space_split_engine_span():
    # Japanese Support spaces its tokens, splitting a compound name across two
    # annotated spans (炭[たん] 治郎[じろう]) or leaving a reading next to a
    # plain fragment (禰豆 子[こ]). The span merge must find the base across
    # that whitespace; 144 of the deck's 322 inline-reading cards failed
    # exactly here before the space-collapsed match.
    generated = "( 炭[たん] 治郎[じろう])すみませんが 竹[たけ]を 頂[いただ]けますか?"
    cleaned = "(炭治郎)すみませんが 竹を 頂けますか?"
    anns = [{"base": "炭治郎", "reading": "たんじろう", "occurrence": 1}]
    result = override_furigana_reading(generated, cleaned, anns)
    assert result is not None
    # The engine's own spacing elsewhere is untouched: only the annotated
    # spans merge, and the leading space after the bracket stays.
    assert result == "( 炭治郎[たんじろう])すみませんが 竹[たけ]を 頂[いただ]けますか?"


def test_override_furigana_reading_refuses_to_split_one_reading():
    # 本町通り[ほんちょうどおり] is one engine reading: cutting 本町 out of it
    # would need the engine's reading of the base — a guess, so it is refused.
    generated = "あそこの 本町通り[ほんちょうどおり]"
    cleaned = "あそこの 本町通り"
    anns = [{"base": "本町", "reading": "ほんちゅう", "occurrence": 1}]
    assert override_furigana_reading(generated, cleaned, anns) is None


def test_detect_inline_readings_keeps_the_bold_markup_it_finds():
    # The subtitle bolds the word it annotates: <b>赤犬</b>(あかいぬ). The
    # annotation goes, the bold stays (markup-only change).
    sentence = "海軍大将 <b>赤犬</b>(あかいぬ)は<br>その恨みを利用した"
    cleaned, anns = detect_inline_readings(sentence)
    assert cleaned == "海軍大将 <b>赤犬</b>は<br>その恨みを利用した"
    assert anns == [{"base": "赤犬", "reading": "あかいぬ", "occurrence": 1}]
    # Idempotent: a cleaned sentence has nothing left to strip.
    assert detect_inline_readings(cleaned) == (cleaned, [])


def test_detect_inline_readings_accepts_full_width_parentheses():
    cleaned, anns = detect_inline_readings("あそこの本町（ほんちゅう）通り")
    assert cleaned == "あそこの本町通り"
    assert anns == [{"base": "本町", "reading": "ほんちゅう", "occurrence": 1}]


def test_detect_inline_readings_refuses_a_single_kanji_run():
    # A one-kanji run plus a short kana pair is almost always a fragment of a
    # longer name in this deck (三浦 -> 浦(みうら), 都樹子 -> 子(つきこ)), not
    # that character's own reading: it stays exactly as mined.
    sentence = "浦(みうら)さんは元気?"
    assert detect_inline_readings(sentence) == (sentence, [])


def test_strip_furigana_annotation_space_bug1():
    # Japanese Support pads token boundaries; strip padding inside [ ] readings.
    assert strip_furigana_annotation_space("[ TV]") == "[TV]"
    assert strip_furigana_annotation_space("[ TEL]（ベル）") == "[TEL]（ベル）"  # noqa: E501
    assert strip_furigana_annotation_space("漢字[ かんじ]") == "漢字[かんじ]"
    assert (
        strip_furigana_annotation_space(" 星野[ほしの] 好美[よしみ]")
        == " 星野[ほしの] 好美[よしみ]"
    )
    assert strip_furigana_annotation_space("[TV] ずっと") == "[TV] ずっと"
    assert strip_furigana_annotation_space("あの[") == "あの["  # unbalanced untouched
    # idempotent
    once = strip_furigana_annotation_space("[ TV] 漢[ かん]")
    assert strip_furigana_annotation_space(once) == once == "[TV] 漢[かん]"
