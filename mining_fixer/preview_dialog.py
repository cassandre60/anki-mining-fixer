"""Preview dialog: show the plan, accept rows, apply them.

Qt-facing module. It never decides anything itself — the plan comes from
``plan.py`` and the re-assertion from ``applier.py`` — so what the user sees
is exactly what is written.

Planning runs on Anki's background task manager (``plan_preview_in_background``)
and the table is a ``QAbstractTableModel``, so toggling the scope checkbox does
no SQLite, MeCab or widget construction on the UI thread. Results are
coalesced and cached per scope state by :class:`mining_fixer.replan.ReplanQueue`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aqt.qt import (
    QAbstractItemView,
    QAbstractTableModel,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFont,
    QHeaderView,
    QLabel,
    QLineEdit,
    QModelIndex,
    QPlainTextEdit,
    QPushButton,
    QSortFilterProxyModel,
    Qt,
    QTableView,
    QTabWidget,
    QTimer,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
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
    plan_preview_in_background,
    preflight_fields,
    read_state,
    write_run_log,
)
from .matching import NoteRecord
from .plan import PlannedWrite
from .replan import ReplanQueue, ReplanRequest
from .report import OP_LABELS, badge_explanation, guard_reason

_HEADERS = ["", "note", "field", "change", "source", "badges"]

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


class _ChangesModel(QAbstractTableModel):
    """One plan as model data; a reset is O(rows), never widget creation.

    Check state is data, not a ``QTableWidgetItem``: manual ticks survive every
    re-plan (``_overrides``) and the view only paints the visible rows.
    """

    checkStateChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict = {}
        self._writes: tuple[PlannedWrite, ...] = ()
        self._checks: dict[tuple[int, str], bool] = {}
        self._overrides: dict[tuple[int, str], bool] = {}
        self._labels: list[str] = []
        self._summaries: list[str] = []
        self._sources: list[str] = []
        self._badges: list[str] = []
        self._check_tips: list[str] = []
        self._badge_tips: list[str] = []

    def set_plan(
        self,
        writes: Sequence[PlannedWrite],
        records: Mapping[int, NoteRecord],
        cfg: dict,
    ) -> None:
        self.beginResetModel()
        try:
            self._cfg = cfg
            self._writes = tuple(writes)
            self._checks = {}
            self._labels = []
            self._summaries = []
            self._sources = []
            self._badges = []
            self._check_tips = []
            self._badge_tips = []
            seen: set[int] = set()
            for write in self._writes:
                key = (write.note_id, write.field)
                checked = self._overrides.get(key, bool(write.default_checked))
                self._checks[key] = checked

                record = records.get(write.note_id)
                expression = record.get("Expression") if record is not None else ""
                first = write.note_id not in seen
                seen.add(write.note_id)
                self._labels.append(
                    f"{expression or '(no expression)'} · {write.note_id}" if first else ""
                )
                self._summaries.append(_change_summary(write))
                self._sources.append(f"note {write.source_note_id}" if write.source_note_id else "")
                self._badges.append(", ".join(write.badges))
                self._check_tips.append(self._check_tip(write, checked))
                self._badge_tips.append(
                    "\n".join(f"• {badge_explanation(b)}" for b in write.badges)
                    if write.badges
                    else ""
                )
        finally:
            self.endResetModel()

    def _check_tip(self, write: PlannedWrite, checked: bool) -> str:
        default = bool(write.default_checked)
        reason = guard_reason(write, self._cfg)
        if checked:
            return "Checked by default" if default else f"Checked manually (originally: {reason})"
        return reason if not default else "Unchecked manually"

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008 - Qt signature
        return 0 if parent.isValid() else len(self._writes)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: B008 - Qt signature
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(_HEADERS)
        ):
            return _HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._writes)):
            return None
        row, column = index.row(), index.column()
        write = self._writes[row]
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            checked = self._checks.get((write.note_id, write.field), False)
            return Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return ""
            if column == 1:
                return self._labels[row]
            if column == 2:
                return write.field
            if column == 3:
                return self._summaries[row]
            if column == 4:
                return self._sources[row]
            if column == 5:
                return self._badges[row]
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 0:
                return self._check_tips[row]
            if column == 5:
                return self._badge_tips[row] or None
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or not index.isValid():
            return False
        if index.column() != 0 or not (0 <= index.row() < len(self._writes)):
            return False
        write = self._writes[index.row()]
        key = (write.note_id, write.field)
        checked = Qt.CheckState(value) == Qt.CheckState.Checked
        self._checks[key] = checked
        self._overrides[key] = checked
        self._check_tips[index.row()] = self._check_tip(write, checked)
        self.dataChanged.emit(
            index,
            index,
            [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.ToolTipRole],
        )
        self.checkStateChanged.emit()
        return True

    def checked_keys(self) -> list[tuple[int, str]]:
        return [key for key, checked in self._checks.items() if checked]

    def write_at(self, row: int) -> PlannedWrite:
        return self._writes[row]


class _ChangesFilter(QSortFilterProxyModel):
    """Case-insensitive substring filter over note, field, change and badges."""

    def __init__(self) -> None:
        super().__init__()
        self._needle = ""

    def set_needle(self, text: str) -> None:
        needle = (text or "").strip().lower()
        if needle != self._needle:
            self._needle = needle
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:
        if not self._needle:
            return True
        model = self.sourceModel()
        if model is None:
            return True
        for column in (1, 2, 3, 5):
            value = model.data(model.index(source_row, column), Qt.ItemDataRole.DisplayRole)
            if self._needle in str(value or "").lower():
                return True
        return False


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
        self.cfg = dict(cfg) if cfg is not None else load_config()
        self.browser = browser
        self.scope_note_ids = list(scope_note_ids) if scope_note_ids is not None else None
        self.pending: tuple | None = None
        self.preview: Preview | None = None
        self.records_by_id: dict[int, NoteRecord] = {}
        self._queue: ReplanQueue[Preview] = ReplanQueue()
        self._closed = False
        self._busy = False
        self._body_built = False
        self._error_label: QLabel | None = None

        self.setWindowTitle("Mining Fixer — preview")
        self.resize(1180, 680)
        self.root = QVBoxLayout(self)

        self.neighbour_box = QCheckBox(NEIGHBOUR_LABEL)
        self.neighbour_box.setChecked(bool(self.cfg.get("fix_neighbours", True)))
        self.neighbour_box.setToolTip(
            "Also fixes the cards mined from the same subtitle line (they share "
            "this scene's Sentence Audio), and a selected fragment's scene "
            "parent. Unticked, only the selected cards are changed."
        )
        self.neighbour_box.toggled.connect(self._on_scope_toggled)
        self.root.addWidget(self.neighbour_box)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.root.addWidget(self.body)

        self.status = QLabel("Planning…")
        self.root.addWidget(self.status)

        box = QDialogButtonBox()
        self.apply_button = QPushButton("Apply")
        self.apply_button.setDefault(True)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._on_apply)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)
        box.addButton(self.apply_button, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(close_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.root.addWidget(box)

        # Debounce a burst of toggles; the queue coalesces what still arrives.
        self._replan_timer = QTimer(self)
        self._replan_timer.setSingleShot(True)
        self._replan_timer.setInterval(120)
        self._replan_timer.timeout.connect(self._request_current_scope)

        self._request_scope(bool(self.cfg.get("fix_neighbours", True)))

    # --- planning -----------------------------------------------------

    def _on_scope_toggled(self, checked: bool) -> None:
        """React to the checkbox immediately; the plan itself runs off-thread."""
        self.cfg = {**self.cfg, "fix_neighbours": bool(checked)}
        self._set_busy(True)
        self._replan_timer.start()

    def _request_current_scope(self) -> None:
        self._request_scope(bool(self.neighbour_box.isChecked()))

    def _request_scope(self, state: bool) -> None:
        request = self._queue.request(state)
        if request.cached is not None:
            self._adopt(request.cached, state)
            self._set_busy(False)
            return
        if not request.start:
            self._set_busy(True)
            return
        self._start_worker(request)

    def _start_worker(self, request: ReplanRequest[Preview]) -> None:
        self._set_busy(True)
        cfg = {**self.cfg, "fix_neighbours": request.state}
        records = list(self.records_by_id.values()) if self.records_by_id else None
        started = plan_preview_in_background(
            self,
            cfg,
            self.scope_note_ids,
            records,
            lambda preview: self._worker_done(request, preview),
            lambda exc: self._worker_failed(request, exc),
        )
        if started:
            return
        # Older host or no task manager: keep the add-on working synchronously.
        try:
            preview = plan_preview(self.col, cfg, self.scope_note_ids, records=records)
        except Exception as exc:
            self._worker_failed(request, exc)
            return
        self._worker_done(request, preview)

    def _worker_done(self, request: ReplanRequest[Preview], preview: Preview) -> None:
        if self._closed:
            return
        shown, following = self._queue.finish(request.token, request.state, preview)
        if shown is not None:
            self._adopt(shown, request.state)
        if following is not None:
            self._start_worker(following)
        else:
            self._set_busy(False)

    def _worker_failed(self, request: ReplanRequest[Preview], exc: Exception) -> None:
        if self._closed:
            return
        following = self._queue.fail(request.token, request.state)
        if following is not None:
            self._start_worker(following)
            return
        if self.preview is not None and not self.preview.error and not self._busy:
            # A cached plan for the newest state is already on screen; this
            # failed run was superseded, so keep showing it.
            self._set_busy(False)
            return
        self._set_busy(False)
        self.status.setText(f"Re-planning failed: {exc}")
        self.apply_button.setEnabled(False)

    def _adopt(self, preview: Preview, state: bool) -> None:
        self.preview = preview
        self.cfg = {**self.cfg, "fix_neighbours": state}
        if preview.records_by_id:
            self.records_by_id = preview.records_by_id
        if preview.error:
            self._show_error(preview.error)
            return
        if not self._body_built:
            self._build_body(preview)
        else:
            self._update_body(preview)

    def _show_error(self, message: str) -> None:
        if self._error_label is None:
            label = QLabel(message)
            label.setWordWrap(True)
            self.body_layout.addWidget(label)
            self._error_label = label
        else:
            self._error_label.setText(message)
        self.status.setText(message)
        self.apply_button.setEnabled(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.status.setText("Re-planning…")
            self.apply_button.setEnabled(False)
        else:
            self._refresh_status()

    # --- body ---------------------------------------------------------

    def _build_body(self, preview: Preview) -> None:
        plan = preview.plan
        assert plan is not None
        self.header_label = QLabel()
        font = QFont()
        font.setBold(True)
        self.header_label.setFont(font)
        self.body_layout.addWidget(self.header_label)

        self.excluded_label = QLabel()
        self.excluded_label.setWordWrap(True)
        self.excluded_label.setVisible(False)
        self.body_layout.addWidget(self.excluded_label)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._changes_page(), "Proposed changes")
        self.reasons_view = self._text_view()
        self.tabs.addTab(self.reasons_view, "Not fixable")
        self.warnings_view = self._text_view()
        self.tabs.addTab(self.warnings_view, "Warnings")
        self.body_layout.addWidget(self.tabs)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(150)
        self.body_layout.addWidget(self.detail)

        self._body_built = True
        self._update_body(preview)

    def _changes_page(self) -> QWidget:
        page = QWidget()
        inner = QVBoxLayout(page)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter by expression, field, or badge…")
        self.filter_input.setClearButtonEnabled(True)
        inner.addWidget(self.filter_input)

        self.model = _ChangesModel()
        self.proxy = _ChangesFilter()
        self.proxy.setSourceModel(self.model)
        self.filter_input.textChanged.connect(self.proxy.set_needle)
        self.model.checkStateChanged.connect(self._refresh_status)

        table = QTableView()
        table.setModel(self.proxy)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(False)
        vertical = table.verticalHeader()
        assert vertical is not None
        vertical.setVisible(False)
        header = table.horizontalHeader()
        assert header is not None
        for column in range(len(_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(0, 28)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        selection = table.selectionModel()
        assert selection is not None
        selection.selectionChanged.connect(self._on_selection_changed)
        self.table = table
        inner.addWidget(table)
        return page

    def _text_view(self) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setReadOnly(True)
        return view

    def _update_body(self, preview: Preview) -> None:
        plan = preview.plan
        assert plan is not None
        if self._error_label is not None:
            self._error_label.setVisible(False)
        checked = sum(1 for w in plan.writes if w.default_checked)
        self.header_label.setText(
            f"{preview.scope_label} · {len(plan.writes)} proposed writes · "
            f"{checked} checked by default · {len(plan.reasons)} not fixable · "
            f"{len(plan.warnings)} warning(s)"
        )
        self.excluded_label.setVisible(bool(preview.excluded))
        if preview.excluded:
            self.excluded_label.setText(
                f"{len(preview.excluded)} selected note(s) use a different note type "
                "and were skipped."
            )

        self.tabs.setTabText(0, f"Proposed changes ({len(plan.writes)})")
        self.reasons_view.setPlainText(
            "\n".join(self._reason_lines(preview)) or "Nothing to report."
        )
        self.tabs.setTabText(1, f"Not fixable ({len(plan.reasons)})")
        self.warnings_view.setPlainText("\n".join(plan.warnings) or "Nothing to report.")
        self.tabs.setTabText(2, f"Warnings ({len(plan.warnings)})")

        self.model.set_plan(plan.writes, preview.records_by_id or {}, self.cfg)
        self.detail.setPlainText("")
        self._refresh_status()

    def _reason_lines(self, preview: Preview) -> list[str]:
        plan = preview.plan
        records = preview.records_by_id or {}
        assert plan is not None
        lines = []
        for note_id, reason in sorted(plan.reasons.items()):
            record = records.get(note_id)
            expression = record.get("Expression") if record is not None else ""
            lines.append(f"{expression or '(no expression)'} · {note_id}: {reason}")
        return lines

    # --- interaction --------------------------------------------------

    def _on_selection_changed(self, *_args) -> None:
        if not self._body_built:
            return
        selection = self.table.selectionModel()
        if selection is None:
            return
        indexes = selection.selectedRows()
        if not indexes:
            return
        source = self.proxy.mapToSource(indexes[0])
        if not source.isValid():
            return
        write = self.model.write_at(source.row())
        lines = [
            f"note {write.note_id} · {write.field} · {OP_LABELS.get(write.op, write.op)}",
            f"badges: {', '.join(write.badges) or '(none)'}",
        ]
        if write.badges:
            lines.append("")
            for badge in write.badges:
                lines.append(f"  • {badge}: {badge_explanation(badge)}")
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

    def _refresh_status(self) -> None:
        count = len(self.model.checked_keys()) if self._body_built else 0
        self.apply_button.setText(f"Apply {count} checked change(s)")
        self.apply_button.setEnabled(not self._busy and count > 0)
        if self._busy or not self._body_built or self.preview is None:
            return
        if self.preview.error:
            return
        applied_tag = self.cfg.get("applied_tag")
        note = (
            f" · cards that finish with nothing left to do get the tag “{applied_tag}”"
            if applied_tag
            else ""
        )
        self.status.setText(f"{count} row(s) selected{note}")

    # --- apply --------------------------------------------------------

    def _on_apply(self) -> None:
        if self.preview is None:
            return
        plan = self.preview.plan
        if plan is None:
            return
        checked_keys = set(self.model.checked_keys())
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

    def done(self, result: int) -> None:
        """Stop accepting worker callbacks once the dialog is gone."""
        self._closed = True
        self._replan_timer.stop()
        super().done(result)


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
