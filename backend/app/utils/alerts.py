"""System alerts utility for triggering OS-level desktop notifications."""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)


def trigger_system_notification(title: str, message: str) -> None:
    """Send an OS notification without interpolating untrusted text into a shell."""
    if sys.platform == "win32":
        # Market and model output must never be inserted into a PowerShell
        # command string. The encoded fixed script reads title/body from its
        # own process environment instead.
        ps_script = """
        [void] [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms");
        $objNotifyIcon = New-Object System.Windows.Forms.NotifyIcon;
        $objNotifyIcon.Icon = [System.Drawing.SystemIcons]::Information;
        $objNotifyIcon.BalloonTipIcon = "Info";
        $objNotifyIcon.BalloonTipText = [Environment]::GetEnvironmentVariable("APEX_ALERT_MESSAGE");
        $objNotifyIcon.BalloonTipTitle = [Environment]::GetEnvironmentVariable("APEX_ALERT_TITLE");
        $objNotifyIcon.Visible = $True;
        $objNotifyIcon.ShowBalloonTip(5000);
        """
        encoded = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
        child_env = os.environ.copy()
        child_env["APEX_ALERT_TITLE"] = str(title)
        child_env["APEX_ALERT_MESSAGE"] = str(message)
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-EncodedCommand", encoded],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
            )
            logger.info("Sent OS desktop notification via PowerShell toast.")
        except Exception as exc:
            logger.error("Failed to show Windows desktop notification: %s", exc)
    else:
        logger.info("System Alert [%s]: %s (Non-Windows platform)", title, message)
