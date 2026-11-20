import sys
from serial.tools import list_ports


def confirm_board(port, firmware):
    """Inspect enumerated ports only. The first byte is sent after explicit confirmation."""
    devices = {item.device.casefold(): item for item in list_ports.comports()}
    board = devices.get(port.casefold())
    if board is None:
        raise SystemExit(f"Selected port {port} is absent. Available: {', '.join(devices) or 'none'}. USB unopened.")
    print(f"USB port: {board.device}  |  {board.description}  |  {board.hwid}")
    print(f"Selected board protocol: {firmware}. Check the physical LED controller and its firmware.")
    if not sys.stdin or not sys.stdin.isatty():
        raise SystemExit("Interactive board confirmation is required. USB unopened.")
    try:
        confirmed = input(f"Type {board.device} to open this board and send frames: ").strip()
    except (KeyboardInterrupt, EOFError):
        confirmed = ""
    if confirmed.casefold() != board.device.casefold():
        raise SystemExit("Cancelled. USB unopened.")
