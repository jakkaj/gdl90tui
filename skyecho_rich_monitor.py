#!/usr/bin/env python3
"""
SkyEcho Rich TUI Monitor
A beautiful terminal UI for monitoring ADS-B traffic from SkyEcho devices
"""

import socket
import sys
import argparse
import time
import threading
import select
from pathlib import Path
from datetime import datetime
from math import radians, cos, sin, asin, sqrt, atan2, degrees
from typing import Dict, Optional, Tuple

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich.align import Align

# Platform-specific imports for keyboard input
try:
    import termios
    import tty
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False

# Add the gdl90 library to the Python path
gdl90_path = Path(__file__).parent / "gdl90"
if gdl90_path.exists():
    sys.path.insert(0, str(gdl90_path))

from gdl90 import decoder


class MagmaGradient:
    """Magma-like color gradient without matplotlib dependency"""

    # Hand-picked colors that approximate the magma colormap
    # From hot (new) to cold (old): yellow -> orange -> red -> purple -> dark
    COLORS = [
        "#FCFFA4",  # 0% - Bright yellow (hot/new)
        "#FCA50A",  # 20% - Orange
        "#E8533A",  # 40% - Red-orange
        "#BC3754",  # 60% - Deep red
        "#7C2F7F",  # 80% - Purple
        "#3B0F70",  # 100% - Dark purple (cold/old)
    ]

    @classmethod
    def get_color(cls, normalized_value: float) -> str:
        """
        Get a color from the magma gradient

        Args:
            normalized_value: Value between 0.0 (new/hot) and 1.0 (old/cold)

        Returns:
            Hex color string
        """
        normalized_value = max(0.0, min(1.0, normalized_value))

        # Find the two colors to interpolate between
        index = normalized_value * (len(cls.COLORS) - 1)
        lower_idx = int(index)
        upper_idx = min(lower_idx + 1, len(cls.COLORS) - 1)

        # If exact match, return that color
        if lower_idx == upper_idx:
            return cls.COLORS[lower_idx]

        # Simple approach: just return the nearest color
        # (True interpolation would be more complex)
        if index - lower_idx < 0.5:
            return cls.COLORS[lower_idx]
        else:
            return cls.COLORS[upper_idx]


class GeographicCalculator:
    """Calculate distances and bearings between geographic coordinates"""

    @staticmethod
    def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate the great circle distance between two points on Earth

        Returns:
            Distance in nautical miles
        """
        # Convert to radians
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))

        # Earth radius in nautical miles
        r_nm = 3440.065

        return c * r_nm

    @staticmethod
    def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate the bearing from point 1 to point 2

        Returns:
            Bearing in degrees (0-360)
        """
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

        dlon = lon2 - lon1

        x = sin(dlon) * cos(lat2)
        y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)

        initial_bearing = atan2(x, y)

        # Convert to degrees and normalize to 0-360
        bearing = (degrees(initial_bearing) + 360) % 360

        return bearing


class TrafficTarget:
    """Represents a single traffic target"""

    def __init__(self, address: int, msg):
        self.address = address
        self.icao = f"{address:06X}"
        self.callsign = msg.CallSign.strip() if hasattr(msg, 'CallSign') else "-"
        self.latitude = msg.Latitude
        self.longitude = msg.Longitude
        self.altitude = msg.Altitude
        self.ground_speed = msg.HVelocity
        self.vertical_speed = msg.VVelocity
        self.track = msg.TrackHeading
        self.last_seen = time.time()
        self.first_seen = time.time()

    def update(self, msg):
        """Update target with new message data"""
        if hasattr(msg, 'CallSign'):
            self.callsign = msg.CallSign.strip() or self.callsign
        self.latitude = msg.Latitude
        self.longitude = msg.Longitude
        self.altitude = msg.Altitude
        self.ground_speed = msg.HVelocity
        self.vertical_speed = msg.VVelocity
        self.track = msg.TrackHeading
        self.last_seen = time.time()

    def age_seconds(self) -> float:
        """Get age in seconds since last update"""
        return time.time() - self.last_seen


class SkyEchoMonitor:
    """Main monitoring application"""

    def __init__(self, bind_ip: str, port: int, ttl: int, radar_range: float, split_view: bool):
        self.bind_ip = bind_ip
        self.port = port
        self.ttl = ttl
        self.radar_range = radar_range
        self.split_view = split_view

        self.console = Console()
        self.decoder = decoder.Decoder()
        self.decoder.format = 'none'  # We'll handle output ourselves

        # State
        self.ownship: Optional[Dict] = None
        self.traffic: Dict[int, TrafficTarget] = {}
        self.last_heartbeat: Optional[datetime] = None
        self.gps_status = "No GPS"
        self.device_info = {
            "model": "SkyEcho",
            "version": "Unknown",
            "serial": "Unknown"
        }
        self.packets_received = 0
        self.first_packet_received = False

        # Keyboard input handling
        self.keyboard_lock = threading.Lock()
        self.keyboard_running = False
        self.keyboard_thread: Optional[threading.Thread] = None
        self.split_toggled = False  # Flag to show visual feedback
        self.range_changed = False  # Flag to show range change feedback
        self.range_zoom_steps = [1, 2, 5, 10, 20, 50, 100, 200]  # Available zoom levels in nm
        self.needs_refresh = True  # Flag for resize handling

        # Socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.1)  # Non-blocking with timeout
        self.sock.bind((self.bind_ip, self.port))

        # Override decoder's _decodeMessage to capture messages
        self.original_decode = self.decoder._decodeMessage
        self.decoder._decodeMessage = self._capture_message

    def _capture_message(self, escaped_message):
        """Intercept decoded messages"""
        # Call original decode logic
        rawMsg = self.decoder._unescape(escaped_message)
        if len(rawMsg) < 5:
            return False

        msg = rawMsg[:-2]
        crc = rawMsg[-2:]

        from gdl90.fcs import crcCheck
        if not crcCheck(msg, crc):
            return False

        from gdl90 import messages
        m = messages.messageToObject(msg)
        if not m:
            return False

        # Process the message
        self._process_message(m)
        return True

    def _process_message(self, msg):
        """Process a decoded GDL90 message"""
        if not self.first_packet_received:
            self.first_packet_received = True

        msg_type = msg.MsgType

        if msg_type == 'Heartbeat':
            self.last_heartbeat = datetime.now()
            # Could extract GPS status from StatusByte1

        elif msg_type == 'OwnshipReport':
            self.ownship = {
                'latitude': msg.Latitude,
                'longitude': msg.Longitude,
                'altitude': msg.Altitude,
                'ground_speed': msg.HVelocity,
                'track': msg.TrackHeading,
                'vertical_speed': 0,  # Not in MSG10
                'updated': datetime.now()
            }

        elif msg_type == 'OwnshipGeometricAltitude':
            if self.ownship:
                self.ownship['altitude'] = msg.Altitude
                self.ownship['vertical_speed'] = 0  # Could track changes

        elif msg_type == 'TrafficReport':
            address = msg.Address
            if address in self.traffic:
                self.traffic[address].update(msg)
            else:
                self.traffic[address] = TrafficTarget(address, msg)

        elif msg_type == 'GpsTime':
            self.gps_status = f"GPS OK - {msg.Hour:02d}:{msg.Minute:02d} UTC"

    def cleanup_stale_traffic(self):
        """Remove traffic targets older than TTL"""
        current_time = time.time()
        stale_addresses = [
            addr for addr, target in self.traffic.items()
            if (current_time - target.last_seen) > self.ttl
        ]
        for addr in stale_addresses:
            del self.traffic[addr]

    def _keyboard_listener(self):
        """Listen for keyboard input in a background thread"""
        if not HAS_TERMIOS:
            return  # Only works on Unix systems

        # Save terminal settings
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        try:
            # Set terminal to raw mode
            tty.setraw(fd)

            while self.keyboard_running:
                # Check if input is available (non-blocking)
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    char = sys.stdin.read(1)

                    if char == 's' or char == 'S':
                        # Toggle split view
                        with self.keyboard_lock:
                            self.split_view = not self.split_view
                            self.split_toggled = True

                    elif char == '+' or char == '=':
                        # Zoom in (decrease range)
                        with self.keyboard_lock:
                            self._zoom_in()

                    elif char == '-' or char == '_':
                        # Zoom out (increase range)
                        with self.keyboard_lock:
                            self._zoom_out()

                    elif char == 'q' or char == 'Q' or char == '\x03':  # \x03 is Ctrl-C
                        # Quit signal
                        self.keyboard_running = False
                        break

        finally:
            # Restore terminal settings
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _zoom_in(self):
        """Decrease radar range (zoom in)"""
        # Find current range in zoom steps or closest lower value
        current_idx = 0
        for i, step in enumerate(self.range_zoom_steps):
            if step >= self.radar_range:
                current_idx = i
                break

        # Move to previous step (smaller range)
        if current_idx > 0:
            self.radar_range = self.range_zoom_steps[current_idx - 1]
            self.range_changed = True

    def _zoom_out(self):
        """Increase radar range (zoom out)"""
        # Find current range in zoom steps or closest higher value
        current_idx = len(self.range_zoom_steps) - 1
        for i, step in enumerate(self.range_zoom_steps):
            if step > self.radar_range:
                current_idx = i
                break

        # Move to next step (larger range)
        if current_idx < len(self.range_zoom_steps) - 1:
            self.radar_range = self.range_zoom_steps[current_idx]
            self.range_changed = True
        elif self.radar_range < self.range_zoom_steps[-1]:
            self.radar_range = self.range_zoom_steps[-1]
            self.range_changed = True

    def create_ownship_panel(self) -> Panel:
        """Create the ownship information panel"""
        if not self.ownship:
            content = Align.center(Text("Waiting for ownship data...", style="dim yellow"), vertical="middle")
        else:
            lines = [
                f"Position:  {self.ownship['latitude']:>11.6f}°  {self.ownship['longitude']:>11.6f}°",
                f"Altitude:  {self.ownship['altitude']:>6} ft",
                f"Speed:     {self.ownship['ground_speed']:>6} kts",
                f"Track:     {self.ownship['track']:>6.1f}°",
                f"V/S:       {self.ownship['vertical_speed']:>6} fpm",
                f"GPS:       {self.gps_status}",
            ]
            content = Text("\n".join(lines))

        return Panel(
            content,
            title="[bold cyan]OWNSHIP[/bold cyan]",
            border_style="cyan",
            padding=(0, 2)  # No vertical padding to save space
        )

    def create_traffic_table(self, max_rows: int = 20) -> Table:
        """Create the traffic table"""
        table = Table(
            show_header=True,
            header_style="bold magenta",
            expand=True,  # Fill available panel width as recommended
            show_edge=False,
            padding=(0, 1),
            collapse_padding=True
        )

        table.add_column("ICAO", style="cyan", no_wrap=True)
        table.add_column("Callsign", no_wrap=True)
        table.add_column("Range", justify="right", no_wrap=True)
        table.add_column("Brng", justify="right", no_wrap=True)
        table.add_column("Alt", justify="right", no_wrap=True)
        table.add_column("GS", justify="right", no_wrap=True)
        table.add_column("Age", justify="right", no_wrap=True)

        if not self.ownship or not self.traffic:
            # Show empty state
            for _ in range(3):
                table.add_row("—", "—", "—", "—", "—", "—", "—", style="dim")
            return table

        # Calculate range/bearing for each target and sort by range
        targets_with_range = []
        for target in self.traffic.values():
            range_nm = GeographicCalculator.haversine_distance(
                self.ownship['latitude'], self.ownship['longitude'],
                target.latitude, target.longitude
            )
            bearing = GeographicCalculator.calculate_bearing(
                self.ownship['latitude'], self.ownship['longitude'],
                target.latitude, target.longitude
            )
            targets_with_range.append((range_nm, bearing, target))

        # Sort by range (closest first) and limit to max_rows
        targets_with_range.sort(key=lambda x: x[0])
        targets_with_range = targets_with_range[:max_rows]

        # Add rows with magma color for age
        for range_nm, bearing, target in targets_with_range:
            age = target.age_seconds()

            # Normalize age to 0-1 for color gradient
            normalized_age = min(age / self.ttl, 1.0)
            color = MagmaGradient.get_color(normalized_age)

            age_str = f"{int(age)}s"

            table.add_row(
                target.icao,
                target.callsign[:10],
                f"{range_nm:.1f} nm",
                f"{bearing:.0f}°",
                f"{target.altitude} ft",
                f"{target.ground_speed} kt",
                f"[{color}]{age_str}[/{color}]"
            )

        return table

    def create_radar_placeholder(self) -> Panel:
        """Create a placeholder for the radar visualization"""
        content = Text.assemble(
            ("RADAR VISUALIZATION\n\n", "bold yellow"),
            (f"Range: {self.radar_range:.0f} nm\n\n", "cyan"),
            ("Coming soon...\n\n", "dim"),
            ("This area will display a\n", "dim"),
            ("polar radar plot showing\n", "dim"),
            ("traffic positions relative\n", "dim"),
            ("to ownship.\n", "dim"),
        )

        return Panel(
            Align.center(content, vertical="middle"),
            title="[bold yellow]RADAR[/bold yellow]",
            border_style="yellow"
        )

    def create_device_bar(self) -> Panel:
        """Create the device information bar"""
        visible_count = len(self.traffic)

        # Build status message
        status_parts = [
            ("Device: ", "bold"),
            (f"{self.device_info['model']} ", "cyan"),
            ("│ Ver: ", "bold"),
            (f"{self.device_info['version']} ", "green"),
            ("│ SN: ", "bold"),
            (f"{self.device_info['serial']} ", "yellow"),
            ("│ Traffic: ", "bold"),
            (f"{visible_count} ", "magenta"),
        ]

        # Add keyboard hints
        if HAS_TERMIOS:
            status_parts.extend([
                ("│ ", "bold"),
                ("[S]", "cyan"),
                ("plit ", "dim"),
                ("[+/-]", "cyan"),
                ("Zoom ", "dim"),
                ("[Q]", "cyan"),
                ("uit", "dim"),
            ])

        # Add visual feedback if split was just toggled
        if self.split_toggled:
            mode = "SPLIT" if self.split_view else "FULL"
            status_parts.extend([
                (" │ ", "bold"),
                (f"⚡ {mode} VIEW", "bold yellow"),
            ])

        # Add visual feedback if range was changed
        if self.range_changed:
            # Determine range display format
            if self.radar_range >= 1:
                range_str = f"{self.radar_range:.0f} nm"
            else:
                range_str = f"{self.radar_range:.1f} nm"

            status_parts.extend([
                (" │ ", "bold"),
                (f"📡 RANGE: {range_str}", "bold green"),
            ])

        content = Text.assemble(*status_parts)
        return Panel(content, style="on grey11")

    def create_loading_screen(self) -> Layout:
        """Create the loading screen shown before first packet"""
        layout = Layout()

        content = Text.assemble(
            ("SkyEcho Rich Monitor\n\n", "bold cyan"),
            (f"Listening on {self.bind_ip}:{self.port}\n\n", "yellow"),
            ("Waiting for GDL90 data...\n", "dim"),
        )

        layout.update(
            Panel(
                Align.center(content, vertical="middle"),
                title="[bold]Loading[/bold]",
                border_style="blue"
            )
        )

        return layout

    def create_layout(self) -> Layout:
        """Create the main TUI layout"""
        layout = Layout()

        # Use fixed sizes for header/footer, ratio for body
        layout.split(
            Layout(name="header", size=10),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3)
        )

        # Body: Split or full traffic table
        if self.split_view:
            layout["body"].split_row(
                Layout(name="traffic", ratio=1, minimum_size=30),
                Layout(name="radar", ratio=1, minimum_size=30)
            )

        return layout

    def render_layout(self, layout: Layout) -> Layout:
        """Render all content into the layout - call this inside Live context"""
        # BEST PRACTICE: Use fixed conservative row count
        # Don't dynamically read console.size during render - let Rich handle layout
        available_rows = 15  # Conservative default that fits most terminals

        # Update all panels
        layout["header"].update(self.create_ownship_panel())

        if self.split_view:
            layout["body"]["traffic"].update(Panel(
                self.create_traffic_table(max_rows=available_rows),
                border_style="magenta",
                padding=(0, 1)
            ))
            layout["body"]["radar"].update(self.create_radar_placeholder())
        else:
            layout["body"].update(Panel(
                self.create_traffic_table(max_rows=available_rows),
                border_style="magenta",
                padding=(0, 1)
            ))

        layout["footer"].update(self.create_device_bar())

        return layout

    def _on_resize(self, *_):
        """Handle terminal resize signal"""
        self.needs_refresh = True

    def run(self):
        """Main run loop"""
        # Set up resize handler
        if HAS_TERMIOS:
            import signal
            signal.signal(signal.SIGWINCH, self._on_resize)

        # Start keyboard listener thread if available
        if HAS_TERMIOS:
            self.keyboard_running = True
            self.keyboard_thread = threading.Thread(target=self._keyboard_listener, daemon=True)
            self.keyboard_thread.start()

        # Create layout structure once
        layout = self.create_layout()

        with Live(
            self.create_loading_screen(),
            console=self.console,
            refresh_per_second=10,
            screen=True,
            auto_refresh=False  # Manual refresh for better control
        ) as live:
            try:
                while True:
                    # Check if keyboard thread requested quit
                    if not self.keyboard_running:
                        break

                    # Receive UDP packets
                    try:
                        data, addr = self.sock.recvfrom(4096)
                        self.packets_received += 1
                        self.decoder.addBytes(data)
                        self.needs_refresh = True
                    except socket.timeout:
                        pass

                    # Cleanup stale traffic
                    self.cleanup_stale_traffic()

                    # Check for split toggle and reset feedback flag after a moment
                    with self.keyboard_lock:
                        if self.split_toggled:
                            # Show feedback for a short time then clear it
                            threading.Timer(2.0, self._clear_toggle_feedback).start()
                            self.needs_refresh = True
                        if self.range_changed:
                            # Show range feedback for a short time then clear it
                            threading.Timer(2.0, self._clear_range_feedback).start()
                            self.needs_refresh = True

                    # Update display when needed
                    if self.needs_refresh:
                        if self.first_packet_received:
                            # Recreate layout if split view changed
                            if hasattr(self, '_last_split_view') and self._last_split_view != self.split_view:
                                layout = self.create_layout()
                            self._last_split_view = self.split_view

                            live.update(self.render_layout(layout), refresh=True)
                        else:
                            live.update(self.create_loading_screen(), refresh=True)
                        self.needs_refresh = False

                    # Small sleep to avoid busy loop
                    time.sleep(0.05)

            except KeyboardInterrupt:
                # Handle Ctrl-C gracefully
                self.keyboard_running = False

            finally:
                # Clean up
                self.keyboard_running = False
                if self.keyboard_thread and self.keyboard_thread.is_alive():
                    self.keyboard_thread.join(timeout=1.0)
                self.sock.close()
                # Clear the screen and show exit message
                if HAS_TERMIOS:
                    self.console.clear()
                self.console.print("\n[yellow]Monitor stopped[/yellow]")

    def _clear_toggle_feedback(self):
        """Clear the split toggle visual feedback flag"""
        with self.keyboard_lock:
            self.split_toggled = False

    def _clear_range_feedback(self):
        """Clear the range change visual feedback flag"""
        with self.keyboard_lock:
            self.range_changed = False


def main():
    parser = argparse.ArgumentParser(
        description="SkyEcho Rich TUI Monitor - Beautiful ADS-B traffic display"
    )
    parser.add_argument(
        "--port", type=int, default=4000,
        help="UDP port to listen on (default: 4000)"
    )
    parser.add_argument(
        "--bind", type=str, default="",
        help="Bind IP address (default: all interfaces)"
    )
    parser.add_argument(
        "--ttl", type=int, default=60,
        help="Seconds before stale targets are removed (default: 60)"
    )
    parser.add_argument(
        "--radar-range", type=float, default=10.0,
        help="Radar display range in nautical miles (default: 10.0)"
    )
    parser.add_argument(
        "--split", action="store_true",
        help="Enable split view with radar placeholder (default: traffic only)"
    )

    args = parser.parse_args()

    monitor = SkyEchoMonitor(
        bind_ip=args.bind,
        port=args.port,
        ttl=args.ttl,
        radar_range=args.radar_range,
        split_view=args.split
    )

    monitor.run()


if __name__ == "__main__":
    main()
