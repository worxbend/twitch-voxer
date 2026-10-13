"""Offline fresh-volume/recreate/stop acceptance for the built image.

Usage: python tests/container_smoke.py twitch-voxer:local
Creates only uniquely named test resources and always removes them. No Twitch,
model downloads, published ports, or real operator volumes are involved.
"""

import json
import subprocess
import sys
import time
import uuid


def command(*args, timeout=30):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(f"Docker command failed: {result.stderr.strip()}")
    return result.stdout.strip()


def smoke(image):
    prefix = "voxer-smoke-" + uuid.uuid4().hex[:12]
    state, cache = prefix + "-state", prefix + "-cache"
    container = prefix
    env = {
        "VOXER_SETTINGS_FILE": "/data/settings.json",
        "VOXER_TOKEN_FILE": "/data/tokens.json",
        "VOXER_MESSAGES_PATH": "/data/messages.json",
        "VOXER_AUDIO_DIR": "/tmp/audio",
        "VOXER_SERVER_HOST": "0.0.0.0",
        "VOXER_OAUTH_HOST": "0.0.0.0",
    }

    def boot():
        started = time.monotonic()
        args = [
            "run",
            "-d",
            "--name",
            container,
            "--network",
            "none",
            "--read-only",
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=256m",
            "-v",
            state + ":/data",
            "-v",
            cache + ":/home/voxer/.cache",
        ]
        for name, value in env.items():
            args.extend(["-e", f"{name}={value}"])
        command(*args, image)
        for _ in range(50):
            try:
                stage = command(
                    "exec",
                    container,
                    "python",
                    "-m",
                    "voxer.control",
                    "stage",
                    timeout=10,
                )
            except RuntimeError:
                time.sleep(0.1)
                continue
            assert stage == "setup_required", stage
            return time.monotonic() - started
        raise AssertionError("Control failed to start")

    def python(source):
        return command("exec", container, "python", "-c", source)

    def stop():
        started = time.monotonic()
        command("stop", "--time", "10", container, timeout=15)
        assert command("inspect", "-f", "{{.State.ExitCode}}", container) == "0"
        duration = time.monotonic() - started
        command("rm", container)
        return duration

    for volume in (state, cache):
        command("volume", "create", volume)
    try:
        first = boot()
        python(
            "import os,sys,urllib.request,importlib.util; from pathlib import Path; assert importlib.util.find_spec('playwright') is None; assert not Path('/usr/local/bin/uv').exists(); assert os.getuid()==1000; assert 'supertonic' not in sys.modules; assert Path('/data/settings.json').stat().st_mode & 0o777 == 0o600; assert Path('/data/messages.json').read_text()=='[]\\n'; assert not list(Path('/app').rglob('wow.mp3')); assert Path('/app/.venv/bin/voxer-fetch-emotes').is_file(); assert len(list(Path('/app/voxer/sounds').glob('*.mp3')))==17; Path('/home/voxer/.cache/sentinel').write_text('cached'); assert urllib.request.urlopen('http://localhost:4343/healthz').status==200"
        )
        detail = json.loads(
            command("exec", container, "python", "-m", "voxer.control", "status")
        )
        assert not detail["ready"] and "token" not in json.dumps(detail)
        python(
            "from voxer.token_store import write_json_atomic; write_json_atomic('/data/tokens.json', {'123':{'token':'synthetic','refresh':'synthetic'}}); write_json_atomic('/data/voices.json', {'viewer':'M1'}); write_json_atomic('/data/messages.json', {'messages':[{'text':'synthetic reminder', 'frequency_per_hour':1}]})"
        )
        snapshot = python(
            "import hashlib; from pathlib import Path; print(hashlib.sha256(b''.join(Path('/data',n).read_bytes() for n in ('settings.json','tokens.json','voices.json','messages.json'))).hexdigest())"
        )
        stopped = stop()
        second = boot()
        after = python(
            "import hashlib; from pathlib import Path; assert Path('/home/voxer/.cache/sentinel').read_text()=='cached'; print(hashlib.sha256(b''.join(Path('/data',n).read_bytes() for n in ('settings.json','tokens.json','voices.json','messages.json'))).hexdigest())"
        )
        assert after == snapshot, "Private state changed on recreation"
        stop()
        print(
            json.dumps(
                {
                    "first_control_seconds": round(first, 3),
                    "recreated_control_seconds": round(second, 3),
                    "setup_stop_seconds": round(stopped, 3),
                    "result": "passed",
                }
            )
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, timeout=15
        )
        for volume in (state, cache):
            subprocess.run(
                ["docker", "volume", "rm", volume], capture_output=True, timeout=15
            )


if __name__ == "__main__":
    smoke(sys.argv[1] if len(sys.argv) == 2 else "twitch-voxer:local")
