#!/usr/bin/env bash
# Host-only Docker launcher. Secrets stay in the container's private volume.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPOSE=(docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml")

fail() { printf '%s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null || fail 'Install Docker with the Compose plugin first.'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required.'
docker info >/dev/null 2>&1 || fail 'Docker is not available. Start Docker and check your permissions.'

control() { "${COMPOSE[@]}" exec -T voxer python -m voxer.control "$1"; }
wait_control() {
    local attempt deadline=$((SECONDS + 30))
    for ((attempt=0; attempt<30 && SECONDS<deadline; attempt++)); do
        if control stage 2>/dev/null; then return 0; fi
        sleep 1
    done
    fail 'Control did not start within 30 seconds. Check docker compose logs and ports 4343/8080.'
}
open_setup() {
    local url
    url=$(control setup-url) || fail 'Cannot obtain a private setup link.'
    if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v xdg-open >/dev/null; then
        xdg-open "$url" >/dev/null 2>&1 && return 0
    elif [[ "$(uname -s)" == Darwin ]] && command -v open >/dev/null; then
        open "$url" >/dev/null 2>&1 && return 0
    fi
    printf 'Open this private, single-use link on the Docker host:\n%s\n' "$url"
}

selected_image() {
    # Resolve Compose's .env interpolation as well as exported overrides.
    "${COMPOSE[@]}" config --images
}
ensure_image() {
    local image
    image=$(selected_image) || fail 'Cannot resolve the configured image.'
    [[ -n "$image" && "$image" != *$'\n'* ]] || fail 'Expected exactly one configured Voxer image.'
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        if [[ "$image" == twitch-voxer:local ]]; then
            "${COMPOSE[@]}" build voxer
        else
            "${COMPOSE[@]}" pull voxer
        fi
    fi
}

case "${1:-start}" in
    start)
        # Development default: build once, then reuse. Release-image overrides
        # are acquired only when absent; routine start never builds or pulls.
        running=$("${COMPOSE[@]}" ps --status running -q voxer)
        ensure_image
        "${COMPOSE[@]}" up -d --no-build --pull never voxer
        stage=$(wait_control)
        if [[ -n "$running" ]]; then
            control status
            exit 0
        fi
        # Do not mistake model preparation for completed token validation.
        deadline=$((SECONDS + 120))
        for ((attempt=0; attempt<120 && SECONDS<deadline; attempt++)); do
            case "$stage" in
                setup_required|authorization_required) open_setup; exit 0 ;;
                ready|error) control status; exit 0 ;;
            esac
            sleep 1
            stage=$(control stage) || fail 'Container stopped during startup.'
        done
        control status
        printf 'Still starting. Use ./voxer.sh status or ./voxer.sh setup to inspect progress.\n'
        ;;
    stop) "${COMPOSE[@]}" stop voxer ;;
    status) control status ;;
    setup) wait_control >/dev/null; open_setup ;;
    emotes)
        ensure_image
        "${COMPOSE[@]}" run --rm --no-deps --entrypoint python voxer -m voxer.fetch_emotes --stored-only
        ;;
    prepare)
        ensure_image
        "${COMPOSE[@]}" run --rm --no-deps --entrypoint python voxer -m voxer.prepare
        ;;
    update)
        image=$(selected_image)
        [[ -n "$image" && "$image" != twitch-voxer:local ]] || fail 'Set VOXER_IMAGE to a versioned registry image; update never rebuilds local source.'
        "${COMPOSE[@]}" pull voxer
        printf 'Image downloaded. Run ./voxer.sh stop, then ./voxer.sh start to use it.\n'
        ;;
    *) fail 'Usage: ./voxer.sh start|stop|status|setup|prepare|emotes|update' ;;
esac
