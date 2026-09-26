"""Read the live caret/selection of an ImGui input_text (e59s05).

DearPyGui's public API exposes neither the caret nor the selection of an
``input_text`` (upstream hoffstadt/DearPyGui#1650, #173, #2555). The installed
wheel, however, is NOT stripped: it exports ImGui's own
``ImGuiInputTextState::GetCursorPos / GetSelectionStart / GetSelectionEnd``
getters and ``ImGui::GetCurrentContext`` (ImGui 1.92.5). This module resolves
those symbols with ``ctypes`` and finds the single live state through its
``ImGuiContext`` back-pointer.

Linux/ELF only; every step is validated and any problem returns ``None``, so the
caller keeps its own fallback. No dpg widget call, no project state, no network:
it is safe to import and unit-test headless (HIGH-1). See
``specs/adr/ADR-text-caret-ctypes.md`` and
``specs/TEXT_EDITOR_CARET_SURVEY_LATEST.md`` for the evidence and guardrails.
"""

from __future__ import annotations

import ctypes
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Field offsets inside ImGuiInputTextState (ImGui 1.92.5, verified in the binary):
# Ctx(0) Stb(8) Flags(16) ID(20) TextLen(24).
_STATE_OFFSET_CTX = 0
_STATE_OFFSET_STB = 8
_STATE_OFFSET_WIDGET_ID = 20
_STATE_OFFSET_TEXT_LEN = 24
_STATE_HEADER_BYTES = _STATE_OFFSET_TEXT_LEN + 4
# The scan step and the sanity bounds; a candidate outside these is not a state.
_CANDIDATE_STEP = 8
_MIN_STB_BYTES = 32
_MAX_TEXT_LEN = 1_000_000

_EXPORTED_SYMBOLS: dict[str, str] = {
    "context": "_ZN5ImGui17GetCurrentContextEv",
    "cursor": "_ZNK19ImGuiInputTextState12GetCursorPosEv",
    "selection_start": "_ZNK19ImGuiInputTextState17GetSelectionStartEv",
    "selection_end": "_ZNK19ImGuiInputTextState15GetSelectionEndEv",
}


def symbol_names() -> tuple[str, ...]:
    """The mangled ImGui symbols this module needs (for presence checks)."""
    return tuple(_EXPORTED_SYMBOLS.values())


@dataclass(frozen=True)
class CaretState:
    """One validated read of the ImGui InputText state."""

    widget_id: int
    cursor: int
    selection_start: int
    selection_end: int

    @property
    def selection(self) -> tuple[int, int] | None:
        """The normalised (lo, hi) selected range, or None when empty."""
        if self.selection_start == self.selection_end:
            return None
        low, high = sorted((self.selection_start, self.selection_end))
        return (low, high)


class CaretBackend(Protocol):
    """The raw memory/symbol access the reader needs (injected for tests)."""

    def context_pointer(self) -> int: ...
    def allocation_size(self, address: int) -> int: ...
    def readable(self, address: int, size: int) -> bool: ...
    def read_u32(self, address: int) -> int: ...
    def read_i32(self, address: int) -> int: ...
    def read_u64(self, address: int) -> int: ...
    def cursor_pos(self, state: int) -> int: ...
    def selection_start(self, state: int) -> int: ...
    def selection_end(self, state: int) -> int: ...


class CaretReader:
    """Locate and read the ImGui InputText state; ``None`` when unavailable."""

    def __init__(self, backend: CaretBackend) -> None:
        self._backend = backend
        self._context = 0
        self._state_address = 0

    def read(self, expected_widget_id: int | None = None) -> CaretState | None:
        """The current caret/selection, or None (never raises).

        ``expected_widget_id`` rejects a state owned by a DIFFERENT input: the
        ImGui state slot is single-slot and is reused by any other InputText
        (combo filter, colour popup, another field).
        """
        try:
            address = self._resolve_state_address()
            if address is None:
                return None
            return self._validate_state(address, expected_widget_id)
        except Exception:  # a probe must never break the caller
            return None

    def _resolve_state_address(self) -> int | None:
        context = int(self._backend.context_pointer())
        if context <= 0:
            self._context, self._state_address = 0, 0
            return None
        if context == self._context and self._state_address:
            return self._state_address
        address = self._find_state(context)
        self._context, self._state_address = context, address or 0
        return address

    def _find_state(self, context: int) -> int | None:
        size = int(self._backend.allocation_size(context))
        limit = size - _STATE_HEADER_BYTES
        if limit <= 0:
            return None
        for offset in range(0, limit, _CANDIDATE_STEP):
            address = context + offset
            if not self._backend.readable(address, _STATE_HEADER_BYTES):
                break  # past the context allocation
            if self._backend.read_u64(address + _STATE_OFFSET_CTX) != context:
                continue
            if self._validate_state(address, None) is not None:
                return address
        return None

    def _validate_state(self, address: int, expected_widget_id: int | None) -> CaretState | None:
        if not self._backend.readable(address, _STATE_HEADER_BYTES):
            return None
        text_len = self._backend.read_i32(address + _STATE_OFFSET_TEXT_LEN)
        if not 0 <= text_len <= _MAX_TEXT_LEN:
            return None
        widget_id = self._backend.read_u32(address + _STATE_OFFSET_WIDGET_ID)
        if expected_widget_id is not None and widget_id != expected_widget_id:
            return None
        stb = self._backend.read_u64(address + _STATE_OFFSET_STB)
        if not self._backend.readable(stb, _MIN_STB_BYTES):
            return None
        cursor = int(self._backend.cursor_pos(address))
        start = int(self._backend.selection_start(address))
        end = int(self._backend.selection_end(address))
        if not all(0 <= value <= text_len for value in (cursor, start, end)):
            return None
        return CaretState(widget_id, cursor, start, end)


def _load_shared(path: str) -> Any:
    return ctypes.CDLL(path)


def _int_getter(library: Any, symbol: str) -> Any:
    function = library[symbol]
    function.restype = ctypes.c_int
    function.argtypes = [ctypes.c_void_p]
    return function


def _dearpygui_extension_path() -> str | None:
    try:
        import dearpygui
    except ImportError:
        return None
    module_file = getattr(dearpygui, "__file__", None)
    if not module_file:
        return None  # a stubbed module (tests) has no real extension
    candidate = Path(module_file).resolve().parent / "_dearpygui.so"
    return str(candidate) if candidate.is_file() else None


def _parse_readable_ranges() -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    try:
        with open("/proc/self/maps", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 2 or "r" not in parts[1]:
                    continue
                start, end = parts[0].split("-")
                ranges.append((int(start, 16), int(end, 16)))
    except (OSError, ValueError):
        return []
    return ranges


class CtypesImGuiBackend:
    """The real backend: resolves the ImGui symbols from the loaded binary."""

    def __init__(self, library: Any, libc: Any) -> None:
        self._library = library
        self._readable_ranges: list[tuple[int, int]] | None = None
        get_context = library[_EXPORTED_SYMBOLS["context"]]
        get_context.restype = ctypes.c_void_p
        get_context.argtypes = []
        self._get_context = get_context
        self._cursor = _int_getter(library, _EXPORTED_SYMBOLS["cursor"])
        self._selection_start = _int_getter(library, _EXPORTED_SYMBOLS["selection_start"])
        self._selection_end = _int_getter(library, _EXPORTED_SYMBOLS["selection_end"])
        self._malloc_usable_size = libc.malloc_usable_size
        self._malloc_usable_size.restype = ctypes.c_size_t
        self._malloc_usable_size.argtypes = [ctypes.c_void_p]

    @classmethod
    def from_environment(
        cls, extension_path: str | None = None, *, load: Any = None
    ) -> CtypesImGuiBackend | None:
        """Build the backend, or None off Linux / when a symbol is missing."""
        if platform.system() != "Linux":
            return None
        path = extension_path or _dearpygui_extension_path()
        if not path:
            return None
        loader = load or _load_shared
        try:
            library = loader(path)
            if any(not hasattr(library, name) for name in symbol_names()):
                return None
            libc = loader("libc.so.6")
        except (OSError, AttributeError, TypeError):
            return None
        return cls(library, libc)

    # --- CaretBackend -----------------------------------------------------
    def context_pointer(self) -> int:
        return int(self._get_context() or 0)

    def allocation_size(self, address: int) -> int:
        return int(self._malloc_usable_size(ctypes.c_void_p(address)))

    def readable(self, address: int, size: int) -> bool:
        if address <= 0 or size < 0:
            return False
        if self._readable_ranges is None:
            self._readable_ranges = _parse_readable_ranges()
        return any(
            start <= address and address + size <= end for start, end in self._readable_ranges
        )

    def read_u32(self, address: int) -> int:
        return int(ctypes.c_uint32.from_address(address).value)

    def read_i32(self, address: int) -> int:
        return int(ctypes.c_int32.from_address(address).value)

    def read_u64(self, address: int) -> int:
        return int(ctypes.c_uint64.from_address(address).value)

    def cursor_pos(self, state: int) -> int:
        return int(self._cursor(ctypes.c_void_p(state)))

    def selection_start(self, state: int) -> int:
        return int(self._selection_start(ctypes.c_void_p(state)))

    def selection_end(self, state: int) -> int:
        return int(self._selection_end(ctypes.c_void_p(state)))
