"""Keyboard input source (layout / input method) via HIToolbox TIS.

pyobjc does not wrap TIS, so it is called through ctypes; CF objects are
bridged to and from Python with objc.objc_object / objc.pyobjc_id.
"""

import ctypes
from typing import Any

import Foundation
import objc

_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_carbon.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
_carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
_carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_carbon.TISCreateInputSourceList.restype = ctypes.c_void_p
_carbon.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
_carbon.TISSelectInputSource.restype = ctypes.c_int32
_carbon.TISSelectInputSource.argtypes = [ctypes.c_void_p]
_cf.CFRelease.argtypes = [ctypes.c_void_p]


def _constant(name: str) -> int:
    """Raw CFStringRef of an exported TIS constant."""
    return ctypes.c_void_p.in_dll(_carbon, name).value


def _string(ptr: int) -> str:
    return str(objc.objc_object(c_void_p=ptr))


def _source_id(source_ptr: int) -> str:
    value = _carbon.TISGetInputSourceProperty(source_ptr, _constant("kTISPropertyInputSourceID"))
    return _string(value) if value else ""


def _enabled_sources() -> list[tuple[Any, str]]:
    """Enabled, selectable keyboard sources as (TIS object, source id)."""
    # Without this filter palettes and handwriting sources show up too.
    query = Foundation.NSDictionary.dictionaryWithDictionary_({
        _string(_constant("kTISPropertyInputSourceCategory")):
            _string(_constant("kTISCategoryKeyboardInputSource")),
        _string(_constant("kTISPropertyInputSourceIsSelectCapable")): True,
    })
    raw = _carbon.TISCreateInputSourceList(objc.pyobjc_id(query), False)
    if not raw:
        return []
    # The Python proxy retains the array, so the create reference can go.
    sources = objc.objc_object(c_void_p=raw)
    _cf.CFRelease(raw)
    return [(source, _source_id(objc.pyobjc_id(source))) for source in sources]


def current_input_source() -> str:
    """Input source id, e.g. com.apple.keylayout.ABC."""
    source = _carbon.TISCopyCurrentKeyboardInputSource()
    try:
        return _source_id(source)
    finally:
        _cf.CFRelease(source)


def list_input_sources() -> list[str]:
    return [source_id for _, source_id in _enabled_sources()]


def select_input_source(source_id: str) -> None:
    sources = _enabled_sources()
    for source, sid in sources:
        if sid == source_id:
            status = _carbon.TISSelectInputSource(objc.pyobjc_id(source))
            if status != 0:
                raise RuntimeError(f"TISSelectInputSource({source_id}) failed with status {status}")
            return
    available = ", ".join(sid for _, sid in sources)
    raise ValueError(f"input source {source_id!r} is not enabled; available: {available}")
