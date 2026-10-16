"""
ScriptFlow Mailer Module.
Re-exports email_service functions for direct access from app.mailer.
"""

from .integrations.email_service import (
    ConnectionResult,
    SendResult,
    get_email_config,
    save_email_config,
    get_smtp_client,
    test_connection,
    send_email,
    send_bulk_emails,
    send_access_codes_to_agents,
    send_recording_reminder,
    send_supervisor_notification,
)

__all__ = [
    "ConnectionResult",
    "SendResult",
    "get_email_config",
    "save_email_config",
    "get_smtp_client",
    "test_connection",
    "send_email",
    "send_bulk_emails",
    "send_access_codes_to_agents",
    "send_recording_reminder",
    "send_supervisor_notification",
]
