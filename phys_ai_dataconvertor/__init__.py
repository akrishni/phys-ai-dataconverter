"""phys_ai_dataconvertor: convert robot recordings (ROS 2 MCAP) into training datasets (LeRobot v3).

Copyright (c) 2026 Anand Krishniyer. MIT License."""

__version__ = "0.1.0"

from phys_ai_dataconvertor.model import ConversionError  # noqa: E402,F401
from phys_ai_dataconvertor.convert import ConversionReport, ConvertOptions, convert  # noqa: E402,F401
