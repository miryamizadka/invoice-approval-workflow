"""Intake Service: system entry point - accepts submissions, forwards to Decision."""

from services.intake.app import create_app
from services.intake.service import IntakeService, build_intake_service

__all__ = ["IntakeService", "build_intake_service", "create_app"]
