"""Read-only, traversal-safe audio example assets. No device or sensor writes."""
from pathlib import Path
from urllib.parse import unquote


def asset_path(page_dir, url_path):
    root=(Path(page_dir)/"static"/"tower-audio").resolve()
    prefix="/static/tower-audio/"
    if not url_path.startswith(prefix):
        return None
    candidate=(root/unquote(url_path[len(prefix):])).resolve()
    if not candidate.is_relative_to(root) or candidate.suffix not in (".js",".json",".wav") or not candidate.is_file():
        return None
    return candidate


def serve(handler, url_path):
    path=asset_path(handler.page_dir,url_path)
    if path is None:
        handler.send_error(404)
        return
    data=path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type",{".js":"application/javascript",".json":"application/json",".wav":"audio/wav"}[path.suffix])
    handler.send_header("Content-Length",str(len(data)))
    handler.send_header("Cache-Control","no-cache")
    handler.end_headers()
    handler.wfile.write(data)
