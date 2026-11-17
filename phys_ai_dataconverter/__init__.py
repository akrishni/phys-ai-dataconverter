"""phys_ai_dataconverter: convert robot recordings (ROS 2 MCAP) into training datasets (LeRobot v3).

Copyright (c) 2026 Anand Krishniyer. MIT License."""

__version__ = "0.2.0"

from phys_ai_dataconverter.model import ConversionError  # noqa: E402,F401
from phys_ai_dataconverter.convert import ConversionReport, ConvertOptions, convert  # noqa: E402,F401
