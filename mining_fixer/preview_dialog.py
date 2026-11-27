"""Preview dialog: show the plan, accept rows, apply them.

Qt-facing module. It never decides anything itself — the plan comes from
``plan.py`` and the re-assertion from ``applier.py`` — so what the user sees
is exactly what is written.
"""

from __future__ import annotations

from collections.abc import Mapping

from aqt.qt import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFont,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimer,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import showCritical, showInfo, tooltip

from .applier import ApplyDecision, run_after_editor_saved, verify_writes
from .collection_io import (
    Preview,
    apply_decision,
    apply_plan_to_fixpoint,
    build_decision,
    load_config,
    media_exists,
    notify_collection_changed,
    plan_preview,
    preflight_fields,
    read_state,
    write_run_log,
)
from .matching import NoteRecord
from .plan import PlannedWrite
from .report import OP_LABELS, badge_explanation, guard_reason

_HEADERS = ["", "note", "field", "change", "source", "badges"]
_CHECK_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 2

#: The scope checkbox (B12), worded so the user knows exactly what it widens.
NEIGHBOUR_LABEL = "Also fix the other cards mined from the same subtitle line"


def _clip(text: str, limit: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _change_summary(write: PlannedWrite) -> str:
    if write.op == "add_tags":
        added = " ".join(sorted(set(write.new_value.split()) - set(write.old_value.split())))
        base = write.old_value or "(no tags)"
        return _clip(f"{base}  +  {added}")
    if write.old_value:
        return _clip(f"{write.old_value}  →  {write.new_value}")
    return _clip(write.new_value)


class PreviewDialog(QDialog):
    """The add-on's main window: one row per planned write, checked by default
    exactly as the plan says (guarded rows arrive unchecked)."""

    def __init__(
        self,
        col,
        cfg: dict | None = None,
        scope_note_ids=None,
        browser=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.col = col
        self.cfg = cfg if cfg is not None else load_config()
        self.browser = browser
        self.scope_note_ids = list(scope_note_ids) if scope_note_ids is not None else None
        self.preview: Preview = plan_preview(col, self.cfg, self.scope_note_ids)
        self.pending: tuple | None = None
        self.checks: dict[tuple[int, str], bool] = {}

        self.setWindowTitle("Mining Fixer — preview")
        self.resize(1180, 680)
        self.root = QVBoxLayout(self)
        self.content: QWidget | None = None
        self._install_content()

    def _install_content(self) -> None:
        """Build the dialog body for the current plan (re-run on a scope change)."""
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.root.addWidget(self.content)
        self._populate()

    def _replan(self) -> None:
        """Re-plan for the new scope; the preview is always the truth (Q-C4)."""
        self.cfg = {**self.cfg, "fix_neighbours": bool(self.neighbour_box.isChecked())}
        self.preview = plan_preview(self.col, self.cfg, self.scope_note_ids)
        self.checks = {}
        old = self.content
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        self._install_content()

    def _populate(self) -> None:
        inner = self.content_layout
        neighbour_box = QCheckBox(NEIGHBOUR_LABEL)
        neighbour_box.setChecked(bool(self.cfg.get("fix_neighbours", True)))
        neighbour_box.setToolTip(
            "Also fixes the cards mined from the same subtitle line (they share "
            "this scene's Sentence Audio), and a selected fragment's scene "
            "parent. Unticked, only the selected cards are changed."
        )
        neighbour_box.toggled.connect(lambda _checked: self._replan())
        self.neighbour_box = neighbour_box
        inner.addWidget(neighbour_box)

        if self.preview.error:
            inner.addWidget(QLabel(self.preview.error))
            inner.addWidget(self._close_box())
            return

        plan = self.preview.plan
        assert plan is not None
        checked = sum(1 for w in plan.writes if w.default_checked)
        summary = (
            f"{self.preview.scope_label} · {len(plan.writes)} proposed writes · "
            f"{checked} checked by default · {len(plan.reasons)} not fixable · "
            f"{len(plan.warnings)} warning(s)"
        )
        header = QLabel(summary)
        font = QFont()
        font.setBold(True)
        header.setFont(font)
        inner.addWidget(header)
        if self.preview.excluded:
            excluded = QLabel(
                f"{len(self.preview.excluded)} selected note(s) use a different note type "
                "and were skipped."
            )
            inner.addWidget(excluded)

        tabs = QTabWidget()
        tabs.addTab(self._changes_page(plan.writes), f"Proposed changes ({len(plan.writes)})")
        tabs.addTab(
            self._text_page(self._reason_lines()),
            f"Not fixable ({len(plan.reasons)})",
        )
        tabs.addTab(
            self._text_page(list(plan.warnings)),
            f"Warnings ({len(plan.warnings)})",
        )
        inner.addWidget(tabs)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(150)
        inner.addWidget(self.detail)

        self.status = QLabel("")
        inner.addWidget(self.status)

        box = self._action_box()
        inner.addWidget(box)
        self._refresh_status()

    # --- page builders ------------------------------------------------

    def _changes_page(self, writes) -> QWidget:
        page = QWidget()
        inner = QVBoxLayout(page)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter by expression, field, or badge…")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._apply_filter)
        inner.addWidget(self.filter_input)

        table = QTableWidget(0, len(_HEADERS))
        table.setHorizontalHeaderLabels(_HEADERS)
        vheader = table.verticalHeader()
        if vheader is not None:
            vheader.setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(False)
        header = table.horizontalHeader()
        if header is not None:
            for column in range(len(_HEADERS)):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        records = self.preview.records_by_id or {}
        seen: set[int] = set()
        table.setRowCount(len(writes))
        for index, write in enumerate(writes):
            key = (write.note_id, write.field)
            self.checks[key] = bool(write.default_checked)

            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            check_item.setCheckState(
                Qt.CheckState.Checked if write.default_checked else Qt.CheckState.Unchecked
            )
            check_item.setData(_KEY_ROLE, f"{key[0]}|{key[1]}")
            check_item.setToolTip(guard_reason(write, self.cfg))
            table.setItem(index, 0, check_item)

            record = records.get(write.note_id)
            expression = record.get("Expression") if record is not None else ""
            label = f"{expression or '(no expression)'} · {write.note_id}"
            table.setItem(index, 1, QTableWidgetItem(label if write.note_id not in seen else ""))
            seen.add(write.note_id)
            table.setItem(index, 2, QTableWidgetItem(write.field))
            table.setItem(index, 3, QTableWidgetItem(_change_summary(write)))
            source = f"note {write.source_note_id}" if write.source_note_id else ""
            table.setItem(index, 4, QTableWidgetItem(source))

            badge_text = ", ".join(write.badges)
            badge_item = QTableWidgetItem(badge_text)
            if write.badges:
                tooltip = "\n".join(f"• {badge_explanation(b)}" for b in write.badges)
                badge_item.setToolTip(tooltip)
            table.setItem(index, 5, badge_item)

        table.itemChanged.connect(self._on_item_changed)
        table.itemSelectionChanged.connect(lambda: self._show_detail(table))
        self.table = table
        inner.addWidget(table)
        return page

    def _text_page(self, lines: list[str]) -> QWidget:
        page = QWidget()
        inner = QVBoxLayout(page)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(lines) if lines else "Nothing to report.")
        inner.addWidget(view)
        return page

    def _reason_lines(self) -> list[str]:
        plan = self.preview.plan
        records = self.preview.records_by_id or {}
        assert plan is not None
        lines = []
        for note_id, reason in sorted(plan.reasons.items()):
            record = records.get(note_id)
            expression = record.get("Expression") if record is not None else ""
            lines.append(f"{expression or '(no expression)'} · {note_id}: {reason}")
        return lines

    # --- interaction --------------------------------------------------

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or self.table.itemChanged is None:
            return
        raw = item.data(_KEY_ROLE)
        if not raw:
            return
        note_id, field = str(raw).split("|", 1)
        is_checked = item.checkState() == Qt.CheckState.Checked
        self.checks[(int(note_id), field)] = is_checked

        plan = self.preview.plan
        if plan is not None and 0 <= item.row() < len(plan.writes):
            write = plan.writes[item.row()]
            default_checked = bool(write.default_checked)
            reason = guard_reason(write, self.cfg)
            if is_checked:
                new_tip = (
                    "Checked by default"
                    if default_checked
                    else f"Checked manually (originally: {reason})"
                )
            else:
                new_tip = reason if not default_checked else "Unchecked manually"
            if item.toolTip() != new_tip:
                self.table.blockSignals(True)
                try:
                    item.setToolTip(new_tip)
                finally:
                    self.table.blockSignals(False)

        self._refresh_status()

    def _show_detail(self, table: QTableWidget) -> None:
        plan = self.preview.plan
        if plan is None:
            return
        rows = {index.row() for index in table.selectedIndexes()}
        if not rows:
            return
        write = plan.writes[min(rows)]
        lines = [
            f"note {write.note_id} · {write.field} · {OP_LABELS.get(write.op, write.op)}",
            f"badges: {', '.join(write.badges) or '(none)'}",
        ]
        if write.badges:
            lines.append("")
            for b in write.badges:
                lines.append(f"  • {b}: {badge_explanation(b)}")
        lines.extend(
            [
                "",
                "current:",
                write.old_value or "(empty)",
                "",
                "after:",
                write.new_value,
            ]
        )
        self.detail.setPlainText("\n".join(lines))

    def _checked_keys(self) -> list[tuple[int, str]]:
        return [key for key, on in self.checks.items() if on]

    def _apply_filter(self, text: str) -> None:
        """Show only table rows whose expression, field, or badges contain *text*."""
        needle = text.strip().lower()
        for row in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row, False)
                continue
            visible = False
            for col in (1, 2, 5):  # note/expression, field, badges
                item = self.table.item(row, col)
                if item is not None and needle in (item.text() or "").lower():
                    visible = True
                    break
            if not visible:
                # Also search the change summary column
                item = self.table.item(row, 3)
                if item is not None and needle in (item.text() or "").lower():
                    visible = True
            self.table.setRowHidden(row, not visible)

    def _refresh_status(self) -> None:
        count = len(self._checked_keys())
        applied_tag = self.cfg.get("applied_tag")
        note = (
            f" · cards that finish with nothing left to do get the tag “{applied_tag}”"
            if applied_tag
            else ""
        )
        self.status.setText(f"{count} row(s) selected{note}")
        if hasattr(self, "apply_button"):
            self.apply_button.setText(f"Apply {count} checked change(s)")
            self.apply_button.setEnabled(count > 0)

    def _action_box(self) -> QDialogButtonBox:
        box = QDialogButtonBox()
        self.apply_button = QPushButton("Apply")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self._on_apply)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)
        box.addButton(self.apply_button, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(close_button, QDialogButtonBox.ButtonRole.RejectRole)
        return box

    def _close_box(self) -> QDialogButtonBox:
        box = QDialogButtonBox()
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)
        box.addButton(close_button, QDialogButtonBox.ButtonRole.RejectRole)
        return box

    # --- apply --------------------------------------------------------

    def _on_apply(self) -> None:
        plan = self.preview.plan
        if plan is None:
            return
        checked_keys = set(self._checked_keys())
        declined_keys = frozenset({(w.note_id, w.field) for w in plan.writes} - checked_keys)
        decision = build_decision(self.col, plan, checked_keys, self.cfg)
        if not decision.row_count() and not decision.skipped:
            showInfo("Nothing to apply: no row is selected.", parent=self)
            return
        # Nothing is written here: this slot runs inside the dialog's nested
        # event loop, and Anki's editor may still be saving. The decision is
        # handed to open_preview_dialog(), which writes after this dialog has
        # closed and the editor has flushed (see applier.run_after_editor_saved).
        self.pending = (
            decision,
            {
                "run": plan.version,
                "deck": self.cfg.get("deck_name"),
                "scope": list(self.preview.scope),
                "rows": decision.row_count(),
            },
            self.cfg,
            tuple(self.preview.scope),
            declined_keys,
            self.preview.records_by_id,
        )
        self.accept()


def _finish_apply(
    col,
    decision: ApplyDecision,
    meta: dict,
    browser=None,
    parent=None,
    cfg: dict | None = None,
    scope: tuple[int, ...] = (),
    excluded_keys: frozenset[tuple[int, str]] = frozenset(),
    initial_records: Mapping[int, NoteRecord] | None = None,
) -> None:
    """Preflight, log, write to fixpoint, verify — outside the dialog (Q-A2/Q-A5)."""
    field_problems = preflight_fields(col, decision)
    if field_problems:
        showInfo(
            "Nothing was written — the configured fields do not match this note type:\n\n"
            + "\n".join(field_problems),
            parent=parent,
        )
        return
    if cfg is not None and scope:
        result = apply_plan_to_fixpoint(
            col,
            cfg,
            scope,
            meta,
            first_decision=decision,
            take_backup=False,
            excluded_keys=excluded_keys,
            initial_records=initial_records,
        )
        writes = result.writes
        modified = result.modified
        log_path = result.log_path
        problems = result.problems
    else:
        # Legacy path (no cfg/scope): the decision is written as re-asserted.
        # The clean-only ``applied_tag`` belongs to the fixpoint controller.
        log_path = write_run_log(decision, meta) if decision.row_count() else None
        modified = apply_decision(col, decision)
        writes = decision.row_count()
        problems = verify_writes(
            decision,
            lambda nid: read_state(col, nid),
            media_exists=lambda name: media_exists(col, name),
        )
    if modified:
        notify_collection_changed(browser=browser)
    lines = [f"Applied {writes} write(s) to {modified} note(s)."]
    if decision.skipped:
        by_reason: dict[str, int] = {}
        for skipped in decision.skipped:
            by_reason[skipped.reason] = by_reason.get(skipped.reason, 0) + 1
        detail = ", ".join(f"{count} {reason}" for reason, count in sorted(by_reason.items()))
        lines.append(f"Skipped {len(decision.skipped)} note(s): {detail}.")
    if log_path is not None:
        lines.append(
            f"Undo available: Tools → “Mining Fixer: undo last run”.\nLog: {log_path.name}"
        )
    if problems:
        lines.append("VERIFY PROBLEMS — undo and report these:")
        lines.extend(f"  {problem}" for problem in problems)
    else:
        lines.append("Verified: every written value matches the plan.")
    if problems or decision.skipped:
        showInfo("\n".join(lines), parent=parent)
    else:
        tooltip(f"Mining Fixer: applied {writes} write(s) to {modified} note(s).", parent=parent)


def apply_pending(col, pending, browser=None, parent=None) -> None:
    """Write the accepted rows once the host is quiescent.

    The editor flush comes first so a pending Browser save cannot land on top
    of the values this run writes. The 1.5 s safety net only fires if Anki
    loses the save callback (torn-down editor); the write is once-only either
    way.
    """
    if len(pending) == 6:
        decision, meta, cfg, scope, excluded_keys, initial_records = pending
    elif len(pending) == 5:
        decision, meta, cfg, scope, excluded_keys = pending
        initial_records = None
    else:
        decision, meta = pending
        cfg, scope, excluded_keys, initial_records = None, (), frozenset(), None
    run_after_editor_saved(
        browser,
        lambda: _finish_apply(
            col,
            decision,
            meta,
            browser=browser,
            parent=parent,
            cfg=cfg,
            scope=scope,
            excluded_keys=excluded_keys,
            initial_records=initial_records,
        ),
        schedule=lambda once: QTimer.singleShot(1500, once),
    )


def open_preview_dialog(scope_note_ids=None, browser=None) -> None:
    """Menu entry point: preview, then apply once the dialog has closed.

    ``scope_note_ids`` is the Browser selection when called from the context
    menu; omitted, the dialog plans the newest ``preview_last_n`` notes.
    ``browser`` (when known) is used to flush its editor before writing, so a
    pending editor save cannot overwrite this run.
    """
    from aqt import mw

    if mw is None or mw.col is None:
        showCritical("Open a profile first.")
        return
    if browser is None:
        try:
            from aqt import dialogs

            browser = (
                dialogs.getInstance("Browser")
                or dialogs.getInstance("EditCurrent")
                or dialogs.getInstance("NewEditCurrent")
            )
        except Exception:
            browser = None
    dialog = PreviewDialog(mw.col, load_config(), scope_note_ids, browser=browser, parent=mw)
    dialog.exec()
    if dialog.pending is not None:
        apply_pending(mw.col, dialog.pending, browser=browser, parent=mw)
