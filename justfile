image := "pool-runners"
config := env_var_or_default("RUNNERS_CONFIG", "runners.toml")

default:
    @just --list

test *args:
    uv run pytest {{ args }}

check:
    uv run pool-runners -c {{ config }} --check

run *args:
    uv run pool-runners -c {{ config }} {{ args }}

build:
    docker build -t {{ image }} .

version *args:
    #!/usr/bin/env bash
    set -euo pipefail
    given=({{ args }})
    old="$(grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2)"
    if [ "${#given[@]}" -eq 0 ]; then
        printf 'v%s\n' "$old"
        exit 0
    fi
    new="${given[0]#v}"
    [[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || {
        echo "это не версия: ${given[0]}" >&2
        exit 1
    }
    if [ "$new" != "$old" ]; then
        grep -rl --exclude-dir=.git --exclude-dir=.venv "$old" . | xargs perl -pi -e "s/\Q$old\E/$new/g"
    fi
    printf 'v%s\n' "$new"

qa:
    bash <(curl -fsSL https://raw.githubusercontent.com/wprhvso/qa-python/v1/scripts/local.sh)

fix:
    bash <(curl -fsSL https://raw.githubusercontent.com/wprhvso/qa-python/v1/scripts/local.sh) --fix
