"""Contract with the uploaded center, on ephemeral localhost sockets only."""
import importlib.util
from pathlib import Path
import socket
from concurrent.futures import ThreadPoolExecutor


def test_collector_accept_sets_restart_timeout(monkeypatch):
    root = Path(__file__).resolve().parents[2]/"exhibition-server"
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location("ripples_test_collector", root/"collector_client.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    link = client._Link()
    with socket.create_server(("127.0.0.1", 0)) as listener, ThreadPoolExecutor(1) as pool:
        link._srv = listener
        for _ in range(2):
            pending = pool.submit(link._accept)
            with socket.create_connection(listener.getsockname()) as sender:
                with pending.result(timeout=2) as accepted:
                    assert accepted.gettimeout() == 5
                    assert accepted.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
                    # Shortened in the test; production remains five seconds.
                    accepted.settimeout(.02)
                    try:
                        client.proto.recv(accepted)
                    except socket.timeout:
                        pass
                    else:
                        raise AssertionError("silent collector must time out")
