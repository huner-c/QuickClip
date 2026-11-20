"""QuickClip Qt user interface widgets and windows."""

from quickclip.ui.range_slider import RangeSlider
from quickclip.ui.region_selector import RegionSelectorOverlay, logical_rect_to_capture_region
from quickclip.ui.standby_bar import StandbyWindow, qt_key_event_to_hotkey
from quickclip.ui.trimmer import TrimmerWindow

__all__ = [
    "RangeSlider",
    "RegionSelectorOverlay",
    "StandbyWindow",
    "TrimmerWindow",
    "logical_rect_to_capture_region",
    "qt_key_event_to_hotkey",
]
