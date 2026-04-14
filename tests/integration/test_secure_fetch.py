# tests/integration/test_secure_fetch.py
import os
import threading
import hashlib
import pytest
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pytest_mes_core.provisioning.secure_fetch import SecureAssetFetcher, ImageVerificationError

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

@pytest.fixture(scope="module")
def local_server(tmp_path_factory):
    """Spawns a local HTTP server to host dummy firmware."""
    serve_dir = tmp_path_factory.mktemp("server_root")
    payload = os.urandom(1024 * 1024) # 1 MB of entropy
    fw_path = serve_dir / "firmware.bin"
    fw_path.write_bytes(payload)

    true_hash = hashlib.sha256(payload).hexdigest()

    server = HTTPServer(('127.0.0.1', 0), lambda *args, **kw: QuietHandler(*args, directory=str(serve_dir), **kw))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{server.server_port}/firmware.bin", true_hash
    server.shutdown()

def test_secure_fetch_atomic_failure_cleanup(local_server, tmp_path):
    url, true_hash = local_server
    dest_path = tmp_path / "download.bin"

    # Simulate a noisy factory Wi-Fi corrupting the hash
    bad_hash = "0000000000000000000000000000000000000000000000000000000000000000"

    with pytest.raises(ImageVerificationError, match="SHA256 mismatch"):
        SecureAssetFetcher.fetch_and_verify(url, bad_hash, dest_path)

    # MATHEMATICAL PROOF: Atomic cleanup executed!
    # Neither the final file nor the partial artifact exists.
    assert not dest_path.exists()
    assert not (tmp_path / "download.part").exists()

def test_secure_fetch_success_and_cache_hit(local_server, tmp_path):
    url, true_hash = local_server
    dest_path = tmp_path / "good.bin"

    # 1. Download it fresh
    SecureAssetFetcher.fetch_and_verify(url, true_hash, dest_path)
    assert dest_path.exists()

    # 2. Idempotency Check: Modify the URL to a broken endpoint.
    # If the cache hit works, it won't even attempt to hit the broken URL!
    SecureAssetFetcher.fetch_and_verify("http://127.0.0.1:9999/broken", true_hash, dest_path)
