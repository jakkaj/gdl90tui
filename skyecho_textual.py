#!/usr/bin/env python3
"""
SkyEcho GDL90 Monitor - Textual TUI Version
A responsive terminal UI for monitoring ADS-B traffic from SkyEcho device
"""

import argparse
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from math import radians, cos, sin, asin, sqrt, atan2, degrees, pi

# Add the gdl90 library to the Python path
gdl90_path = Path(__file__).parent / "gdl90"
if gdl90_path.exists():
    sys.path.insert(0, str(gdl90_path))

try:
    from gdl90 import decoder
except ImportError:
    print("Error: gdl90 library not found. Run 'make install' first.")
    sys.exit(1)

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Container, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, Static, DataTable, Label, Button, Placeholder
from textual.reactive import reactive
from textual.timer import Timer
from rich.text import Text
from rich.table import Table
from rich.console import Console
from rich.style import Style
from rich.align import Align
from rich.panel import Panel


# Magma colormap gradient colors (from hot/new to cold/old)
MAGMA_COLORS = [
    "#FCFFA4",  # Bright yellow (newest)
    "#FCA50A",  # Orange
    "#E8533A",  # Red-orange
    "#BC3754",  # Deep red
    "#7C2F7F",  # Purple
    "#3B0F70",  # Dark purple (oldest)
]


def get_magma_color(fraction: float) -> str:
    """Get color from magma gradient (0=new/hot, 1=old/cold)"""
    # Clamp to [0, 1]
    fraction = max(0.0, min(1.0, fraction))

    # Find position in gradient
    index = fraction * (len(MAGMA_COLORS) - 1)
    lower_idx = int(index)
    upper_idx = min(lower_idx + 1, len(MAGMA_COLORS) - 1)

    # Simple nearest neighbor (could interpolate for smoother gradient)
    if index - lower_idx < 0.5:
        return MAGMA_COLORS[lower_idx]
    else:
        return MAGMA_COLORS[upper_idx]


def get_direction_arrow(track: float) -> str:
    """Get arrow character for track heading"""
    # Normalize to 0-360
    track = track % 360

    # 8 directions (45° each)
    if 337.5 <= track or track < 22.5:
        return "↑"   # N
    elif 22.5 <= track < 67.5:
        return "↗"   # NE
    elif 67.5 <= track < 112.5:
        return "→"   # E
    elif 112.5 <= track < 157.5:
        return "↘"   # SE
    elif 157.5 <= track < 202.5:
        return "↓"   # S
    elif 202.5 <= track < 247.5:
        return "↙"   # SW
    elif 247.5 <= track < 292.5:
        return "←"   # W
    else:  # 292.5 <= track < 337.5
        return "↖"   # NW


@dataclass
class Ownship:
    """Ownship data from GDL90"""
    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude: Optional[int] = None
    track: Optional[float] = None
    hspeed: Optional[float] = None
    vspeed: Optional[float] = None
    nic: Optional[int] = None
    nacp: Optional[int] = None
    gps_status: str = "No Fix"
    last_update: Optional[datetime] = None


@dataclass
class Contact:
    """Traffic contact data"""
    address: str
    callsign: str = "Unknown"
    lat: float = 0.0
    lon: float = 0.0
    altitude: int = 0
    track: float = 0.0
    hspeed: float = 0.0
    vspeed: float = 0.0
    distance: float = 0.0
    bearing: float = 0.0
    relative_alt: int = 0  # Relative to ownship
    last_update: datetime = field(default_factory=datetime.now)

    def age_seconds(self) -> float:
        """Get age of contact in seconds"""
        return (datetime.now() - self.last_update).total_seconds()


@dataclass
class DeviceInfo:
    """Device information from heartbeat messages"""
    model: str = "SkyEcho 2"
    firmware_version: str = "Unknown"
    serial_number: str = "Unknown"
    last_heartbeat: Optional[datetime] = None


class GDL90Receiver(threading.Thread):
    """Background thread for receiving and decoding GDL90 messages"""

    def __init__(self, app: 'SkyEchoTextual', bind_ip: str = "", port: int = 4000):
        super().__init__(daemon=True)
        self.app = app
        self.bind_ip = bind_ip or "0.0.0.0"
        self.port = port
        self.running = False
        self.socket = None
        self.decoder = decoder.Decoder()

    def run(self):
        """Main receiver loop"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.bind_ip, self.port))
            self.socket.settimeout(0.5)  # Allow periodic checks for shutdown
            self.running = True

            while self.running:
                try:
                    data, addr = self.socket.recvfrom(4096)

                    # Process with decoder (prints to stdout)
                    self.decoder.addBytes(data)

                    # Signal we got data
                    self.app.call_from_thread(self.app.data_received)

                    # Update demo data for now
                    self.app.call_from_thread(self.app.update_demo_data)

                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        self.app.call_from_thread(self.app.log_error, str(e))

        except Exception as e:
            self.app.call_from_thread(self.app.log_error, f"Socket error: {e}")
        finally:
            if self.socket:
                self.socket.close()

    def stop(self):
        """Stop the receiver thread"""
        self.running = False


class LoadingScreen(ModalScreen):
    """Modal loading screen shown until first packet received"""

    DEFAULT_CSS = """
    LoadingScreen {
        align: center middle;
    }

    #loading_container {
        width: 60;
        height: 11;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #loading_title {
        text-align: center;
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    #loading_text {
        text-align: center;
        color: $text;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="loading_container"):
            yield Label("SkyEcho GDL90 Monitor", id="loading_title")
            yield Label("Waiting for GDL90 data...", id="loading_text")
            yield Label("")
            yield Label("Make sure your device is:")
            yield Label("• Connected to SkyEcho WiFi")
            yield Label("• Broadcasting on port 4000")
            yield Label("")
            yield Label("Press 'q' to quit", classes="dim")


class TrafficTable(Static):
    """Traffic table widget for the left panel"""

    def __init__(self, traffic: Dict[str, Contact], ownship: Ownship, ttl: int = 60):
        super().__init__()
        self.traffic = traffic
        self.ownship = ownship
        self.ttl = ttl

    def render(self) -> Table:
        """Render the traffic table"""
        table = Table(
            show_header=True,
            header_style="bold magenta",
            show_edge=False,
            expand=True,
            padding=(0, 1),
            collapse_padding=True
        )

        # Add columns
        table.add_column("ICAO", style="cyan", justify="left", no_wrap=True)
        table.add_column("Callsign", justify="left", no_wrap=True)
        table.add_column("Trk", justify="center", no_wrap=True, width=3)  # Direction arrow
        table.add_column("Range", justify="right", no_wrap=True)
        table.add_column("Brng", justify="right", no_wrap=True)
        table.add_column("Alt", justify="right", no_wrap=True)
        table.add_column("GS", justify="right", no_wrap=True)
        table.add_column("Age", justify="right", no_wrap=True)

        if not self.traffic:
            # Show empty state
            for _ in range(3):
                table.add_row("—", "—", "—", "—", "—", "—", "—", "—", style="dim")
        else:
            # Sort by distance (closest first)
            sorted_traffic = sorted(self.traffic.values(), key=lambda c: c.distance)

            # Limit to reasonable number of rows
            for contact in sorted_traffic[:20]:
                age = contact.age_seconds()
                if age > self.ttl:
                    continue

                # Calculate age-based color (magma gradient)
                age_fraction = min(age / self.ttl, 1.0)
                age_color = get_magma_color(age_fraction)

                # Format relative altitude
                rel_alt = f"{contact.relative_alt:+d}" if contact.relative_alt != 0 else "="

                # Get direction arrow from track
                direction_arrow = get_direction_arrow(contact.track)

                table.add_row(
                    contact.address[:6],
                    contact.callsign[:10],
                    direction_arrow,
                    f"{contact.distance:.1f}nm",
                    f"{contact.bearing:.0f}°",
                    f"{contact.altitude}ft {rel_alt}",
                    f"{contact.hspeed:.0f}kt",
                    Text(f"{age:.0f}s", style=age_color)
                )

        return table


class RadarView(Static):
    """Radar visualization widget for the right panel"""

    def __init__(self, traffic: Dict[str, Contact], ownship: Ownship, range_nm: float = 10.0):
        super().__init__()
        self.traffic = traffic
        self.ownship = ownship
        self.range_nm = range_nm

    def render(self) -> Panel:
        """Render the radar view"""
        # For now, show a placeholder with basic info
        content = Text()
        content.append("RADAR VISUALIZATION\n\n", style="bold yellow")
        content.append(f"Range: {self.range_nm:.0f} nm\n\n", style="cyan")

        if self.ownship.lat and self.ownship.lon:
            content.append(f"Ownship: {self.ownship.lat:.4f}, {self.ownship.lon:.4f}\n", style="green")
            content.append(f"Altitude: {self.ownship.altitude or 0} ft\n", style="green")
            content.append(f"Track: {self.ownship.track or 0:.0f}°\n\n", style="green")

        # Show traffic count in range
        in_range = [c for c in self.traffic.values() if c.distance <= self.range_nm]
        content.append(f"Traffic in range: {len(in_range)}\n", style="magenta")

        if in_range:
            content.append("\nClosest targets:\n", style="bold")
            for contact in sorted(in_range, key=lambda c: c.distance)[:5]:
                rel_alt = f"{contact.relative_alt:+d}" if contact.relative_alt != 0 else "="
                content.append(
                    f"  • {contact.callsign}: {contact.distance:.1f}nm @ {contact.bearing:.0f}° {rel_alt}ft\n",
                    style="cyan"
                )

        content.append("\n[dim]Full radar display coming soon...[/dim]")

        return Panel(
            Align.center(content, vertical="middle"),
            title="[bold yellow]RADAR[/bold yellow]",
            border_style="yellow"
        )


class StatusBar(Static):
    """Bottom status bar with ownship and device info"""

    def __init__(self, ownship: Ownship, device: DeviceInfo, traffic_count: int = 0,
                 split_view: bool = False, range_nm: float = 10.0,
                 show_view_feedback: bool = False, view_feedback_text: str = "",
                 show_range_feedback: bool = False):
        super().__init__()
        self.ownship = ownship
        self.device = device
        self.traffic_count = traffic_count
        self.split_view = split_view
        self.range_nm = range_nm
        self.show_view_feedback = show_view_feedback
        self.view_feedback_text = view_feedback_text
        self.show_range_feedback = show_range_feedback

    def render(self) -> Text:
        """Render the status bar"""
        text = Text()

        # Device info
        text.append("Device: ", style="bold")
        text.append(f"{self.device.model} ", style="cyan")

        # Version
        text.append("│ Ver: ", style="bold")
        text.append(f"{self.device.firmware_version} ", style="green")

        # Serial number
        text.append("│ SN: ", style="bold")
        text.append(f"{self.device.serial_number} ", style="yellow")

        # Ownship position
        if self.ownship.lat and self.ownship.lon:
            text.append("│ Pos: ", style="bold")
            text.append(f"{self.ownship.lat:.4f}, {self.ownship.lon:.4f} ", style="white")

            # Altitude
            text.append("│ Alt: ", style="bold")
            text.append(f"{self.ownship.altitude or 0}ft ", style="white")

            # Speed
            text.append("│ GS: ", style="bold")
            text.append(f"{self.ownship.hspeed or 0:.0f}kt ", style="white")

        # Traffic count
        text.append("│ Traffic: ", style="bold")
        text.append(f"{self.traffic_count} ", style="magenta")

        # Keyboard hints
        text.append("│ ", style="bold")
        text.append("[S]", style="cyan")
        text.append("plit ", style="dim")
        text.append("[+/-]", style="cyan")
        text.append("Zoom ", style="dim")
        text.append("[Q]", style="cyan")
        text.append("uit", style="dim")

        # Visual feedback
        if self.show_view_feedback:
            text.append(" │ ", style="bold")
            text.append(f"⚡ {self.view_feedback_text}", style="bold yellow")

        if self.show_range_feedback:
            text.append(" │ ", style="bold")
            text.append(f"📡 RANGE: {self.range_nm:.0f} nm", style="bold green")

        return text


class SkyEchoTextual(App):
    """Main Textual application for SkyEcho monitoring"""

    CSS = """
    Screen {
        background: $background;
    }

    #main_container {
        height: 100%;
    }

    #traffic_panel {
        width: 50%;
        min-width: 40;
        border: solid $primary;
        padding: 0 1;
    }

    #radar_panel {
        width: 50%;
        min-width: 40;
        border: solid $warning;
    }

    #status_bar {
        height: 1;
        dock: bottom;
        background: #1c1c1c;
        padding: 0 1;
    }

    .panel_title {
        text-align: center;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("Q", "quit", "Quit", show=False),
        Binding("s", "cycle_view", "Cycle View"),
        Binding("S", "cycle_view", show=False),
        Binding("+", "zoom_in", "Zoom In"),
        Binding("=", "zoom_in", show=False),
        Binding("-", "zoom_out", "Zoom Out"),
        Binding("_", "zoom_out", show=False),
    ]

    # Zoom steps (nautical miles)
    ZOOM_STEPS = [1, 2, 5, 10, 20, 50, 100, 200]

    # View modes
    VIEW_SPLIT = "split"       # Both panels visible
    VIEW_TRAFFIC = "traffic"   # Traffic only (left full)
    VIEW_RADAR = "radar"       # Radar only (right full)

    def __init__(self, bind_ip: str = "", port: int = 4000, ttl: int = 60, radar_range: float = 10.0):
        super().__init__()
        self.bind_ip = bind_ip
        self.port = port
        self.ttl = ttl
        self.radar_range = radar_range

        # Data models
        self.ownship = Ownship()
        self.traffic: Dict[str, Contact] = {}
        self.device = DeviceInfo()

        # State
        self.first_packet_received = False
        self.receiver_thread: Optional[GDL90Receiver] = None
        self.view_mode = self.VIEW_SPLIT  # Start with split view
        self.show_view_feedback = False
        self.view_feedback_text = "SPLIT VIEW"
        self.show_range_feedback = False
        self.feedback_timer: Optional[Timer] = None

        # Widgets
        self.traffic_table: Optional[TrafficTable] = None
        self.radar_view: Optional[RadarView] = None
        self.status_bar: Optional[StatusBar] = None
        self.main_container: Optional[Horizontal] = None
        self.traffic_panel: Optional[Vertical] = None
        self.radar_panel: Optional[Vertical] = None

    def compose(self) -> ComposeResult:
        """Compose the application layout"""
        # Main container
        with Horizontal(id="main_container") as main_container:
            self.main_container = main_container

            # Traffic panel (left) - always created, visibility controlled by CSS
            with Vertical(id="traffic_panel") as traffic_panel:
                self.traffic_panel = traffic_panel
                yield Label("TRAFFIC", classes="panel_title")
                self.traffic_table = TrafficTable(self.traffic, self.ownship, self.ttl)
                yield ScrollableContainer(self.traffic_table)

            # Radar panel (right) - always created, visibility controlled by CSS
            with Vertical(id="radar_panel") as radar_panel:
                self.radar_panel = radar_panel
                yield Label("RADAR", classes="panel_title")
                self.radar_view = RadarView(self.traffic, self.ownship, self.radar_range)
                yield self.radar_view

        # Status bar (bottom)
        self.status_bar = StatusBar(
            self.ownship, self.device, len(self.traffic),
            self.view_mode == self.VIEW_SPLIT, self.radar_range
        )
        yield Container(self.status_bar, id="status_bar")

        # Apply initial view mode
        self.update_view_mode()

    def on_mount(self) -> None:
        """Start receiver thread and show loading screen"""
        # Show loading screen
        if not self.first_packet_received:
            self.push_screen(LoadingScreen())

        # Start receiver thread
        self.receiver_thread = GDL90Receiver(self, self.bind_ip, self.port)
        self.receiver_thread.start()

        # Set up periodic refresh
        self.set_interval(0.5, self.refresh_display)

    def data_received(self) -> None:
        """Called when any data is received (thread-safe)"""
        if not self.first_packet_received:
            self.first_packet_received = True
            if self.screen.is_modal:
                self.pop_screen()

    def update_demo_data(self) -> None:
        """Update with demo data for testing"""
        # Update device
        self.device.last_heartbeat = datetime.now()

        # Update ownship
        if not self.ownship.lat:
            self.ownship.lat = 37.7749
            self.ownship.lon = -122.4194
            self.ownship.altitude = 5000
            self.ownship.track = 45.0
            self.ownship.hspeed = 120.0
            self.ownship.vspeed = 0.0
            self.ownship.gps_status = "3D Fix"
        self.ownship.last_update = datetime.now()

        # Add some demo traffic
        if len(self.traffic) < 5:
            for i in range(5):
                address = f"A{i:05X}"
                if address not in self.traffic:
                    contact = Contact(
                        address=address,
                        callsign=f"TEST{i+1}",
                        lat=self.ownship.lat + (i * 0.01),
                        lon=self.ownship.lon + (i * 0.01),
                        altitude=5000 + (i * 500),
                        hspeed=150 + (i * 20),
                        track=i * 72,
                        distance=2.5 + (i * 2),
                        bearing=i * 72,
                        relative_alt=(i * 500)
                    )
                    self.traffic[address] = contact

    def refresh_display(self) -> None:
        """Refresh the display and prune old contacts"""
        # Prune old contacts
        now = datetime.now()
        to_remove = []
        for address, contact in self.traffic.items():
            if (now - contact.last_update).total_seconds() > self.ttl:
                to_remove.append(address)

        for address in to_remove:
            del self.traffic[address]

        # Update relative altitudes
        if self.ownship.altitude:
            for contact in self.traffic.values():
                contact.relative_alt = contact.altitude - self.ownship.altitude

        # Refresh widgets
        if self.traffic_table:
            self.traffic_table.refresh()
        if self.radar_view:
            self.radar_view.refresh()
        if self.status_bar:
            self.status_bar.traffic_count = len(self.traffic)
            self.status_bar.show_view_feedback = self.show_view_feedback
            self.status_bar.view_feedback_text = self.view_feedback_text
            self.status_bar.show_range_feedback = self.show_range_feedback
            self.status_bar.refresh()

    def action_quit(self) -> None:
        """Quit the application"""
        if self.receiver_thread:
            self.receiver_thread.stop()
        self.exit()

    def update_view_mode(self) -> None:
        """Update panel visibility based on view mode"""
        if not self.traffic_panel or not self.radar_panel:
            return

        if self.view_mode == self.VIEW_SPLIT:
            # Both panels visible, 50/50 split
            self.traffic_panel.styles.display = "block"
            self.traffic_panel.styles.width = "50%"
            self.radar_panel.styles.display = "block"
            self.radar_panel.styles.width = "50%"
        elif self.view_mode == self.VIEW_TRAFFIC:
            # Traffic only, full width
            self.traffic_panel.styles.display = "block"
            self.traffic_panel.styles.width = "100%"
            self.radar_panel.styles.display = "none"
        elif self.view_mode == self.VIEW_RADAR:
            # Radar only, full width
            self.traffic_panel.styles.display = "none"
            self.radar_panel.styles.display = "block"
            self.radar_panel.styles.width = "100%"

    def action_cycle_view(self) -> None:
        """Cycle through view modes: split → traffic → radar → split"""
        # Cycle to next view mode
        if self.view_mode == self.VIEW_SPLIT:
            self.view_mode = self.VIEW_TRAFFIC
            feedback_text = "TRAFFIC ONLY"
        elif self.view_mode == self.VIEW_TRAFFIC:
            self.view_mode = self.VIEW_RADAR
            feedback_text = "RADAR ONLY"
        else:  # VIEW_RADAR
            self.view_mode = self.VIEW_SPLIT
            feedback_text = "SPLIT VIEW"

        # Update the layout
        self.update_view_mode()
        self.show_view_feedback = True
        self.view_feedback_text = feedback_text

        # Clear feedback after 2 seconds
        if self.feedback_timer:
            self.feedback_timer.stop()
        self.feedback_timer = self.set_timer(2.0, self.clear_view_feedback)

    def action_zoom_in(self) -> None:
        """Decrease radar range (zoom in)"""
        try:
            idx = self.ZOOM_STEPS.index(int(self.radar_range))
            if idx > 0:
                self.radar_range = self.ZOOM_STEPS[idx - 1]
        except (ValueError, IndexError):
            self.radar_range = 10  # Default

        self.show_range_feedback = True
        if self.radar_view:
            self.radar_view.range_nm = self.radar_range

        # Clear feedback after 2 seconds
        if self.feedback_timer:
            self.feedback_timer.stop()
        self.feedback_timer = self.set_timer(2.0, self.clear_range_feedback)

    def action_zoom_out(self) -> None:
        """Increase radar range (zoom out)"""
        try:
            idx = self.ZOOM_STEPS.index(int(self.radar_range))
            if idx < len(self.ZOOM_STEPS) - 1:
                self.radar_range = self.ZOOM_STEPS[idx + 1]
        except (ValueError, IndexError):
            self.radar_range = 10  # Default

        self.show_range_feedback = True
        if self.radar_view:
            self.radar_view.range_nm = self.radar_range

        # Clear feedback after 2 seconds
        if self.feedback_timer:
            self.feedback_timer.stop()
        self.feedback_timer = self.set_timer(2.0, self.clear_range_feedback)

    def clear_view_feedback(self) -> None:
        """Clear view mode feedback"""
        self.show_view_feedback = False

    def clear_range_feedback(self) -> None:
        """Clear range change feedback"""
        self.show_range_feedback = False

    def log_error(self, message: str) -> None:
        """Log an error message"""
        self.notify(f"Error: {message}", severity="error")

    def calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in nautical miles"""
        R = 3440.065  # Earth radius in nautical miles

        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1

        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))

        return R * c

    def calculate_bearing(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate bearing from point 1 to point 2"""
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
        dlon = lon2 - lon1

        x = sin(dlon) * cos(lat2)
        y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)

        bearing = atan2(x, y)
        bearing = degrees(bearing)
        bearing = (bearing + 360) % 360

        return bearing


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='SkyEcho GDL90 Monitor (Textual)')
    parser.add_argument('--bind', type=str, default="",
                      help='IP address to bind to (default: all interfaces)')
    parser.add_argument('--port', type=int, default=4000,
                      help='UDP port to listen on (default: 4000)')
    parser.add_argument('--ttl', type=int, default=60,
                      help='Time-to-live for traffic contacts in seconds (default: 60)')
    parser.add_argument('--radar-range', type=float, default=10.0,
                      help='Initial radar range in nm (default: 10)')

    args = parser.parse_args()

    app = SkyEchoTextual(
        bind_ip=args.bind,
        port=args.port,
        ttl=args.ttl,
        radar_range=args.radar_range
    )
    app.run()


if __name__ == "__main__":
    main()